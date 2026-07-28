"""Живость держателя замка по НОМЕРУ процесса — и почему номера мало.

В контейнере номера процессов маленькие и переиспользуются мгновенно: 26.07 после
рестарта praxis новый python занял /proc/10 — номер мертвеца, державшего замок задачи.
Проверка «есть процесс с таким номером» отвечала «жив» про постороннего, и замок
становился вечным. То же самое жило в двух других местах:

* ``media.py:_ledger_guard`` — каждая операция с исходящими медиа падала бы
  «media outbox ledger is busy» до рестарта (для Егора: перестала слать голос и картинки);
* ``self_model.py:_FileLock`` — плюс кража чужого замка по голому mtime и безусловный
  ``unlink`` чужого замка на выходе (порча проекции её желаний, молча).

Тесты бьют ровно по переиспользованию номера (посторонний ЖИВОЙ процесс с тем же pid),
по обеим сторонам возрастного порога и по правдивости текста отказа.

27.07 корень выправлен: ``process_liveness`` знает метку рождения (``process_started_at``,
второй аргумент ``is_process_alive``, четырёхзначный ``identify``). Три копии
``_proc_started_at``/``_owner_alive`` пока живы — свести их в один вызов должны владельцы
media.py, self_model.py и forge.py. До тех пор ``SharedRootAgreementTests`` ловит их
РАСХОЖДЕНИЕ с корнем; сам факт копий тестом больше не закрепляется.

Тем же заходом закрыта вторая половина: супервизоры ``forge_process``/``forge_verify``
пишут метку рядом со своим номером, иначе ``forge.process(action="stop")`` шлёт killpg
по переиспользованному номеру и убивает постороннего.

Run with:  python praxis_test.py test_pid_identity -v
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import forge
import forge_process
import forge_verify
import media
import process_liveness
import self_model


HAS_PROC = os.name != "nt" and Path("/proc").is_dir()


def ogg(size: int = 16) -> bytes:
    return b"OggS" + b"O" * max(0, size - 4)


def _set_idle(path: Path, seconds: float) -> None:
    """Сделать вид, что держатель молчит ``seconds`` секунд (пульса не было)."""

    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


class MediaLedgerPidIdentityTests(unittest.TestCase):
    """media.MediaSpool._ledger_guard"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_pid_media_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.spool = self._spool()
        self.lock = self.spool._ledger_dir / ".lock"

    def _spool(self) -> media.MediaSpool:
        return media.MediaSpool(
            self.tmp / "workspace" / "media",
            photo_max_bytes=1024,
            audio_max_bytes=2048,
            document_max_bytes=4096,
            max_total_bytes=8192,
            ttl_seconds=60,
            max_queue=4,
            max_turn_media=4,
        )

    def _plant(self, token: str, *, idle: float = 0.0) -> None:
        self.lock.write_text(token, encoding="ascii")
        if idle:
            _set_idle(self.lock, idle)

    def _queue_one(self) -> None:
        item = self.spool.resolve_outbound_bytes(
            ogg(), kind="audio", filename="voice.ogg", target_chat_id=1,
            reply_to_message_id=1, scope="owner", voice_note=True,
        )
        self.spool.enqueue(item)

    def test_reused_process_number_does_not_lock_media_forever(self) -> None:
        # Номер держателя занят ЖИВЫМ посторонним процессом (нами), но метка рождения
        # у него другая — значит держатель мёртв. До починки здесь был вечный busy.
        self._plant(f"{os.getpid()}:111111:deadbeef")
        with mock.patch.object(media, "_proc_started_at", lambda pid: "777777"):
            with self.spool._ledger_guard(timeout=0.4):
                pass
            self._queue_one()
        self.assertEqual(len(self._spool().pending()), 1)

    def test_confirmed_live_holder_is_never_robbed(self) -> None:
        # Обратная сторона: метка рождения совпала — держатель настоящий, замок не наш.
        self._plant(f"{os.getpid()}:111111:deadbeef")
        with mock.patch.object(media, "_proc_started_at", lambda pid: "111111"):
            with self.assertRaises(TimeoutError):
                with self.spool._ledger_guard(timeout=0.3):
                    pass
        self.assertTrue(self.lock.exists())

    def test_refusal_names_the_holder_and_every_threshold(self) -> None:
        self._plant(f"{os.getpid()}:111111:deadbeef", idle=3.0)
        with mock.patch.object(media, "_proc_started_at", lambda pid: "111111"):
            with self.assertRaises(TimeoutError) as caught:
                with self.spool._ledger_guard(timeout=0.3):
                    pass
        text = str(caught.exception)
        self.assertIn(f"held by pid {os.getpid()}", text)
        self.assertIn("111111", text)                       # с какого времени держит
        self.assertIn("identity confirmed", text)           # насколько мы в этом уверены
        self.assertIn("0.3s", text)                         # сколько ждали
        self.assertIn(f"{media.LEDGER_ABANDONED_SEC:g}s", text)   # когда отберём
        # ⚠ Порог измеряет пульс, а пульс — нить best-effort. Назвать возраст файла
        # «пульсом» значит обещать предел, которого может не быть вовсе.
        self.assertNotIn("last heartbeat", text)
        self.assertIn("may not be running", text)

    def test_unreadable_token_refusal_names_the_grace_not_the_minute(self) -> None:
        # ⚠ Здесь применён грейс в секунду, а отказ называл 60с: ей говорили «ждать до
        # минуты» и отбирали замок через полсекунды.
        self._plant("", idle=0.5)
        with self.assertRaises(TimeoutError) as caught:
            with self.spool._ledger_guard(timeout=0.2):
                pass
        text = str(caught.exception)
        self.assertIn(f"{media.LEDGER_UNREADABLE_GRACE_SEC:g}s", text)
        self.assertIn("unreadable", text)
        self.assertIn(f"not after {media.LEDGER_ABANDONED_SEC:g}s", text)

    def test_takeover_log_names_the_reason_that_was_actually_applied(self) -> None:
        # Кража по грейсу нечитаемого токена — в логе обязан стоять грейс, а не «60с
        # бездействия»; кража по мёртвому номеру — «процесса нет», а не «бездействие».
        self._plant("", idle=media.LEDGER_UNREADABLE_GRACE_SEC + 0.5)
        with self.assertLogs("media", level="WARNING") as logs:
            with self.spool._ledger_guard(timeout=0.4):
                pass
        grace_line = "\n".join(logs.output)
        self.assertIn("unreadable", grace_line)
        self.assertIn(f"{media.LEDGER_UNREADABLE_GRACE_SEC:g}s grace", grace_line)
        self.assertNotIn(f"{media.LEDGER_ABANDONED_SEC:g}s without", grace_line)

        self._plant(f"{os.getpid()}:111111:deadbeef")       # свежий mtime, номер чужой
        with mock.patch.object(media, "_proc_started_at", lambda pid: "777777"):
            with self.assertLogs("media", level="WARNING") as logs:
                with self.spool._ledger_guard(timeout=0.4):
                    pass
        reuse_line = "\n".join(logs.output)
        self.assertIn("the number was reused", reuse_line)
        self.assertNotIn("untouched 0.0s, past", reuse_line)

    def test_grace_is_a_named_env_lever_like_every_other_threshold(self) -> None:
        # Закон 2: молчаливых пределов не бывает. Грейс не был ручкой вообще.
        with mock.patch.dict(os.environ,
                             {"PRAXIS_MEDIA_LEDGER_UNREADABLE_GRACE_SEC": "3.5"}):
            self.assertEqual(
                media._env_float("PRAXIS_MEDIA_LEDGER_UNREADABLE_GRACE_SEC", 1.0), 3.5)

    def test_a_live_in_process_holder_is_never_robbed_even_without_a_heartbeat(self) -> None:
        # ⚠ 27.07, проба адверсария (peak=2). Пульс — нить best-effort: start() глотает
        # RuntimeError, а _run() мог навсегда выйти на первой OSError. В контейнере два
        # MediaSpool живут в ОДНОМ процессе (agent.py:367 и mtproto_runner.py:286), и с
        # выключенным пульсом сосед сносил замок ЖИВОЙ транзакции — оба писали в леджер.
        other = self._spool()
        entered = threading.Event()
        release = threading.Event()
        peak = 0
        inside = 0
        counter = threading.Lock()
        failure: list[BaseException] = []

        def hold() -> None:
            nonlocal inside, peak
            try:
                with self.spool._ledger_guard(timeout=2.0):
                    with counter:
                        inside += 1
                        peak = max(peak, inside)
                    entered.set()
                    release.wait(timeout=5)
                    with counter:
                        inside -= 1
            except BaseException as exc:      # pragma: no cover - виден только при поломке
                failure.append(exc)

        with mock.patch.object(media._LockHeartbeat, "start", lambda self: self), \
                mock.patch.object(media, "LEDGER_ABANDONED_SEC", 0.4):
            holder = threading.Thread(target=hold, daemon=True)
            holder.start()
            self.assertTrue(entered.wait(timeout=5))
            time.sleep(0.8)                   # вдвое дольше порога, но держатель жив
            with self.assertRaises(TimeoutError) as caught:
                with other._ledger_guard(timeout=0.5):
                    with counter:
                        inside += 1
                        peak = max(peak, inside)
            release.set()
            holder.join(timeout=5)
        self.assertEqual(failure, [])
        self.assertEqual(peak, 1, "второй писатель вошёл внутрь живой чужой транзакции")
        text = str(caught.exception)
        self.assertIn("this very process", text)
        self.assertIn("never taken over", text)
        # Отказ обязан признать, что мы упёрлись в собственный застрявший поток.
        self.assertIn("its own thread looks stuck", text)

    def test_a_leaked_lock_of_our_own_process_still_expires(self) -> None:
        # Обратная сторона правила «своего не грабим»: файл, оставшийся от НАШЕГО же
        # процесса после неудавшегося unlink, в реестре не числится и стареть обязан —
        # иначе правило само стало бы вечным замком.
        self._plant(f"{os.getpid()}:{media._proc_started_at(os.getpid())}:deadbeef",
                    idle=media.LEDGER_ABANDONED_SEC + 5.0)
        self.assertIsNone(media._held_here(self.lock))
        with self.spool._ledger_guard(timeout=0.4):
            pass

    def test_heartbeat_survives_one_failed_touch_and_says_so(self) -> None:
        # ⚠ Раньше _run() выходил НАВСЕГДА и МОЛЧА на первом же OSError из os.utime:
        # с этой секунды «бездействие» означало «удержание», а отказ обещал пульс.
        missing = self.spool._ledger_dir / ".lock-that-a-contender-removed"
        beat = media._LockHeartbeat(missing, 0.05, token="")
        with self.assertLogs("media", level="WARNING") as logs:
            beat.start()
            time.sleep(0.4)                   # ~8 попыток, каждая с ENOENT
            alive = beat._thread is not None and beat._thread.is_alive()
        beat.stop()
        self.assertTrue(alive, "пульс умер навсегда на первой же осечке os.utime")
        text = "\n".join(logs.output)
        self.assertEqual(text.count("could not touch"), 1, "жалоба повторяется каждые 50мс")
        self.assertIn("keeps trying", text)

    def test_heartbeat_stops_refreshing_a_lock_that_is_no_longer_ours(self) -> None:
        # ⚠ Пульс бил по ПУТИ. После перехвата мы освежали ЧУЖОЙ замок — и мертвец
        # перехватчика выглядел бы живым до конца нашей транзакции.
        self._plant("successor-token")
        beat = media._LockHeartbeat(self.lock, 0.05, token="our-token")
        with self.assertLogs("media", level="WARNING") as logs:
            beat.start()
            deadline = time.monotonic() + 3.0
            while not beat.lost and time.monotonic() < deadline:
                time.sleep(0.02)
        beat.stop()
        self.assertTrue(beat.lost, "пульс продолжал держать чужой замок свежим")
        self.assertIn("no longer ours", "\n".join(logs.output))
        stamp = self.lock.stat().st_mtime
        time.sleep(0.2)
        self.assertEqual(self.lock.stat().st_mtime, stamp, "чужой замок всё ещё освежают")

    def test_idle_threshold_holds_on_both_sides(self) -> None:
        # Держатель жив и подтверждён; решает только молчание. Ниже порога — ждём.
        self._plant(f"{os.getpid()}:111111:deadbeef",
                    idle=media.LEDGER_ABANDONED_SEC - 5.0)
        with mock.patch.object(media, "_proc_started_at", lambda pid: "111111"):
            with self.assertRaises(TimeoutError):
                with self.spool._ledger_guard(timeout=0.3):
                    pass
            # Выше порога — забираем: иначе брошенная транзакция запирает медиа навсегда.
            _set_idle(self.lock, media.LEDGER_ABANDONED_SEC + 5.0)
            with self.spool._ledger_guard(timeout=0.4):
                pass
        self.assertFalse(self.lock.exists())

    def test_legacy_token_without_a_birth_mark_still_expires(self) -> None:
        # Замок, оставшийся от версии до починки: тождества не доказывает, но и вечным
        # быть не должен.
        self._plant(f"{os.getpid()}:deadbeef", idle=media.LEDGER_ABANDONED_SEC + 5.0)
        with self.spool._ledger_guard(timeout=0.4):
            pass

    def test_legacy_token_refusal_admits_it_cannot_prove_identity(self) -> None:
        self._plant(f"{os.getpid()}:deadbeef")
        with self.assertRaises(TimeoutError) as caught:
            with self.spool._ledger_guard(timeout=0.3):
                pass
        self.assertIn("legacy token", str(caught.exception))

    def test_heartbeat_protects_a_slow_but_honest_holder(self) -> None:
        # Возрастной порог не должен грабить того, кто просто долго работает: пока
        # держатель дышит, mtime свежий и замок остаётся за ним. Замок здесь НЕ в реестре
        # своих (иначе правило «своего не грабим» сделало бы проверку вакуумной) — это
        # ровно случай честного держателя в СОСЕДНЕМ процессе.
        other = self._spool()
        token = f"{os.getpid()}:111111:deadbeef"
        self._plant(token, idle=30.0)
        beat = media._LockHeartbeat(self.lock, 0.05, token=token)
        self.addCleanup(beat.stop)
        with mock.patch.object(media, "LEDGER_ABANDONED_SEC", 0.5), \
                mock.patch.object(media, "_proc_started_at", lambda pid: "111111"):
            self.assertIsNone(media._held_here(self.lock))
            beat.start()
            time.sleep(0.6)                   # дольше порога в 0.5с — но пульс бьёт
            with self.assertRaises(TimeoutError):
                with other._ledger_guard(timeout=0.3):
                    pass
            # А без пульса тот же возраст замок отдаёт — порог живой, а не декоративный.
            beat.stop()
            _set_idle(self.lock, 1.2)
            with other._ledger_guard(timeout=0.4):
                pass

    @unittest.skipUnless(HAS_PROC, "birth marks live in /proc")
    def test_real_unrelated_live_process_does_not_hold_the_ledger(self) -> None:
        # Тот же сценарий без единого мока: чужой ЖИВОЙ процесс с этим номером.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        birth = media._proc_started_at(child.pid)
        self.assertTrue(birth.isdigit(), "не прочитали метку рождения живого процесса")
        self._plant(f"{child.pid}:{int(birth) - 1}:deadbeef")
        with self.spool._ledger_guard(timeout=0.4):
            pass
        self.assertTrue(process_liveness.is_process_alive(child.pid),
                        "процесс-«тёзка» обязан быть живым — иначе тест вакуумный")


class SelfLockPidIdentityTests(unittest.TestCase):
    """self_model._FileLock — замок проекции желаний и авторских заметок"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_pid_self_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lock = self.tmp / "self.lock"

    def _plant(self, *, pid: int | None = None, started_at: str = "111111",
               token: str = "foreign", idle: float = 0.0) -> None:
        self.lock.write_text(json.dumps({
            "pid": os.getpid() if pid is None else pid,
            "started_at": started_at,
            "token": token,
            "at": "2026-07-26T19:27:00Z",
        }), encoding="utf-8")
        if idle:
            _set_idle(self.lock, idle)

    def test_reused_process_number_does_not_lock_desires_forever(self) -> None:
        self._plant()
        with mock.patch.object(self_model, "_proc_started_at", lambda pid: "777777"):
            with self_model.FileLock(self.lock, timeout=0.4, stale_after=600):
                pass
        self.assertFalse(self.lock.exists())

    def test_confirmed_live_holder_is_never_robbed(self) -> None:
        self._plant()
        with mock.patch.object(self_model, "_proc_started_at", lambda pid: "111111"):
            with self.assertRaises(TimeoutError):
                with self_model.FileLock(self.lock, timeout=0.3, stale_after=600):
                    pass
        self.assertEqual(json.loads(self.lock.read_text(encoding="utf-8"))["token"], "foreign")

    def test_refusal_names_the_holder_and_every_threshold(self) -> None:
        self._plant(idle=2.0)
        with mock.patch.object(self_model, "_proc_started_at", lambda pid: "111111"):
            with self.assertRaises(TimeoutError) as caught:
                with self_model.FileLock(self.lock, timeout=0.3, stale_after=600):
                    pass
        text = str(caught.exception)
        self.assertIn(f"held by pid {os.getpid()}", text)
        self.assertIn("2026-07-26T19:27:00Z", text)     # с какого времени держит
        self.assertIn("identity confirmed", text)
        self.assertIn("0.3s", text)
        self.assertIn("600s", text)
        self.assertNotIn("last heartbeat", text)        # пульса может не быть вовсе
        self.assertIn("may not be running", text)

    def test_unreadable_token_refusal_names_the_grace_not_the_minute(self) -> None:
        self.lock.write_text("{broken", encoding="utf-8")
        _set_idle(self.lock, 0.5)
        with self.assertRaises(TimeoutError) as caught:
            with self_model.FileLock(self.lock, timeout=0.2, stale_after=600):
                pass
        text = str(caught.exception)
        self.assertIn(f"{self_model.SELF_LOCK_UNREADABLE_GRACE_SEC:g}s", text)
        self.assertIn("unreadable", text)
        self.assertIn("not after 600s", text)

    def test_grace_is_a_named_env_lever_like_every_other_threshold(self) -> None:
        with mock.patch.dict(os.environ,
                             {"PRAXIS_SELF_LOCK_UNREADABLE_GRACE_SEC": "3.5"}):
            self.assertEqual(
                self_model._env_float("PRAXIS_SELF_LOCK_UNREADABLE_GRACE_SEC", 1.0), 3.5)

    def test_a_live_in_process_holder_is_never_robbed_even_without_a_heartbeat(self) -> None:
        # ⚠ 27.07, проба адверсария (peak=2): с выключенным пульсом (а он best-effort)
        # второй писатель входил внутрь живой транзакции и портил проекцию желаний молча.
        entered = threading.Event()
        release = threading.Event()
        peak = 0
        inside = 0
        counter = threading.Lock()
        failure: list[BaseException] = []

        def hold() -> None:
            nonlocal inside, peak
            try:
                with self_model.FileLock(self.lock, timeout=2.0, stale_after=0.4):
                    with counter:
                        inside += 1
                        peak = max(peak, inside)
                    entered.set()
                    release.wait(timeout=5)
                    with counter:
                        inside -= 1
            except BaseException as exc:      # pragma: no cover - виден только при поломке
                failure.append(exc)

        with mock.patch.object(self_model._LockHeartbeat, "start", lambda self: self):
            holder = threading.Thread(target=hold, daemon=True)
            holder.start()
            self.assertTrue(entered.wait(timeout=5))
            time.sleep(0.8)                   # вдвое дольше stale_after, держатель жив
            with self.assertRaises(TimeoutError) as caught:
                with self_model.FileLock(self.lock, timeout=0.5, stale_after=0.4):
                    with counter:
                        inside += 1
                        peak = max(peak, inside)
            release.set()
            holder.join(timeout=5)
        self.assertEqual(failure, [])
        self.assertEqual(peak, 1, "второй писатель вошёл внутрь живой чужой транзакции")
        text = str(caught.exception)
        self.assertIn("this very process", text)
        self.assertIn("never taken over", text)
        self.assertIn("its own thread looks stuck", text)

    def test_a_leaked_lock_of_our_own_process_still_expires(self) -> None:
        # Своего живого не грабим — но протёкший файл в реестре не числится и стареет.
        self._plant(started_at=self_model._proc_started_at(os.getpid()), idle=30.0)
        self.assertIsNone(self_model._held_here(self.lock))
        with self_model.FileLock(self.lock, timeout=0.4, stale_after=10):
            pass

    def test_heartbeat_survives_one_failed_touch_and_says_so(self) -> None:
        missing = self.tmp / "gone.lock"
        beat = self_model._LockHeartbeat(missing, 0.05, token="")
        with self.assertLogs("self_model", level="WARNING") as logs:
            beat.start()
            time.sleep(0.4)
            alive = beat._thread is not None and beat._thread.is_alive()
        beat.stop()
        self.assertTrue(alive, "пульс умер навсегда на первой же осечке os.utime")
        text = "\n".join(logs.output)
        self.assertEqual(text.count("could not touch"), 1, "жалоба повторяется каждые 50мс")
        self.assertIn("keeps trying", text)

    def test_heartbeat_stops_refreshing_a_lock_that_is_no_longer_ours(self) -> None:
        self._plant(token="successor")
        beat = self_model._LockHeartbeat(self.lock, 0.05, token="ours")
        with self.assertLogs("self_model", level="WARNING") as logs:
            beat.start()
            deadline = time.monotonic() + 3.0
            while not beat.lost and time.monotonic() < deadline:
                time.sleep(0.02)
        beat.stop()
        self.assertTrue(beat.lost, "пульс продолжал держать чужой замок свежим")
        self.assertIn("no longer ours", "\n".join(logs.output))
        stamp = self.lock.stat().st_mtime
        time.sleep(0.2)
        self.assertEqual(self.lock.stat().st_mtime, stamp, "чужой замок всё ещё освежают")

    def test_idle_threshold_holds_on_both_sides(self) -> None:
        with mock.patch.object(self_model, "_proc_started_at", lambda pid: "111111"):
            self._plant(idle=8.0)
            with self.assertRaises(TimeoutError):
                with self_model.FileLock(self.lock, timeout=0.3, stale_after=10):
                    pass
            _set_idle(self.lock, 12.0)
            with self_model.FileLock(self.lock, timeout=0.4, stale_after=10):
                pass

    def test_exit_never_unlinks_a_lock_that_is_no_longer_ours(self) -> None:
        # ⚠ Раньше выход снимал ЧУЖОЙ замок: следующий писатель входил внутрь чужой
        # транзакции и портил проекцию желаний молча.
        lock = self_model.FileLock(self.lock, timeout=0.4, stale_after=600)
        with lock:
            self._plant(token="successor")          # замок перехватили, пока мы писали
        self.assertTrue(self.lock.exists(), "чужой замок снят на выходе")
        self.assertEqual(json.loads(self.lock.read_text(encoding="utf-8"))["token"],
                         "successor")
        self.assertTrue(lock.lost_to_takeover, "потерю замка проглотили молча")

    def test_own_lock_is_released_and_the_takeover_flag_stays_clean(self) -> None:
        lock = self_model.FileLock(self.lock, timeout=0.4)
        with lock:
            self.assertTrue(self.lock.exists())
            record = json.loads(self.lock.read_text(encoding="utf-8"))
            self.assertEqual(record["pid"], os.getpid())
            self.assertIn("started_at", record)
        self.assertFalse(self.lock.exists())
        self.assertFalse(lock.lost_to_takeover)

    def test_heartbeat_protects_a_slow_but_honest_holder(self) -> None:
        # Замок здесь НЕ в реестре своих — иначе правило «своего живого не грабим»
        # сделало бы проверку вакуумной. Это честный держатель в соседнем процессе.
        self._plant(token="neighbour", idle=30.0)
        beat = self_model._LockHeartbeat(self.lock, 0.05, token="neighbour")
        self.addCleanup(beat.stop)
        with mock.patch.object(self_model, "_proc_started_at", lambda pid: "111111"):
            self.assertIsNone(self_model._held_here(self.lock))
            beat.start()
            time.sleep(0.6)                   # дольше stale_after=0.5, но пульс бьёт
            with self.assertRaises(TimeoutError):
                with self_model.FileLock(self.lock, timeout=0.3, stale_after=0.5):
                    pass
            # Без пульса тот же возраст замок отдаёт.
            beat.stop()
            _set_idle(self.lock, 1.2)
            with self_model.FileLock(self.lock, timeout=0.4, stale_after=0.5):
                pass

    @unittest.skipUnless(HAS_PROC, "birth marks live in /proc")
    def test_real_unrelated_live_process_does_not_hold_the_self_lock(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        birth = self_model._proc_started_at(child.pid)
        self.assertTrue(birth.isdigit())
        self._plant(pid=child.pid, started_at=str(int(birth) - 1))
        with self_model.FileLock(self.lock, timeout=0.4, stale_after=600):
            pass
        self.assertTrue(process_liveness.is_process_alive(child.pid),
                        "процесс-«тёзка» обязан быть живым — иначе тест вакуумный")


class BirthMarkTests(unittest.TestCase):
    """Сама метка рождения и общий корень в process_liveness."""

    @unittest.skipUnless(HAS_PROC, "birth marks live in /proc")
    def test_birth_mark_is_read_for_a_live_process_and_absent_for_a_dead_one(self) -> None:
        for reader in (media._proc_started_at, self_model._proc_started_at):
            self.assertTrue(reader(os.getpid()).isdigit())
            self.assertEqual(reader(2 ** 22 - 1), "")     # заведомо свободный номер

    def test_missing_birth_mark_is_reported_as_unproven_not_as_death(self) -> None:
        # Вне Linux метки нет. «Не знаю» не должно превращаться ни в «мёртв» (украли бы
        # живой замок), ни в тихое «жив» без оговорки — оговорка есть в тексте отказа.
        for module in (media, self_model):
            with mock.patch.object(module, "_proc_started_at", lambda pid: ""):
                self.assertTrue(module._owner_alive(os.getpid(), "111111"))
            with mock.patch.object(module, "_proc_started_at", lambda pid: "222222"):
                self.assertFalse(module._owner_alive(os.getpid(), "111111"))
                self.assertTrue(module._owner_alive(os.getpid(), ""))

    def test_the_root_knows_the_birth_mark_and_keeps_the_second_argument(self) -> None:
        # 27.07: корень научен метке рождения. Раньше здесь стоял обратный инвариант
        # («утилита про метку не знает»), и он держал три копии как норму.
        import inspect

        params = inspect.signature(process_liveness.is_process_alive).parameters
        self.assertEqual(list(params), ["pid", "started_at"],
                         "у корня отобрали метку рождения — копии снова станут нормой")
        self.assertTrue(callable(process_liveness.process_started_at))


class SharedRootAgreementTests(unittest.TestCase):
    """Три копии против корня: ловим РАСХОЖДЕНИЕ, а не сам факт копий.

    ``_proc_started_at``/``_owner_alive`` лежат байт-в-байт в media.py, self_model.py и
    forge.py. Свести их в один вызов ``process_liveness`` — работа владельцев этих трёх
    файлов; пока копии живы, единственное, что можно проверить, — что они отвечают ровно
    как корень. Разъедутся — покраснеет здесь, а не в проде на чьём-то замке.
    """

    MODULES = (media, self_model, forge)

    @unittest.skipUnless(HAS_PROC, "birth marks live in /proc")
    def test_every_copy_reads_the_same_field_of_proc_stat(self) -> None:
        mine = process_liveness.process_started_at(os.getpid())
        self.assertTrue(mine.isdigit(), "корень не прочитал собственную метку рождения")
        for module in self.MODULES:
            self.assertEqual(module._proc_started_at(os.getpid()), mine,
                             f"{module.__name__}._proc_started_at разошёлся с корнем")
            self.assertEqual(module._proc_started_at(2 ** 22 - 1),
                             process_liveness.process_started_at(2 ** 22 - 1),
                             f"{module.__name__} иначе отвечает про свободный номер")

    def test_every_copy_answers_exactly_like_the_root_on_the_whole_matrix(self) -> None:
        # Матрица: живой/свободный номер × записанная метка (нет/совпала/чужая) ×
        # что прочиталось сейчас (нечитаемо/есть). Мокаем читатель метки в каждом
        # модуле и в корне одинаково — сравниваем только логику решения.
        alive, free = os.getpid(), 2 ** 22 - 1
        checked = 0
        for pid in (alive, free):
            for recorded in ("", "111111", "222222"):
                for current in ("", "111111"):
                    with mock.patch.object(process_liveness, "process_started_at",
                                           lambda _pid, value=current: value):
                        root = process_liveness.is_process_alive(pid, recorded)
                    for module in self.MODULES:
                        with mock.patch.object(module, "_proc_started_at",
                                               lambda _pid, value=current: value):
                            copy = module._owner_alive(pid, recorded)
                        self.assertEqual(
                            copy, root,
                            f"{module.__name__}._owner_alive(pid={pid}, recorded={recorded!r}) "
                            f"при текущей метке {current!r} разошёлся с process_liveness")
                        checked += 1
        self.assertEqual(checked, 2 * 3 * 2 * len(self.MODULES))

    def test_the_matrix_would_notice_a_copy_that_drifted(self) -> None:
        # Мысленно сломанная копия обязана валить проверку выше — иначе она вакуумна.
        with mock.patch.object(media, "_owner_alive", lambda pid, started_at="": True):
            with self.assertRaises(AssertionError):
                self.test_every_copy_answers_exactly_like_the_root_on_the_whole_matrix()


class IdentifyVerdictTests(unittest.TestCase):
    """identify() — четыре ответа, потому что «не знаю» не должно выглядеть как факт."""

    def test_free_number_is_gone(self) -> None:
        verdict = process_liveness.identify(2 ** 22 - 1, "111111")
        self.assertEqual(verdict.verdict, process_liveness.GONE)
        self.assertFalse(verdict.alive)
        self.assertFalse(verdict.safe_to_signal)
        self.assertIn(str(2 ** 22 - 1), verdict.note)

    def test_a_record_without_a_mark_is_unproven_alive_but_unsafe_to_signal(self) -> None:
        # Легаси-state.json с прода: номер есть, метки нет. Ждать по нему можно,
        # слать SIGTERM — нет; и отказ обязан сказать, чего именно мы не знаем.
        verdict = process_liveness.identify(os.getpid(), "")
        self.assertEqual(verdict.verdict, process_liveness.UNPROVEN)
        self.assertTrue(verdict.alive, "недоказанное тождество нельзя хоронить")
        self.assertFalse(verdict.safe_to_signal, "сигнал по недоказанному номеру убьёт чужого")
        self.assertIn("не доказано", verdict.note)
        self.assertIn("старого образца", verdict.note)

    def test_an_unreadable_mark_is_unproven_and_says_why(self) -> None:
        with mock.patch.object(process_liveness, "process_started_at", lambda pid: ""):
            verdict = process_liveness.identify(os.getpid(), "111111")
        self.assertEqual(verdict.verdict, process_liveness.UNPROVEN)
        self.assertTrue(verdict.alive)
        self.assertFalse(verdict.safe_to_signal)
        self.assertIn("прочитать не вышло", verdict.note)

    def test_a_matching_mark_is_the_only_thing_that_licenses_a_signal(self) -> None:
        with mock.patch.object(process_liveness, "process_started_at", lambda pid: "111111"):
            verdict = process_liveness.identify(os.getpid(), "111111")
        self.assertEqual(verdict.verdict, process_liveness.SAME)
        self.assertTrue(verdict.alive)
        self.assertTrue(verdict.safe_to_signal)

    def test_a_reused_number_is_named_as_a_stranger_with_both_marks(self) -> None:
        with mock.patch.object(process_liveness, "process_started_at", lambda pid: "777777"):
            verdict = process_liveness.identify(os.getpid(), "111111")
        self.assertEqual(verdict.verdict, process_liveness.OTHER)
        self.assertFalse(verdict.alive)
        self.assertFalse(verdict.safe_to_signal)
        self.assertIn("777777", verdict.note)      # кто занял номер сейчас
        self.assertIn("111111", verdict.note)      # кого мы искали
        self.assertIn("посторонний", verdict.note)

    @unittest.skipUnless(HAS_PROC, "birth marks live in /proc")
    def test_a_real_live_namesake_is_recognised_without_a_single_mock(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        birth = process_liveness.process_started_at(child.pid)
        self.assertTrue(birth.isdigit())
        self.assertTrue(process_liveness.identify(child.pid, birth).safe_to_signal)
        stranger = process_liveness.identify(child.pid, str(int(birth) - 1))
        self.assertEqual(stranger.verdict, process_liveness.OTHER)
        self.assertFalse(stranger.safe_to_signal)
        self.assertTrue(process_liveness.is_process_alive(child.pid),
                        "номер обязан быть занят — иначе тест вакуумный")


class DetachedStateBirthMarkTests(unittest.TestCase):
    """state.json супервизоров: номер без метки = «останови мою команду» убивает чужого.

    ``forge.process(action="stop")`` шлёт ``killpg`` по ``child_pid`` из state.json, а
    ``forge.verify(action="stop")`` — по ``supervisor_pid``. Пока метки там не было,
    единственной защитой был случай: на проде 26.07 /proc/10 после рестарта занял новый
    python, а номера 71/72/111 стоят свободными и достанутся первому же спавну.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_pid_detached_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _mark(self, value: object, field: str) -> None:
        self.assertIsInstance(value, str, f"{field} обязано быть строкой даже вне Linux")
        if HAS_PROC:
            self.assertTrue(str(value).isdigit(), f"{field} не прочитана на Linux")

    def test_process_supervisor_stamps_both_pids_and_closes_the_child(self) -> None:
        directory = self.tmp / "proc-unit"
        directory.mkdir()
        request = directory / "request.json"
        request.write_text(json.dumps({
            "command": f'"{sys.executable}" -c "print(6*7)"',
            "cwd": str(self.tmp), "timeout": 60,
        }), encoding="utf-8")
        self.assertEqual(forge_process.run(request), 0)
        state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
        self._mark(state.get("child_started_at"), "child_started_at")
        self._mark(state.get("supervisor_started_at"), "supervisor_started_at")
        self.assertEqual(state["supervisor_pid"], os.getpid())
        self.assertTrue(state["child_pid"])
        # Команда кончилась — номер больше не наш, и state.json обязан это признать,
        # иначе «останови» целится в того, кто занял номер после.
        self.assertEqual(state["status"], "done")
        self.assertIs(state.get("child_running"), False)

    def test_verification_supervisor_stamps_itself_and_names_the_deadline(self) -> None:
        directory = self.tmp / "verify-unit"
        directory.mkdir()
        request = directory / "request.json"
        request.write_text(json.dumps({
            "root": str(self.tmp), "max_parallel": 1, "timeout": 60,
            "checks": [{"id": "ok", "command": f'"{sys.executable}" -c "print(1)"'}],
        }), encoding="utf-8")
        self.assertEqual(forge_verify.run(request), 0)
        state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
        self._mark(state.get("supervisor_started_at"), "supervisor_started_at")
        self.assertEqual(state["supervisor_pid"], os.getpid())
        result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["matrix_deadline_s"], 60)
        self._mark(result.get("supervisor_started_at"), "supervisor_started_at")

    def test_a_stamped_state_file_proves_a_reused_number_is_not_ours(self) -> None:
        # Ради чего метка и пишется: forge.process(stop) обязан уметь ответить «это уже
        # не мой процесс» вместо killpg вслепую.
        state = {"child_pid": os.getpid(), "child_started_at": "111111"}
        with mock.patch.object(process_liveness, "process_started_at", lambda pid: "777777"):
            verdict = process_liveness.identify(state["child_pid"], state["child_started_at"])
        self.assertFalse(verdict.safe_to_signal)
        # А легаси-файл (метки нет) обязан давать «не знаю», а не молчаливое разрешение.
        legacy = process_liveness.identify(os.getpid(), "")
        self.assertEqual(legacy.verdict, process_liveness.UNPROVEN)
        self.assertFalse(legacy.safe_to_signal)

    def test_the_matrix_default_deadline_is_a_named_lever(self) -> None:
        # Закон 2: срок, который применяется всегда, обязан быть назван и сдвигаем.
        # Раньше это был безымянный литерал 900, молча переписывавший её timeout=0.
        try:
            with mock.patch.dict(os.environ):
                os.environ.pop("PRAXIS_VERIFY_TIMEOUT_SEC", None)
                self.assertEqual(importlib.reload(forge_verify).MATRIX_DEADLINE_SEC, 900)
            with mock.patch.dict(os.environ, {"PRAXIS_VERIFY_TIMEOUT_SEC": "123"}):
                self.assertEqual(importlib.reload(forge_verify).MATRIX_DEADLINE_SEC, 123)
            with mock.patch.dict(os.environ, {"PRAXIS_VERIFY_TIMEOUT_SEC": "не число"}):
                self.assertEqual(importlib.reload(forge_verify).MATRIX_DEADLINE_SEC, 900,
                                 "опечатка в окружении не смеет ронять импорт супервизора")
        finally:
            importlib.reload(forge_verify)

    def test_a_check_without_its_own_deadline_is_told_the_borrowed_one(self) -> None:
        # Проверка без своего срока идёт по общему — и обязана это сказать в своём логе.
        directory = self.tmp / "verify-note"
        (directory / "logs").mkdir(parents=True)
        calls: list[dict] = []
        fake = types.SimpleNamespace(call=lambda verb, args, **kw: (
            calls.append({"verb": verb, "args": args, "kw": kw}),
            {"ok": True, "body": "готово", "exit": 0})[1])
        state = {"checks": {}}
        with mock.patch.dict(sys.modules, {"serverd_client": fake}):
            row = forge_verify._run_one(0, {"id": "lint", "command": "ruff ."}, self.tmp,
                                        directory, 300, state, threading.Lock(), "host", "m1")
        self.assertEqual(row["deadline_s"], 300)
        self.assertEqual(calls[0]["args"]["timeout"], 300, "демон обязан знать предел")
        self.assertGreater(calls[0]["kw"]["timeout"], 300, "клиент переживает бюджет демона")
        log = Path(row["log"]).read_text(encoding="utf-8")
        self.assertIn("300с", log)
        self.assertIn("PRAXIS_VERIFY_TIMEOUT_SEC", log)

    def test_a_check_with_its_own_deadline_is_not_lectured(self) -> None:
        directory = self.tmp / "verify-own"
        (directory / "logs").mkdir(parents=True)
        fake = types.SimpleNamespace(
            call=lambda verb, args, **kw: {"ok": True, "body": "готово", "exit": 0})
        with mock.patch.dict(sys.modules, {"serverd_client": fake}):
            row = forge_verify._run_one(0, {"id": "t", "command": "pytest", "timeout": 1800},
                                        self.tmp, directory, 300, {"checks": {}},
                                        threading.Lock(), "host", "m1")
        self.assertEqual(row["deadline_s"], 1800, "осознанный срок проверки не переписываем")
        self.assertNotIn("[срок]", Path(row["log"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
