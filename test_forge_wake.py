"""Пробуждение-на-готово: детектор завершений воркеров + приоритет (PASS wake)."""
import contextlib
import datetime as dt
import json
import tempfile
import os
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

import forge


class ForgeWakeDetectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_tasks = forge.TASKS_DIR
        self._orig_seen = forge._WAKE_SEEN
        forge.TASKS_DIR = self.tmp / "tasks"
        forge._WAKE_SEEN = self.tmp / "wake_seen.json"
        forge.TASKS_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        forge.TASKS_DIR = self._orig_tasks
        forge._WAKE_SEEN = self._orig_seen

    def _make(self, task_id, agent_id, status, priority="normal", role="worker", finished=None):
        d = forge.TASKS_DIR / task_id / "agents" / agent_id
        d.mkdir(parents=True, exist_ok=True)
        (forge.TASKS_DIR / task_id / "task.json").write_text(
            json.dumps({"id": task_id, "goal": "цель", "priority": priority, "status": "active"}),
            encoding="utf-8")
        fin = finished or dt.datetime.now(dt.timezone.utc).isoformat()
        (d / "result.json").write_text(
            json.dumps({"status": status, "role": role, "result": "готово", "finished": fin}),
            encoding="utf-8")

    def test_norm_priority(self):
        self.assertEqual(forge._norm_priority("urgent"), "urgent")
        self.assertEqual(forge._norm_priority("срочно"), "urgent")
        self.assertEqual(forge._norm_priority(""), "normal")
        self.assertEqual(forge._norm_priority("whatever"), "normal")

    def test_normal_pending_once_then_consumed(self):
        self._make("code-a", "ag1", "done", "normal")
        first = forge.pending_completions(consume=True)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["priority"], "normal")
        self.assertEqual(first[0]["status"], "done")
        self.assertEqual(forge.pending_completions(consume=True), [])  # idempotent

    def test_urgent_peek_then_mark_only_urgent(self):
        self._make("code-u", "ag1", "done", "urgent")
        self._make("code-n", "ag1", "done", "normal")
        self.assertTrue(forge.has_urgent_pending())
        peek = forge.pending_completions(consume=False)
        self.assertEqual(len(peek), 2)  # peek does not consume
        urgent = [c for c in peek if c["priority"] == "urgent"]
        forge.mark_seen([c["key"] for c in urgent])
        self.assertFalse(forge.has_urgent_pending())          # urgent consumed
        left = forge.pending_completions(consume=True)         # normal still fresh
        self.assertEqual([c["priority"] for c in left], ["normal"])

    def test_scout_and_reviewer_never_wake(self):
        self._make("code-s", "ag1", "done", "normal", role="scout")
        self._make("code-r", "ag2", "done", "normal", role="reviewer")
        self.assertEqual(forge.pending_completions(consume=True), [])

    def test_failed_worker_wakes(self):
        self._make("code-f", "ag1", "failed", "urgent")
        self.assertTrue(forge.has_urgent_pending())

    def test_stale_completion_ignored(self):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
        self._make("code-old", "ag1", "done", "normal", finished=old)
        self.assertEqual(forge.pending_completions(consume=True), [])

    def test_corrupt_utf8_result_still_wakes(self):
        # завершённый воркер с одним битым байтом в результате НЕ должен исчезать
        d = forge.TASKS_DIR / "code-c" / "agents" / "ag1"
        d.mkdir(parents=True, exist_ok=True)
        (forge.TASKS_DIR / "code-c" / "task.json").write_text(
            json.dumps({"id": "code-c", "goal": "g", "priority": "urgent", "status": "active"}),
            encoding="utf-8")
        fin = dt.datetime.now(dt.timezone.utc).isoformat()
        raw = (b'{"status":"done","role":"worker","result":"almost '
               + b'\xff' + b' good","finished":"' + fin.encode("utf-8") + b'"}')
        (d / "result.json").write_bytes(raw)
        self.assertTrue(forge.has_urgent_pending())

    def test_invitation_frames_as_fruit_not_chore(self):
        self._make("code-a", "ag1", "done", "urgent")
        text = forge.wake_invitation(forge.pending_completions(consume=False))
        self.assertIn("плод", text)
        self.assertNotIn("повинность", text.split("не задача-повинность")[1] if "не задача-повинность" in text else text[:1])


if __name__ == "__main__":
    unittest.main()


class TestMutationLockSurvivesRestart(unittest.TestCase):
    """Замок задачи форжа не должен переживать смерть своего держателя.

    26.07: `coding_session` завис и умер вместе с контейнером, оставив
    `.mutation.lock`. После рестарта новый python занял ТОТ ЖЕ номер процесса (в
    контейнере номера маленькие и переиспользуются сразу), проверка живости смотрела
    только на номер — и мертвец выглядел живым навсегда. Она честно ждала его: каждое
    пробуждение видело «busy» и назначало следующее.
    """

    def test_a_recycled_pid_is_not_the_same_process(self):
        """Ядро правки: номер совпал, рождение — нет, значит держатель другой.

        Метку рождения подменяем, чтобы проверялась ЛОГИКА, а не наличие /proc: на
        Windows его нет, а решать это должно одинаково везде.
        """
        import forge
        me = os.getpid()
        with mock.patch.object(forge, "_proc_started_at", lambda pid: "111"):
            self.assertTrue(forge._owner_alive(me, "111"), "то же рождение — тот же он")
            self.assertFalse(forge._owner_alive(me, "222"),
                             "тот же номер, другое рождение — это другой процесс")

    def test_an_unknown_birth_falls_back_to_the_old_judgement(self):
        """Где метки рождения не прочитать, поведение прежнее, а не хуже: мёртвым
        процесс объявляем только доказанно, иначе рискуем сорвать чужую транзакцию."""
        import forge
        self.assertTrue(forge._owner_alive(os.getpid(), ""))
        with mock.patch.object(forge, "_proc_started_at", lambda pid: ""):
            self.assertTrue(forge._owner_alive(os.getpid(), "неизвестно"))
        self.assertFalse(forge._owner_alive(10 ** 9, "что угодно"))

    def test_the_token_carries_the_birth_mark(self):
        import forge, tempfile, pathlib as _pl
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(forge, "_task_dir", lambda t: _pl.Path(tmp)):
                with forge._mutation_lock("t1"):
                    raw = (_pl.Path(tmp) / ".mutation.lock").read_text(encoding="utf-8")
        self.assertGreaterEqual(len(raw.split(":")), 3,
                                "в токене есть и номер, и метка рождения")

    def test_an_abandoned_old_format_lock_is_reclaimed(self):
        """Токены, написанные до этой правки, тождества не доказывают — судим по возрасту."""
        import forge, tempfile, pathlib as _pl, os as _os, time as _time
        with tempfile.TemporaryDirectory() as tmp:
            lock = _pl.Path(tmp) / ".mutation.lock"
            lock.write_text(f"{_os.getpid()}:старыйтокенбезрождения", encoding="utf-8")
            old = _time.time() - 600
            _os.utime(lock, (old, old))
            with mock.patch.object(forge, "_task_dir", lambda t: _pl.Path(tmp)):
                with forge._mutation_lock("t1", timeout=1.0):
                    pass          # раньше здесь был TimeoutError навсегда


class TestMutationLockMeasuresIdlenessNotWork(unittest.TestCase):
    """26.07, второй лок-аут: держатель ЖИВ, а работы за ним нет.

    Потолок руки отпускает ход, поток Python убить нельзя — он висит в живом процессе и
    уносит `.mutation.lock`. Проверка тождества честно отвечает «жив». Порог по возрасту
    ЗАХВАТА тут не годится: легальный finish 26.07 держал замок 1566с и завершился
    корректно. Поэтому мерится БЕЗДЕЙСТВИЕ, а держатель бьётся сам.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lock = self.tmp / ".mutation.lock"
        self._patch = mock.patch.object(forge, "_task_dir", lambda t: self.tmp)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _hold(self, age_sec, suffix="держу"):
        """Замок от ЖИВОГО процесса (нас самих), молчащий age_sec секунд."""
        me = os.getpid()
        self.lock.write_text(f"{me}:{forge._proc_started_at(me)}:{suffix}", encoding="utf-8")
        stamp = time.time() - age_sec
        os.utime(self.lock, (stamp, stamp))

    def test_silence_past_the_threshold_frees_the_task(self):
        self._hold(forge.LOCK_IDLE_ABANDONED_SEC + 30)
        with forge._mutation_lock("t1", timeout=1.0):
            pass                       # раньше здесь был вечный TimeoutError

    def test_silence_just_under_the_threshold_is_still_respected(self):
        """Граница, а не тривиальный случай: 30с недомолчал — работу не рвём."""
        self._hold(forge.LOCK_IDLE_ABANDONED_SEC - 30)
        with self.assertRaises(TimeoutError):
            with forge._mutation_lock("t1", timeout=0.3):
                pass

    def test_the_refusal_names_the_threshold_and_its_meaning(self):
        """Правило 2: молчаливых пределов нет. Она должна прочитать и срок, и смысл."""
        self._hold(10)
        with self.assertRaises(TimeoutError) as ctx:
            with forge._mutation_lock("t1", timeout=0.3):
                pass
        text = str(ctx.exception)
        self.assertIn(str(int(forge.LOCK_IDLE_ABANDONED_SEC)), text)
        self.assertIn("бездействи", text.lower())

    def test_the_holder_heartbeat_keeps_a_long_transaction_alive(self):
        """Удар из САМОГО рабочего потока: работа любой длины порога не касается."""
        with forge._mutation_lock("t1", timeout=1.0) as beat:
            stale = time.time() - (forge.LOCK_IDLE_ABANDONED_SEC + 600)
            os.utime(self.lock, (stale, stale))
            beat()
            self.assertGreater(self.lock.stat().st_mtime,
                               time.time() - 5, "удар вернул замку живость")
            beat(1200)
            self.assertGreater(self.lock.stat().st_mtime, time.time() + 600,
                               "объявленный долгий шаг ставит метку в будущее")

    def test_a_declared_long_step_is_not_stolen_from_under_the_holder(self):
        """Гейт тестов предложения — один вызов до 600с: это работа, а не тишина.
        Между «объявила лизинг» и «замолчала» разница ровно в знаке метки."""
        self._hold(-600)                 # «не жди меня раньше, чем через 600с»
        with self.assertRaises(TimeoutError):
            with forge._mutation_lock("t1", timeout=0.3):
                pass
        self._hold(forge.LOCK_IDLE_ABANDONED_SEC + 600)     # тот же срок, но молча
        with forge._mutation_lock("t1", timeout=1.0):
            pass

    def test_two_waiters_cannot_break_each_others_fresh_lock(self):
        """Безусловный unlink давал A.unlink→A.open→A′.unlink(СВЕЖИЙ замок A)→A′.open —
        оба «держат» одну задачу. Снос идёт только по тому токену, который судили."""
        self._hold(forge.LOCK_IDLE_ABANDONED_SEC + 30, suffix="покойник")
        victim = self.lock.read_text(encoding="utf-8")
        self.assertTrue(forge._break_stale_lock(self.lock, victim), "A снял брошенный")
        self.lock.write_text("999999:0:свежий-замок-A", encoding="utf-8")
        self.assertFalse(forge._break_stale_lock(self.lock, victim),
                         "A′ судил ДРУГОЙ токен — сносить нечего")
        self.assertEqual(self.lock.read_text(encoding="utf-8"), "999999:0:свежий-замок-A")
        self.assertFalse((self.tmp / ".mutation.lock.break").exists(), "гард убран за собой")

    def test_a_broken_env_value_does_not_kill_the_import(self):
        """`PRAXIS_FORGE_LOCK_IDLE_SEC=20m` в .deploy.env не имеет права уронить
        импорт forge → импорт agent → весь контейнер."""
        with mock.patch.dict(os.environ, {"PRAXIS_X_SEC": "20m"}):
            self.assertEqual(forge._env_sec("PRAXIS_X_SEC", 300.0), 300.0)
        with mock.patch.dict(os.environ, {"PRAXIS_X_SEC": "45"}):
            self.assertEqual(forge._env_sec("PRAXIS_X_SEC", 300.0), 45.0)
            self.assertEqual(forge._env_sec("PRAXIS_X_SEC", 1.0, scale=3600.0), 45 * 3600.0)

    def test_a_live_holder_of_a_declared_long_step_is_not_evicted(self):
        """P1 ревью: порог бездействия сносил ЖИВОГО держателя посреди работы.

        `coding_checkpoint`/`coding_edit` по host-задаче — ОДИН удалённый вызов, удара
        внутри него нет; serverd отвечал 14.5 минуты при норме в секунды. Пробой до
        правки: держатель работал, второй вошёл (ПЕРЕКРЫТИЕ=True) и начал вторую
        транзакцию по той же задаче."""
        holder_done = threading.Event()

        def slow(task_id, message=""):
            time.sleep(2.5)
            holder_done.set()
            return "Checkpoint x"

        with mock.patch.object(forge, "_checkpoint_unlocked", slow), \
             mock.patch.object(forge, "_remote_step_lease", lambda task: 30.0), \
             mock.patch.object(forge, "LOCK_IDLE_ABANDONED_SEC", 1.0):
            th = threading.Thread(target=lambda: forge.checkpoint("t1", "долгий шаг"))
            th.start()
            time.sleep(1.6)                  # молчит уже дольше порога, но лизинг объявлен
            with self.assertRaises(TimeoutError) as ctx:
                with forge._mutation_lock("t1", timeout=0.4):
                    self.fail("вошли под работающим держателем — это last-writer-wins")
            th.join()
        # Правило 3: работающая рука не выдаётся за замолчавшую («молчит -28с»).
        self.assertIn("объявил долгий шаг", str(ctx.exception))
        self.assertNotIn("молчит -", str(ctx.exception))
        self.assertTrue(holder_done.is_set(), "держатель доработал до конца")

    def test_but_the_lease_is_not_a_blank_cheque(self):
        """Граница с другой стороны: объявленный лизинг кончается, и брошенный замок
        снова снимается — иначе лизинг был бы просто отменой порога."""
        with mock.patch.object(forge, "_checkpoint_unlocked",
                               lambda tid, message="": (time.sleep(3), "Checkpoint x")[1]), \
             mock.patch.object(forge, "_remote_step_lease", lambda task: 0.5), \
             mock.patch.object(forge, "LOCK_IDLE_ABANDONED_SEC", 0.5):
            th = threading.Thread(target=lambda: forge.checkpoint("t1", "лизинг истёк"))
            th.start()
            time.sleep(1.6)                  # метка была на t+0.5, молчит уже 1.1с > 0.5
            with forge._mutation_lock("t1", timeout=1.0):
                pass                         # замок отдан — работы за держателем не видно
            th.join()

    def test_edit_declares_its_lease_too(self):
        """Тот же путь у правки: `workspace.edit` — один вызов, ударить внутри нечем."""
        leases = []
        with mock.patch.object(forge, "_edit_unlocked", lambda *a, **k: "ok"), \
             mock.patch.object(forge, "_remote_step_lease", lambda task: 77.0):
            real = forge._mutation_lock

            @contextlib.contextmanager
            def spy(task_id, timeout=60.0):
                with real(task_id, timeout=timeout) as beat:
                    yield lambda lease_sec=0.0: (leases.append(lease_sec), beat(lease_sec))[1]

            with mock.patch.object(forge, "_mutation_lock", spy):
                forge.edit("t1", "write", path="a.txt", content="x")
        self.assertIn(77.0, leases, f"правка не объявила свой долгий шаг: {leases}")

    def test_the_host_lease_covers_the_retry_of_the_long_tier(self):
        """Лизинг не выдуман: `serverd_client.call` на code=transport повторяет тем же
        rid — значит законное молчание доходит до двух длинных ярусов."""
        long_sec = float(getattr(forge.serverd_client, "LONG_TIMEOUT_SEC", 540.0))
        self.assertGreaterEqual(forge._remote_step_lease({"scope": "host"}), long_sec * 2)
        self.assertEqual(forge._remote_step_lease({"scope": "self"}), 240.0)
        self.assertEqual(forge._remote_step_lease(None), 240.0)


class TestLegacyLockNamesTheThresholdItActuallyApplies(unittest.TestCase):
    """Ложь, найденная верификацией 27.07: на токене СТАРОГО образца (`pid:uuid`, без
    метки рождения) замок снимался по 120с тишины, а текст отказа называл ей 300с
    (LOCK_IDLE_ABANDONED_SEC). На проде такие токены есть — это не гипотетика.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lock = self.tmp / ".mutation.lock"
        patch = mock.patch.object(forge, "_task_dir", lambda t: self.tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def _legacy(self, age_sec, body=None):
        """Замок ЖИВОГО держателя с токеном старого образца, молчащий age_sec секунд."""
        self.lock.write_text(body or f"{os.getpid()}:старыйтокенбезрождения", encoding="utf-8")
        stamp = time.time() - age_sec
        os.utime(self.lock, (stamp, stamp))

    def _refusal(self):
        with self.assertRaises(TimeoutError) as ctx:
            with forge._mutation_lock("t1", timeout=0.3):
                self.fail("вошли под держателем, которого не судили брошенным")
        return str(ctx.exception)

    def test_the_legacy_threshold_is_the_one_that_is_applied(self):
        """Граница именно легаси-порога: 120+1 снимает, 120-30 держит — и оба раза это
        НЕ общий порог (300с), иначе замок в 200с тишины не отдался бы."""
        self.assertLess(forge.LOCK_LEGACY_IDLE_SEC, forge.LOCK_IDLE_ABANDONED_SEC)
        self._legacy(forge.LOCK_LEGACY_IDLE_SEC + 1)
        with forge._mutation_lock("t1", timeout=1.0):
            pass
        self.lock.unlink(missing_ok=True)
        self._legacy(forge.LOCK_IDLE_ABANDONED_SEC - 100)   # 200с: между 120 и 300
        with forge._mutation_lock("t1", timeout=1.0):
            pass                       # применён легаси-порог, а не общий
        self.lock.unlink(missing_ok=True)
        self._legacy(forge.LOCK_LEGACY_IDLE_SEC - 30)
        self.assertIn("120", self._refusal())

    def test_the_refusal_names_the_legacy_threshold_and_not_the_other_one(self):
        """Правило 3: срок в тексте обязан быть тем, который применён к ЭТОМУ замку."""
        self._legacy(10)
        text = self._refusal()
        self.assertIn(str(int(forge.LOCK_LEGACY_IDLE_SEC)), text)
        self.assertIn("старого образца", text.lower())
        self.assertNotIn(f"Брошенным считаю после {int(forge.LOCK_IDLE_ABANDONED_SEC)}с", text)

    def test_an_unreadable_token_is_judged_by_the_same_named_number(self):
        """Нечитаемый файл замка судится тем же порогом — и он тоже назван, а не вшит."""
        self._legacy(10, body="мусор-не-токен")
        text = self._refusal()
        self.assertIn(str(int(forge.LOCK_LEGACY_IDLE_SEC)), text)
        self.lock.unlink(missing_ok=True)
        self._legacy(forge.LOCK_LEGACY_IDLE_SEC + 1, body="мусор-не-токен")
        with forge._mutation_lock("t1", timeout=1.0):
            pass

    def test_a_dead_legacy_holder_is_named_as_dead_not_as_silent(self):
        """«Держатель мёртв» и «молчит дольше порога» — разные факты; в лог шёл второй."""
        self._legacy(1, body=f"{10 ** 9}:старыйтокенбезрождения")
        with self.assertLogs("praxis-forge", level="WARNING") as logs:
            with forge._mutation_lock("t1", timeout=1.0):
                pass
        self.assertIn("держатель мёртв", "\n".join(logs.output))


class TestTheLongStepLeaseIsCountedNotGuessed(unittest.TestCase):
    """Верификация 27.07: «запас на легальном удержании — 80 секунд, а не бесконечность».

    Лизинг долгого шага обязан считаться от ФАКТИЧЕСКИХ сроков того, кто их назначает.
    Самый дорогой шаг под замком — `selfdev.submit` (гейт тестов + полтора десятка git +
    ревью иммунитета моделью): прежние `TEST_TIMEOUT + 300` = 900с вместе с порогом
    бездействия давали 1200с законной тишины при физическом потолке шага ~1500с, то есть
    замок мог быть снят из-под ЖИВОГО мёржа её собственного кода.
    """

    def test_the_submit_lease_covers_the_deadlines_submit_itself_names(self):
        import selfdev
        tests = float(getattr(selfdev, "TEST_TIMEOUT", 600))
        git = float(getattr(selfdev, "GIT_TIMEOUT", 60))
        lease = forge._submit_lease()
        self.assertGreaterEqual(lease, tests + forge._SUBMIT_GIT_CALLS * git,
                                "лизинг обязан покрывать тесты И все вызовы git внутри submit")
        self.assertGreater(lease, tests + 300, "прежнее «TEST_TIMEOUT+300» шаг не покрывало")

    def test_the_lease_moves_with_the_deadlines_it_is_made_of(self):
        """Не константа-двойник: подняли чужой срок — лизинг поднялся сам."""
        import selfdev
        with mock.patch.object(selfdev, "TEST_TIMEOUT", 1200):
            self.assertGreaterEqual(forge._submit_lease(), 1200 + forge._SUBMIT_GIT_CALLS * 60)

    def test_finish_declares_that_lease_before_the_gate(self):
        """Шов: посчитать мало — шаг обязан ОБЪЯВИТЬ этот срок перед вызовом submit."""
        import selfdev
        tmp = Path(tempfile.mkdtemp())
        orig = forge.TASKS_DIR
        forge.TASKS_DIR = tmp / "tasks"
        self.addCleanup(setattr, forge, "TASKS_DIR", orig)
        (forge.TASKS_DIR / "code-s").mkdir(parents=True, exist_ok=True)
        (forge.TASKS_DIR / "code-s" / "task.json").write_text(json.dumps({
            "id": "code-s", "goal": "цель", "root": str(tmp), "scope": "self",
            "proposal_id": "p1", "status": "active",
            "created": dt.datetime.now(dt.timezone.utc).isoformat(),
        }), encoding="utf-8")
        leases = []
        with mock.patch.object(selfdev, "submit", lambda *a, **k: "Предложение p1 отправлено"):
            forge._finish_unlocked(
                "code-s", title="t", review="мой вердикт по диффу: смотрела глазами",
                checked="тесты", submit=True, survey={"changed": [], "notes": [],
                                                      "verification_before": {}},
                beat=lambda lease_sec=0.0: leases.append(lease_sec))
        self.assertIn(forge._submit_lease(), leases,
                      f"гейт предложения не объявил свой долгий шаг: {leases}")


class TestZeroMeansOffNotOneSecond(unittest.TestCase):
    """P1 ревью: `max(1.0, ...)` в `_env_sec` превращал «выключено» в «одна секунда».

    В этом доме 0 значит ВЫКЛЮЧЕНО (`agent.py: if TOOL_CEILING_SEC <= 0`,
    `core/subagents.overdue_minutes` — «0 = выключен»). Пробой до правки:
    PRAXIS_TOOL_CEILING_SEC=0 → потолок 1с и приписка «дольше потолка руки (1с)»;
    PRAXIS_FORGE_LOCK_IDLE_SEC=0 → порог 1с, то есть самая разрушительная настройка
    вместо выключения.
    """

    def test_zero_and_negative_mean_off(self):
        for raw in ("0", "0.0", "-5"):
            with mock.patch.dict(os.environ, {"PRAXIS_X_SEC": raw}):
                self.assertEqual(forge._env_sec("PRAXIS_X_SEC", 300.0), 0.0, raw)
                self.assertEqual(forge._env_sec("PRAXIS_X_SEC", 6.0, scale=3600.0), 0.0, raw)

    def test_a_small_positive_value_is_not_silently_rewritten(self):
        """Пол в 1.0 менял названное оператором число молча — это то же молчаливое
        ограничение, только со стороны настройки."""
        with mock.patch.dict(os.environ, {"PRAXIS_X_SEC": "0.5"}):
            self.assertEqual(forge._env_sec("PRAXIS_X_SEC", 300.0), 0.5)
            self.assertEqual(forge._env_sec("PRAXIS_X_SEC", 6.0, scale=3600.0), 1800.0)

    def test_a_removed_hand_ceiling_does_not_cut_her_deadline_or_invent_one(self):
        """PRAXIS_TOOL_CEILING_SEC=0 = потолка руки нет (так и печатает serverd_client).
        Резать её срок нечем, и ссылаться в приписке не на что."""
        with mock.patch.object(forge, "_HAND_CEILING_SEC", 0.0):
            self.assertEqual(forge._remote_run_deadline(300), (300, ""))
            self.assertEqual(forge._remote_run_deadline(3600), (3600, ""))
            self.assertEqual(forge._remote_run_deadline(0), (0, ""))

    def test_the_ceiling_still_cuts_when_it_exists(self):
        """Обратная сторона: выключатель не превратился в отмену механизма."""
        with mock.patch.object(forge, "_HAND_CEILING_SEC", 600.0):
            given, note = forge._remote_run_deadline(3600)
            self.assertEqual(given, int(forge.REMOTE_RUN_DEADLINE_SEC))
            self.assertIn("600", note)

    def test_a_disabled_idle_threshold_never_evicts_a_live_holder_and_says_so(self):
        tmp = Path(tempfile.mkdtemp())
        lock = tmp / ".mutation.lock"
        me = os.getpid()
        with mock.patch.object(forge, "_task_dir", lambda t: tmp), \
             mock.patch.object(forge, "LOCK_IDLE_ABANDONED_SEC", 0.0):
            lock.write_text(f"{me}:{forge._proc_started_at(me)}:держу", encoding="utf-8")
            stamp = time.time() - 86400                    # сутки тишины
            os.utime(lock, (stamp, stamp))
            with self.assertRaises(TimeoutError) as ctx:
                with forge._mutation_lock("t1", timeout=0.3):
                    self.fail("порог выключен — живого держателя снимать нечем")
        text = str(ctx.exception)
        self.assertIn("ВЫКЛЮЧЕН", text, "правило 2: выключенный порог назван, а не молчит")
        self.assertIn("PRAXIS_FORGE_LOCK_IDLE_SEC", text)

    def test_a_disabled_reaper_is_off_not_six_times_more_eager(self):
        """PRAXIS_FORGE_TASK_ABANDONED_H=0 («жнеца не надо») давал порог 1ч вместо 6ч —
        то есть выключатель делал жнеца в шесть раз агрессивнее."""
        from core import events as core_events
        tmp = Path(tempfile.mkdtemp())
        d = tmp / "tasks" / "code-x"
        d.mkdir(parents=True, exist_ok=True)
        born = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
        (d / "task.json").write_text(json.dumps(
            {"id": "code-x", "goal": "g", "status": "active", "created": born,
             "updated": born}), encoding="utf-8")
        stamp = time.time() - 3 * 86400
        for p in (d / "task.json", d):
            os.utime(p, (stamp, stamp))
        with mock.patch.object(forge, "TASKS_DIR", tmp / "tasks"), \
             mock.patch.object(core_events, "JOURNAL", tmp / "j.jsonl"), \
             mock.patch.object(core_events, "DELIVERED", tmp / "d.json"):
            with mock.patch.object(forge, "TASK_ABANDONED_SEC", 0.0):
                self.assertEqual(forge.reconcile_lost_tasks(), 0, "жнец выключен")
            with mock.patch.object(forge, "TASK_ABANDONED_SEC", 6 * 3600.0):
                self.assertEqual(forge.reconcile_lost_tasks(), 1,
                                 "а с порогом — та же задача честно находится")

    def test_a_disabled_unit_trust_window_keeps_the_old_judgement(self):
        tmp = Path(tempfile.mkdtemp())
        d = tmp / "agents" / "agent-1"
        d.mkdir(parents=True, exist_ok=True)
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat()
        (d / "request.json").write_text(json.dumps(
            {"id": "agent-1", "supervisor_pid": os.getpid(), "created": old}), encoding="utf-8")
        with mock.patch.object(forge, "UNIT_LIVENESS_TRUST_SEC", 0.0):
            self.assertEqual(forge._unit_state(d)["status"], "running")


class TestUnitLivenessNeedsABirthMark(unittest.TestCase):
    """Та же болезнь, что чинил fb09430b в замке, — но в юнитах.

    На проде четыре юнита висели `running` без result.json с номерами 29409/71/72/111.
    В контейнере номера маленькие: первый же спавн получает такой номер, и finish
    отвечает «Задача ещё живая: agents running=1» НАВСЕГДА — задачу нельзя закрыть, а
    state_line постоянно врёт.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _unit(self, **request):
        d = self.tmp / "agents" / "agent-1"
        d.mkdir(parents=True, exist_ok=True)
        (d / "request.json").write_text(json.dumps(request), encoding="utf-8")
        return d

    def test_a_recycled_pid_no_longer_looks_alive(self):
        me = os.getpid()
        d = self._unit(id="agent-1", supervisor_pid=me, supervisor_started_at="0",
                       created=dt.datetime.now(dt.timezone.utc).isoformat())
        with mock.patch.object(forge, "_proc_started_at", lambda pid: "999"):
            self.assertEqual(forge._unit_state(d)["status"], "lost",
                             "номер занят другим процессом — это не наш супервизор")

    def test_the_same_process_is_still_running(self):
        me = os.getpid()
        d = self._unit(id="agent-1", supervisor_pid=me,
                       supervisor_started_at=forge._proc_started_at(me),
                       created=dt.datetime.now(dt.timezone.utc).isoformat())
        self.assertEqual(forge._unit_state(d)["status"], "running")

    def test_a_legacy_unit_past_the_trust_window_says_unknown_not_running(self):
        """Юниты до 27.07 метки не имеют. «running» по одному номеру было бы враньём и
        запирало бы finish; «lost» — тоже враньё. Честно: недоказуемо."""
        old = (dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(seconds=forge.UNIT_LIVENESS_TRUST_SEC + 3600)).isoformat()
        d = self._unit(id="agent-1", supervisor_pid=os.getpid(), created=old)
        state = forge._unit_state(d)
        self.assertEqual(state["status"], "unknown")
        self.assertNotIn(state["status"], forge._LIVE_UNIT_STATES, "finish больше не заперт")
        self.assertIn("метки рождения", state["liveness"])

    def test_a_fresh_legacy_unit_is_still_trusted(self):
        """Граница: внутри окна доверия поведение прежнее — живой воркер не хороним."""
        fresh = (dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(seconds=forge.UNIT_LIVENESS_TRUST_SEC - 3600)).isoformat()
        d = self._unit(id="agent-1", supervisor_pid=os.getpid(), created=fresh)
        self.assertEqual(forge._unit_state(d)["status"], "running")

    def test_a_legacy_unit_with_a_dead_pid_is_lost(self):
        d = self._unit(id="agent-1", supervisor_pid=10 ** 9,
                       created=dt.datetime.now(dt.timezone.utc).isoformat())
        self.assertEqual(forge._unit_state(d)["status"], "lost")


class TestKillTreeProvesItIsKillingItsOwnProcess(unittest.TestCase):
    """Верификация 27.07: метке рождения forge уже научен, а `killpg` шёл по ГОЛОМУ номеру.

    В контейнере номера маленькие и переиспользуются первым же спавном (26.07 /proc/10
    после рестарта занял новый python) — значит «останови мой субагент» могло снести
    группу постороннего процесса. Доказательства по порядку: живой Popen → метка
    рождения → уникальная командная строка. Ни одного нет — не убиваем вслепую и говорим,
    что доказать нечем (не запрет: в отказе названо, чем посмотреть и как снять руками).
    """

    def test_a_recycled_number_is_not_killed(self):
        with mock.patch.object(forge, "_proc_started_at", lambda pid: "новая-метка"):
            doubt = forge._kill_identity(os.getpid(), "старая-метка")
        self.assertIn("не трогаю", doubt)
        self.assertIn("посторонний", doubt)

    def test_the_same_process_is_killed_as_before(self):
        """Обратная сторона: доказали тождество — руку не отнимаем."""
        with mock.patch.object(forge, "_proc_started_at", lambda pid: "метка"):
            self.assertEqual(forge._kill_identity(os.getpid(), "метка"), "")

    def test_without_a_birth_mark_the_command_line_can_still_prove_it(self):
        """Юниты до 27.07 метки не имеют — но путь их request.json уникален."""
        with mock.patch.object(forge, "_proc_started_at", lambda pid: "какая-то"), \
             mock.patch.object(forge, "_proc_cmdline",
                               lambda pid: "python forge_worker.py --request /x/agents/a1/request.json"):
            self.assertEqual(forge._kill_identity(999, "", "/x/agents/a1/request.json"), "")
            doubt = forge._kill_identity(999, "", "/x/agents/ДРУГОЙ/request.json")
        self.assertIn("метки рождения", doubt)
        self.assertIn("/proc/999/cmdline", doubt, "сказано, чем посмотреть самой")
        self.assertIn("kill -TERM -999", doubt, "и как снять руками — это совет, не забор")

    def test_where_nothing_can_be_read_the_old_behaviour_stays(self):
        """Windows и уже умерший номер: метки не прочитать — судим как раньше, иначе
        правка превратилась бы в забор там, где доказывать нечего."""
        with mock.patch.object(forge, "_proc_started_at", lambda pid: ""):
            self.assertEqual(forge._kill_identity(999, "старая-метка"), "")
            self.assertEqual(forge._kill_identity(999, ""), "")

    def test_no_signal_is_sent_when_identity_is_unproven(self):
        """Главное: отказ обязан случиться ДО сигнала, а не после."""
        with mock.patch.object(forge, "_proc_started_at", lambda pid: "новая"), \
             mock.patch.object(forge.os, "killpg", mock.Mock(), create=True) as killpg, \
             mock.patch.object(forge.subprocess, "run", mock.Mock()) as run:
            out = forge._kill_tree(os.getpid(), "старая")
        self.assertIn("не трогаю", out)
        killpg.assert_not_called()
        run.assert_not_called()

    def test_the_escalation_window_is_covered_too(self):
        """Между TERM и KILL номер может уйти другому: наш умер, ядро отдало номер —
        и добивающий SIGKILL прилетел бы постороннему."""
        marks = iter(["моя", "чужая", "чужая"])          # умер сразу после TERM
        killpg = mock.Mock()
        with mock.patch.object(forge, "_proc_started_at", lambda pid: next(marks, "чужая")), \
             mock.patch.object(forge, "_RUNNERS", {}), \
             mock.patch.object(forge, "signal", mock.Mock(SIGTERM=15, SIGKILL=9)), \
             mock.patch.object(forge.os, "name", "posix"), \
             mock.patch.object(forge.os, "killpg", killpg, create=True):
            # os.name/signal подменены, чтобы POSIX-ветка проверялась и на Windows: на
            # проде она единственная живая, а прогон гейта идёт не там.
            forge._kill_tree(os.getpid(), "моя")
        self.assertEqual(killpg.call_count, 1, "добивать чужой процесс нельзя")

    def test_all_three_stop_paths_go_through_the_proof(self):
        """Три вызова из находки: coding_process(stop), coding_verify(stop),
        coding_agent(stop) — раньше все трое били по голому номеру."""
        tmp = Path(tempfile.mkdtemp())
        orig = forge.TASKS_DIR
        forge.TASKS_DIR = tmp / "tasks"
        self.addCleanup(setattr, forge, "TASKS_DIR", orig)
        (forge.TASKS_DIR / "code-k").mkdir(parents=True, exist_ok=True)
        (forge.TASKS_DIR / "code-k" / "task.json").write_text(json.dumps({
            "id": "code-k", "goal": "цель", "root": str(tmp), "scope": "self",
            "status": "active", "created": dt.datetime.now(dt.timezone.utc).isoformat(),
        }), encoding="utf-8")
        for kind, unit_id in (("processes", "proc-1"), ("verifications", "verify-1"),
                              ("agents", "agent-1")):
            d = forge._unit_dir("code-k", kind, unit_id)
            d.mkdir(parents=True, exist_ok=True)
            (d / "request.json").write_text(json.dumps({
                "id": unit_id, "task_id": "code-k", "command": "sleep 100",
                "supervisor_pid": os.getpid(), "supervisor_started_at": "старая-метка",
                "created": dt.datetime.now(dt.timezone.utc).isoformat(),
            }), encoding="utf-8")
        (forge._unit_dir("code-k", "processes", "proc-1") / "state.json").write_text(
            json.dumps({"status": "running", "child_pid": os.getpid(),
                        "child_started_at": "старая-метка"}), encoding="utf-8")
        with mock.patch.object(forge, "_proc_started_at", lambda pid: "новая-метка"), \
             mock.patch.object(forge, "_RUNNERS", {}), \
             mock.patch.object(forge.os, "killpg", mock.Mock(), create=True) as killpg, \
             mock.patch.object(forge.subprocess, "run", mock.Mock()) as run:
            said = [forge.process("code-k", "stop", process_id="proc-1"),
                    forge.verify("code-k", "stop", verification_id="verify-1"),
                    forge.agent("code-k", "stop", agent_id="agent-1")]
        killpg.assert_not_called()
        run.assert_not_called()
        for text in said:
            self.assertIn("не трогаю", text, f"убийство вслепую осталось здесь: {text}")


class TestFinishTellsTheTruthAndDoesNotHoldTheLock(unittest.TestCase):
    """26.07: finish держал замок вокруг ТРЁХ удалённых вызовов; impact по /tmp сжёг
    ~13 минут ядра, и всё это время задача была заперта. Плюс молчание демона читалось
    как «изменений нет» и «активных операций нет» — и задача терминализовалась вслепую.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = forge.TASKS_DIR
        forge.TASKS_DIR = self.tmp / "tasks"
        forge.TASKS_DIR.mkdir(parents=True, exist_ok=True)
        self.addCleanup(setattr, forge, "TASKS_DIR", self._orig)
        (forge.TASKS_DIR / "hcode-t").mkdir(parents=True, exist_ok=True)
        (forge.TASKS_DIR / "hcode-t" / "task.json").write_text(json.dumps({
            "id": "hcode-t", "goal": "цель", "root": "/srv/x", "scope": "host",
            "source_git": "", "base_commit": "", "status": "active", "priority": "normal",
            "created": dt.datetime.now(dt.timezone.utc).isoformat(),
        }), encoding="utf-8")

    def _fake_serverd(self, calls, ok=False):
        fake = mock.MagicMock()
        fake.call.side_effect = lambda verb, args=None, **kw: (
            calls.append(f"rpc:{verb}")
            or ({"ok": True, "operations": []} if ok
                else {"ok": False, "code": "timeout", "error": "не ответил за 180с"}))
        fake.workspace_inspect.side_effect = lambda root, action, **kw: (
            calls.append(f"rpc:inspect.{action}") or "[serverd] не ответил")
        return fake

    def test_the_survey_runs_under_the_lock_with_a_declared_lease(self):
        """Поправка ревью к первому заходу этой волны.

        Осмотр я сначала вынес НАРУЖУ замка — и получил ту же болезнь с другого конца:
        при занятом замке finish всё равно отказывает, но три RPC в демон уже оплачены,
        а 26.07 таких заходов по одной задаче было пять подряд, и демон в этот момент
        горел. Лок-аут лечится не местом осмотра, а тем, что замок больше не переживает
        ни смерть держателя, ни его молчание. Значит осмотр — под замком, но каждый его
        удалённый вызов обязан ОБЪЯВИТЬ свой срок молчания, иначе порог бездействия
        снесёт работающего.
        """
        path = forge.TASKS_DIR / "hcode-t" / "task.json"
        task = json.loads(path.read_text(encoding="utf-8"))
        task["source_git"] = "/srv/x"           # чтобы прошли все три вызова осмотра
        path.write_text(json.dumps(task), encoding="utf-8")
        calls = []

        @contextlib.contextmanager
        def _fake_lock(task_id, timeout=60.0):
            calls.append("lock")
            yield lambda lease_sec=0.0: calls.append(f"beat:{lease_sec:.0f}")

        with mock.patch.object(forge, "serverd_client", self._fake_serverd(calls)), \
             mock.patch.object(forge, "_mutation_lock", _fake_lock):
            forge.finish("hcode-t", checked="глазами", submit=False)
        rpc = [i for i, c in enumerate(calls) if c.startswith("rpc:")]
        self.assertEqual(len(rpc), 3, f"осмотр — три удалённых вызова: {calls}")
        self.assertTrue(all(i > calls.index("lock") for i in rpc),
                        f"осмотр обязан идти ПОД замком, порядок: {calls}")
        for i in rpc:
            # КАЖДЫЙ вызов осмотра объявляет свой срок молчания непосредственно перед
            # собой: иначе повисший на этом вызове поток снесут посреди работы.
            self.assertTrue(calls[i - 1].startswith("beat:") and float(calls[i - 1][5:]) > 0,
                            f"вызов {calls[i]} не объявил лизинг: {calls[max(0, i - 2):i + 1]}")

    def test_a_busy_lock_costs_the_daemon_nothing(self):
        """Пробой до правки: занятый замок → отказ, но ['inspect.impact', 'op.list',
        'inspect.diff'] уже улетели. Пять самопробуждений подряд = пятнадцать таких
        вызовов в демон, который и так лежит."""
        calls = []

        @contextlib.contextmanager
        def _busy(task_id, timeout=60.0):
            raise TimeoutError(f"task {task_id}: замок мутации занят (держатель pid 10)")
            yield

        with mock.patch.object(forge, "serverd_client", self._fake_serverd(calls)), \
             mock.patch.object(forge, "_mutation_lock", _busy):
            out = forge.finish("hcode-t", checked="глазами", submit=False)
        self.assertEqual(calls, [], f"занятый замок оплачен вызовами демона: {calls}")
        self.assertIn("замок мутации занят", out, "и она читает, почему finish не начался")

    def test_a_silent_daemon_is_not_read_as_no_operations(self):
        """Правило 3: «не смогла спросить» не имеет права выглядеть как «нечего было»."""
        calls = []
        with mock.patch.object(forge, "serverd_client", self._fake_serverd(calls)):
            out = forge.finish("hcode-t", checked="глазами", submit=False)
        self.assertIn("НЕИЗВЕСТН", out.upper())
        self.assertIn("не знала", out)
        task = json.loads((forge.TASKS_DIR / "hcode-t" / "task.json").read_text(encoding="utf-8"))
        self.assertTrue(task["finish_unknowns"], "незнание осталось в записи задачи")

    def test_impact_is_not_asked_when_the_root_has_no_git(self):
        """Именно этот вызов сжёг 26.07 12 минут: у корня нет git — ответ пуст по
        построению, платить за него минутами ядра незачем."""
        calls = []
        with mock.patch.object(forge, "serverd_client", self._fake_serverd(calls)):
            forge.finish("hcode-t", checked="глазами", submit=False)
        self.assertNotIn("rpc:inspect.impact", calls, f"impact не звался: {calls}")

    def test_impact_is_asked_when_there_is_a_git_root(self):
        """Обратная сторона: где git есть — спрашиваем, гейт не превратился в забор."""
        path = forge.TASKS_DIR / "hcode-t" / "task.json"
        task = json.loads(path.read_text(encoding="utf-8"))
        task["source_git"] = "/srv/x"
        path.write_text(json.dumps(task), encoding="utf-8")
        calls = []
        with mock.patch.object(forge, "serverd_client", self._fake_serverd(calls)):
            forge.finish("hcode-t", checked="глазами", submit=False)
        self.assertIn("rpc:inspect.impact", calls)


class TestRemoteRunDeadlineIsNamed(unittest.TestCase):
    def test_unbounded_becomes_a_named_deadline_with_a_way_out(self):
        given, note = forge._remote_run_deadline(0)
        self.assertEqual(given, int(forge.REMOTE_RUN_DEADLINE_SEC))
        self.assertIn(str(given), note)
        self.assertIn("coding_process", note, "названа честная дорога для долгого")

    def test_a_reachable_request_of_hers_is_passed_through_untouched(self):
        """Её собственный достижимый срок — её выбор; молча резать его не за что."""
        self.assertEqual(forge._remote_run_deadline(30), (30, ""))
        self.assertEqual(forge._remote_run_deadline(int(forge._HAND_CEILING_SEC)),
                         (int(forge._HAND_CEILING_SEC), ""))

    def test_a_request_above_the_hand_ceiling_is_cut_and_said_out_loud(self):
        """Час на host при потолке руки 600с = ответ, которого она не увидит никогда."""
        given, note = forge._remote_run_deadline(3600)
        self.assertEqual(given, int(forge.REMOTE_RUN_DEADLINE_SEC))
        self.assertIn("3600", note)
        self.assertIn(str(int(forge._HAND_CEILING_SEC)), note)


class TestLostTaskReaperWakesHer(unittest.TestCase):
    """26.07 18:56:19 задача hcode-c584ba6e открыта по ПРЯМОЙ просьбе Егора и брошена
    через 34 секунды; ещё четыре такие же висели с 19.07. Потерять её намерение молча
    хуже, чем выполнить поздно."""

    def setUp(self):
        from core import events as core_events
        self.core_events = core_events
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = {"tasks": forge.TASKS_DIR, "journal": core_events.JOURNAL,
                      "delivered": core_events.DELIVERED}
        forge.TASKS_DIR = self.tmp / "tasks"
        forge.TASKS_DIR.mkdir(parents=True, exist_ok=True)
        core_events.JOURNAL = self.tmp / "core_events.jsonl"
        core_events.DELIVERED = self.tmp / "core_events_delivered.json"

    def tearDown(self):
        forge.TASKS_DIR = self._orig["tasks"]
        self.core_events.JOURNAL = self._orig["journal"]
        self.core_events.DELIVERED = self._orig["delivered"]

    def _task(self, task_id, age_sec, **extra):
        d = forge.TASKS_DIR / task_id
        d.mkdir(parents=True, exist_ok=True)
        born = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_sec)).isoformat()
        row = {"id": task_id, "goal": "свежая ссылка на 30 минут", "root": "/srv/wedding",
               "status": "active", "priority": "normal", "created": born, "updated": born}
        row.update(extra)
        (d / "task.json").write_text(json.dumps(row), encoding="utf-8")
        stamp = time.time() - age_sec
        for p in list(d.rglob("*")) + [d]:
            os.utime(p, (stamp, stamp))
        return d

    def _age_out(self, d, age_sec):
        """Состарить УЖЕ существующую задачу: и запись `updated`, и mtime всех следов."""
        row = json.loads((d / "task.json").read_text(encoding="utf-8"))
        row["updated"] = (dt.datetime.now(dt.timezone.utc)
                          - dt.timedelta(seconds=age_sec)).isoformat()
        (d / "task.json").write_text(json.dumps(row), encoding="utf-8")
        stamp = time.time() - age_sec
        for p in list(d.rglob("*")) + [d]:
            os.utime(p, (stamp, stamp))

    def test_an_abandoned_task_is_labelled_and_wakes_her(self):
        self._task("hcode-c5", forge.TASK_ABANDONED_SEC + 3600)
        self.assertEqual(forge.reconcile_lost_tasks(), 1)
        task = json.loads((forge.TASKS_DIR / "hcode-c5" / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(task["status"], "lost")
        pending = self.core_events.undelivered({"subagent_result"})
        self.assertEqual(len(pending), 1, "разбудила ровно один раз")
        self.assertEqual(pending[0]["dedup_key"], "forge:hcode-c5:task:lost1")
        recap = pending[0]["payload"]["recap"]
        self.assertIn("не закрыла", recap)
        self.assertIn(str(int(forge.TASK_ABANDONED_SEC / 3600)), recap, "порог назван")
        self.assertIn("coding_session(finish)", recap, "рычаги названы")
        self.assertEqual(forge.reconcile_lost_tasks(), 0, "второй раз не будит")

    def test_a_task_that_is_merely_young_is_left_alone(self):
        """Граница: 10 минут не дотянув до порога — это не брошенная задача."""
        self._task("code-fresh", forge.TASK_ABANDONED_SEC - 600)
        self.assertEqual(forge.reconcile_lost_tasks(), 0)
        task = json.loads((forge.TASKS_DIR / "code-fresh" / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(task["status"], "active")

    def test_a_task_with_a_live_unit_is_left_alone(self):
        d = self._task("code-live", forge.TASK_ABANDONED_SEC + 3600)
        unit = d / "agents" / "agent-1"
        unit.mkdir(parents=True, exist_ok=True)
        me = os.getpid()
        (unit / "request.json").write_text(json.dumps({
            "id": "agent-1", "supervisor_pid": me,
            "supervisor_started_at": forge._proc_started_at(me),
            "created": (dt.datetime.now(dt.timezone.utc)).isoformat()}), encoding="utf-8")
        stamp = time.time() - (forge.TASK_ABANDONED_SEC + 3600)
        for p in [unit / "request.json", unit, unit.parent, d]:
            os.utime(p, (stamp, stamp))
        self.assertEqual(forge.reconcile_lost_tasks(), 0, "живой воркер — не брошенная задача")

    def test_a_task_without_a_birth_date_is_not_declared_dead(self):
        """Возраста не знаем — молчим, а не выдумываем смерть."""
        d = forge.TASKS_DIR / "code-nodate"
        d.mkdir(parents=True, exist_ok=True)
        (d / "task.json").write_text(json.dumps(
            {"id": "code-nodate", "goal": "g", "status": "active"}), encoding="utf-8")
        old = time.time() - (forge.TASK_ABANDONED_SEC + 3600)
        os.utime(d / "task.json", (old, old))
        os.utime(d, (old, old))
        self.assertEqual(forge.reconcile_lost_tasks(), 0)

    def test_the_label_falls_off_as_soon_as_she_works_again(self):
        """Ярлык не приговор: любое её действие по задаче возвращает active — иначе
        блок состояния продолжал бы врать «задач 0», пока она в задаче живёт."""
        self._task("hcode-c5", forge.TASK_ABANDONED_SEC + 3600)
        forge.reconcile_lost_tasks()
        forge._event("hcode-c5", "command", summary="снова работаю")
        task = json.loads((forge.TASKS_DIR / "hcode-c5" / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(task["status"], "active")

    def test_the_same_task_lost_a_second_time_wakes_her_again(self):
        """P1 ревью: ключ `forge:<id>:task` был один на задачу НАВСЕГДА.

        Юнит завершается ровно раз — для него это верно. Задача теряется многократно:
        потерялась → разбудила → она сделала ход (задача снова active) → бросила опять.
        Второе событие ложилось в журнал с тем же ключом, `undelivered` пропускало его
        как доставленное (ключ живёт в delivered, пока журнал не сожмётся — недели), а
        лог рапортовал «ярлык lost + пробуждение». Пробой до правки: журнал 2 строки,
        НЕДОСТАВЛЕНО = [].
        """
        d = self._task("hcode-c5", forge.TASK_ABANDONED_SEC + 3600)
        self.assertEqual(forge.reconcile_lost_tasks(), 1)
        first = self.core_events.undelivered({"subagent_result"})
        self.core_events.mark_delivered([e["dedup_key"] for e in first])   # ход прожит
        forge._event("hcode-c5", "command", summary="вернулась, один ход")  # -> active
        self._age_out(d, forge.TASK_ABANDONED_SEC + 3600)                  # и снова бросила

        self.assertEqual(forge.reconcile_lost_tasks(), 1)
        second = self.core_events.undelivered({"subagent_result"})
        self.assertEqual([e["dedup_key"] for e in second], ["forge:hcode-c5:task:lost2"],
                         "вторая потеря — своё пробуждение, а не тишина под старым ключом")
        task = json.loads((forge.TASKS_DIR / "hcode-c5" / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(task["lost_count"], 2)
        self.assertEqual(forge.reconcile_lost_tasks(), 0, "и по-прежнему не будит дважды подряд")

    def test_a_lost_task_is_visible_in_the_state_line(self):
        self._task("hcode-c5", forge.TASK_ABANDONED_SEC + 3600)
        forge.reconcile_lost_tasks()
        line = forge.state_line()
        self.assertIn("hcode-c5", line)
        self.assertIn("не закрыты", line)


class TestUpstreamEye(unittest.TestCase):
    """Глаз на ЧУЖОЙ репозиторий вместо ожидания, пока скажут словами.

    Живой случай: 26.07 она аудитировала замороженный SHA 107fca05 репозитория Арета,
    27.07 он выложил поверх ed288276 — дословно два из трёх её требований, — и она об
    этом не узнала. Её проект локальный (`git remote` пуст), на чужую работу она смотрит
    разовым клоном во временный каталог, и после `finish` не остаётся вообще ничего.
    Поэтому наблюдение берётся из ЕЁ СЛОВ (репозиторий назван в цели), спрашивает один
    HEAD и живёт дольше задачи.
    """

    LIVE_GOAL = ("Клонировать и проверить ровно frozen SHA "
                 "107fca058b77e86be685f3073501f6f0c5d39284 репозитория "
                 "https://github.com/AreteLimen/papertrade-lab. Дать два независимых "
                 "вердикта; не менять upstream, baseline или артефакты.")
    NEW_HEAD = "ed2882761a75b48ce4b728b0bf3d98c1d402ce5b"
    OLD_HEAD = "107fca058b77e86be685f3073501f6f0c5d39284"
    KEY = "github.com/aretelimen/papertrade-lab"

    def setUp(self):
        import tasks as tasks_module
        self.tasks_module = tasks_module
        self.tmp = Path(tempfile.mkdtemp())
        self._patchers = [
            mock.patch.object(forge, "TASKS_DIR", self.tmp / "tasks"),
            mock.patch.object(forge, "_UPSTREAMS", self.tmp / "upstreams.json"),
            mock.patch.object(forge, "_UPSTREAMS_RETIRED", self.tmp / "retired.json"),
            mock.patch.object(forge, "UPSTREAM_CHECK_SEC", 1800.0),
            mock.patch.object(forge, "UPSTREAM_KEEP", 12),
            mock.patch.object(forge, "UPSTREAM_BLIND_SEC", 24 * 3600.0),
        ]
        for p in self._patchers:
            p.start()
        forge.TASKS_DIR.mkdir(parents=True, exist_ok=True)
        forge._UPSTREAM_STATE["ts"] = 0.0
        self.woken = []

    def tearDown(self):
        for p in reversed(self._patchers):
            p.stop()
        forge._UPSTREAM_STATE["ts"] = 0.0

    # ── помощники ────────────────────────────────────────────────────────────
    def _task(self, task_id="hcode-f1", goal=None, status="done", updated=None):
        d = forge.TASKS_DIR / task_id
        d.mkdir(parents=True, exist_ok=True)
        stamp = updated or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        task = {"id": task_id, "goal": goal if goal is not None else self.LIVE_GOAL,
                "target": "/tmp", "scope": "host", "status": status,
                "created": stamp, "updated": stamp}
        (d / "task.json").write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        return task

    @contextlib.contextmanager
    def _remote(self, answer):
        """40 hex -> ответ remote, что угодно другое -> ошибка. Сети в тестах нет."""
        def fake(url):
            value = str(answer(url) if callable(answer) else answer)
            ok = len(value) == 40 and all(c in "0123456789abcdef" for c in value)
            return (value, "") if ok else ("", value)
        with mock.patch.object(forge, "_ls_remote_head", fake), \
             mock.patch.object(self.tasks_module, "add",
                               lambda *a, **kw: self.woken.append((a, kw))):
            yield

    def _registry(self):
        return json.loads((self.tmp / "upstreams.json").read_text(encoding="utf-8"))

    def _watch(self):
        return self._registry()[self.KEY]

    # ── тесты ────────────────────────────────────────────────────────────────
    def test_her_own_words_arm_the_watch_and_her_own_sha_is_the_baseline(self):
        """Ссылка в живой цели кончается точкой, а базлайн — frozen SHA из её же слов."""
        forge._arm_upstreams(self._task())
        row = self._watch()
        self.assertEqual(row["url"], "https://github.com/AreteLimen/papertrade-lab",
                         "хвостовая точка не часть адреса")
        self.assertEqual(row["known_head"], self.OLD_HEAD)
        self.assertIn("цель", row["known_source"])

    def test_two_repos_in_one_goal_get_no_guessed_baseline(self):
        """Чей это SHA — неизвестно. Догадку выдать за «что она видела» нельзя."""
        goal = (f"сверить https://github.com/a/one и https://github.com/b/two "
                f"на {self.OLD_HEAD}")
        forge._arm_upstreams(self._task(goal=goal))
        data = self._registry()
        self.assertEqual(sorted(data), ["github.com/a/one", "github.com/b/two"])
        self.assertEqual([r["known_head"] for r in data.values()], ["", ""])

    def test_the_live_case_reaches_her_as_a_fact_exactly_once(self):
        self._task()
        with self._remote(self.NEW_HEAD):
            self.assertEqual(forge.check_upstreams(force=True), 1)
            self.assertEqual(len(self.woken), 1)
            kind, text = self.woken[0][0][0], self.woken[0][0][1]
            self.assertEqual(kind, "wake", "её собственный будильник, а не второй")
            self.assertIn(self.OLD_HEAD[:8], text)
            self.assertIn(self.NEW_HEAD[:8], text)
            self.assertIn("papertrade-lab", text)
            self.assertIn("ФАКТ, не задача", text)
            self.assertIn("не склонировала", text, "увидела ≠ пошла проверять")
            self.assertIn("обрезано", text, "срез её же цели виден, а не молчалив")
            # второй обход по тому же HEAD молчит: сдвиг — событие, а не состояние
            self.assertEqual(forge.check_upstreams(force=True), 0)
            self.assertEqual(len(self.woken), 1)
        self.assertEqual(self._watch()["known_head"], self.NEW_HEAD)

    def test_a_wake_that_did_not_get_placed_never_swallows_the_move(self):
        """Правило 4: потерять её намерение молча хуже, чем показать позже."""
        self._task()

        def explode(*a, **kw):
            raise RuntimeError("планировщик недоступен")

        with mock.patch.object(forge, "_ls_remote_head", lambda url: (self.NEW_HEAD, "")), \
             mock.patch.object(self.tasks_module, "add", explode):
            self.assertEqual(forge.check_upstreams(force=True), 0)
        self.assertEqual(self._watch()["known_head"], self.OLD_HEAD,
                         "точка отсчёта не сдвинулась — факт ещё не отдан")
        with self._remote(self.NEW_HEAD):
            self.assertEqual(forge.check_upstreams(force=True), 1,
                             "и придёт следующим обходом")

    def test_a_first_probe_without_a_baseline_only_writes_the_starting_point(self):
        """Своё незнание нельзя выдать за её новость."""
        self._task(goal="аудит https://github.com/AreteLimen/papertrade-lab")
        with self._remote(self.NEW_HEAD):
            self.assertEqual(forge.check_upstreams(force=True), 0)
            self.assertEqual(self.woken, [])
        row = self._watch()
        self.assertEqual(row["known_head"], self.NEW_HEAD)
        self.assertIn("не твой взгляд", row["known_source"])

    def test_a_link_that_is_not_a_repository_is_dropped_and_leaves_a_trail(self):
        """26.07 в цели стоял свадебный сайт. Вечное слепое наблюдение — тоже ложь."""
        self._task(task_id="hcode-c5", goal="посмотри https://example.com/wed/site",
                   status="active")
        with self._remote("fatal: repository not found"):
            for _ in range(forge._UPSTREAM_ARM_TRIES - 1):
                forge.check_upstreams(force=True)
                self.assertIn("example.com/wed/site", self._registry(),
                              "раньше названного срока не снимаем")
            forge.check_upstreams(force=True)
            self.assertNotIn("example.com/wed/site", self._registry())
            self.assertEqual(self.woken, [], "снятие пустого наблюдения её не будит")
            trail = (forge.TASKS_DIR / "hcode-c5" / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("upstream_dropped", trail)
            forge.check_upstreams(force=True)
            self.assertNotIn("example.com/wed/site", self._registry(),
                             "обход не поднимает снятое из той же старой задачи по кругу")

    def test_naming_it_again_in_a_new_task_brings_the_watch_back(self):
        """Снятие — не запрет: её новый жест сильнее прошлой неудачи."""
        self._task(task_id="hcode-c5", goal="посмотри https://example.com/wed/site")
        with self._remote("fatal: repository not found"):
            for _ in range(forge._UPSTREAM_ARM_TRIES):
                forge.check_upstreams(force=True)
        self.assertNotIn("example.com/wed/site", self._registry())
        forge._arm_upstreams(self._task(task_id="hcode-c6",
                                        goal="снова https://example.com/wed/site"))
        self.assertIn("example.com/wed/site", self._registry())

    def test_blindness_is_told_once_and_never_as_nothing_changed(self):
        """«Не могу спросить» ≠ «ничего не менялось»."""
        self._task()
        with self._remote(self.OLD_HEAD):
            forge.check_upstreams(force=True)          # базлайн подтверждён
        with self._remote("Could not resolve host: github.com"):
            self.assertEqual(forge.check_upstreams(force=True), 0,
                             "внутри порога слепоты — молчим")
            self.assertEqual(self.woken, [])
            data = self._registry()
            aged = (dt.datetime.now(dt.timezone.utc)
                    - dt.timedelta(seconds=forge.UPSTREAM_BLIND_SEC + 60))
            data[self.KEY]["blind_since"] = aged.isoformat(timespec="seconds")
            (self.tmp / "upstreams.json").write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(forge.check_upstreams(force=True), 1,
                             "за порогом — сказать вслух")
            self.assertIn("ОСЛЕПЛА", self.woken[0][0][1])
            self.assertIn("не «ничего не менялось»", self.woken[0][0][1])
            self.assertEqual(forge.check_upstreams(force=True), 0,
                             "но не молотит одним и тем же каждый обход")
            self.assertEqual(len(self.woken), 1)
        self.assertTrue(self._watch()["blind_told"])

    def test_someone_elses_push_does_not_resurrect_a_lost_task(self):
        """`_event` снимает ярлык «потеряна из виду» — но чужой push не её ход."""
        self._task(task_id="hcode-c5", status="lost")
        with self._remote(self.NEW_HEAD):
            forge.check_upstreams(force=True)
        task = json.loads(
            (forge.TASKS_DIR / "hcode-c5" / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(task["status"], "lost")
        self.assertFalse((forge.TASKS_DIR / "hcode-c5" / "events.jsonl").exists())

    def test_the_switch_turns_it_off_instead_of_making_it_eager(self):
        """В этом доме 0 значит ВЫКЛЮЧЕНО — и об этом громко, а не молча."""
        self._task()

        def never(url):
            self.fail("наблюдение выключено — сети быть не должно")

        with mock.patch.object(forge, "UPSTREAM_CHECK_SEC", 0.0), \
             mock.patch.object(forge, "_ls_remote_head", never), \
             self.assertLogs("praxis-forge", level="INFO") as logs:
            self.assertEqual(forge.check_upstreams(force=True), 0)
        self.assertTrue(any("ВЫКЛЮЧЕНО" in line for line in logs.output))

    def test_the_pass_budget_defers_the_tail_instead_of_forgetting_it(self):
        """Двенадцать молчащих remote по 20с — это четыре минуты занятого потока.
        Бюджет обхода откладывает хвост на следующий такт, а не выкидывает его."""
        for i in range(4):
            self._task(task_id=f"t{i}", goal=f"https://github.com/o/r{i}")
        asked = []

        def slow(url):
            asked.append(url)
            time.sleep(0.05)
            return (self.NEW_HEAD, "")

        with mock.patch.object(forge, "UPSTREAM_PASS_SEC", 0.06),              mock.patch.object(forge, "_ls_remote_head", slow),              mock.patch.object(self.tasks_module, "add", lambda *a, **kw: None):
            forge.check_upstreams(force=True)
            self.assertLess(len(asked), 4, "бюджет вправду остановил обход")
            first = list(asked)
            forge.check_upstreams(force=True)
        self.assertTrue(set(asked) - set(first),
                        "следующий такт спрашивает именно неспрошенных, а не тех же")

    def test_the_rate_gate_holds_between_passes(self):
        """Граница такта: свежий обход без force в сеть второй раз не идёт."""
        self._task()
        calls = []
        with mock.patch.object(forge, "_ls_remote_head",
                               lambda url: (calls.append(url), (self.OLD_HEAD, ""))[1]):
            forge.check_upstreams(force=True)
            self.assertEqual(len(calls), 1)
            forge.check_upstreams()
            self.assertEqual(len(calls), 1, "внутри такта remote не спрашиваем")
            forge._UPSTREAM_STATE["ts"] -= forge.UPSTREAM_CHECK_SEC + 1
            forge.check_upstreams()
            self.assertEqual(len(calls), 2, "а такт прошёл — спрашиваем снова")

    def test_the_backfill_keeps_the_repos_of_the_newest_tasks(self):
        """Потолок не должен решать «кто успел в glob»: остаётся то, чем она занималась позже."""
        old = (dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(days=9)).isoformat(timespec="seconds")
        mid = (dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(days=2)).isoformat(timespec="seconds")
        self._task(task_id="t-old", goal="https://github.com/o/old", updated=old)
        self._task(task_id="t-mid", goal="https://github.com/o/mid", updated=mid)
        self._task(task_id="t-new", goal="https://github.com/o/new")
        with mock.patch.object(forge, "UPSTREAM_KEEP", 2):
            forge._arm_upstreams_from_tasks()
        self.assertEqual(sorted(self._registry()), ["github.com/o/mid", "github.com/o/new"])

    def test_a_new_task_over_the_cap_evicts_the_oldest_and_says_so(self):
        """Её свежий жест сильнее потолка — вытесняется самое давнее, и это названо вслух."""
        old = (dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(days=9)).isoformat(timespec="seconds")
        mid = (dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(days=2)).isoformat(timespec="seconds")
        with mock.patch.object(forge, "UPSTREAM_KEEP", 2):
            forge._arm_upstreams(self._task(task_id="t-old",
                                            goal="https://github.com/o/old", updated=old))
            forge._arm_upstreams(self._task(task_id="t-mid",
                                            goal="https://github.com/o/mid", updated=mid))
            self.assertEqual(len(self._registry()), 2)
            with self.assertLogs("praxis-forge", level="INFO") as logs:
                forge._arm_upstreams(self._task(task_id="t-new",
                                                goal="https://github.com/o/new"))
        self.assertEqual(sorted(self._registry()), ["github.com/o/mid", "github.com/o/new"])
        self.assertTrue(any("PRAXIS_FORGE_UPSTREAM_KEEP" in line for line in logs.output),
                        "правило 2: потолок назван, а не молчит")

    def test_credentials_in_a_link_never_reach_the_registry(self):
        """Единственный твёрдый рельс дома: креды не текут."""
        forge._arm_upstreams(self._task(
            goal="аудит https://praxis:ghp_SECRETTOKEN@github.com/AreteLimen/papertrade-lab"))
        raw = (self.tmp / "upstreams.json").read_text(encoding="utf-8")
        self.assertNotIn("ghp_SECRETTOKEN", raw)
        self.assertIn("github.com/AreteLimen/papertrade-lab", raw)

    def test_a_port_is_not_mistaken_for_the_owner(self):
        self.assertEqual(
            forge._upstream_urls("https://git.example.com:8443/team/repo.git"),
            [("https://git.example.com:8443/team/repo", "git.example.com/team/repo")])
        self.assertEqual(
            forge._upstream_urls("git@github.com:AreteLimen/papertrade-lab.git"),
            [("git@github.com:AreteLimen/papertrade-lab",
              "github.com/aretelimen/papertrade-lab")])
        self.assertEqual(forge._upstream_urls("зашла на https://example.com/страница"), [])

    def test_what_she_reads_about_the_watch_names_its_limits(self):
        """Правило 2: у предела нет права быть молчаливым."""
        task = self._task()
        forge._arm_upstreams(task)
        note = forge._upstream_note(task)
        self.assertIn("papertrade-lab", note)
        self.assertIn(f"{forge.UPSTREAM_CHECK_SEC / 60:.0f} мин", note)
        self.assertIn(str(forge.UPSTREAM_KEEP), note)
        self.assertIn("факт, не задача", note)

    def test_the_state_line_carries_only_what_she_was_not_told_yet(self):
        self._task()
        self.assertEqual(forge.upstream_line(), "", "пока нечего сказать — молчит")
        with mock.patch.object(forge, "_ls_remote_head", lambda url: (self.NEW_HEAD, "")), \
             mock.patch.object(self.tasks_module, "add", lambda *a, **kw: None):
            forge.check_upstreams(force=True)
        self.assertEqual(forge.upstream_line(), "",
                         "сказанное уходит: иначе forge был бы «активен» вечно")

    def test_an_untold_move_is_visible_in_the_state_line(self):
        """Обратная сторона: пока факт не отдан, он виден там же, где остальное про forge."""
        self._task()

        def explode(*a, **kw):
            raise RuntimeError("планировщик недоступен")

        with mock.patch.object(forge, "_ls_remote_head", lambda url: (self.NEW_HEAD, "")), \
             mock.patch.object(self.tasks_module, "add", explode):
            forge.check_upstreams(force=True)
        line = forge.upstream_line()
        self.assertIn(self.KEY, line)
        self.assertIn(self.NEW_HEAD[:8], line)
        self.assertIn(self.KEY, forge.state_line())

    def test_one_network_hiccup_does_not_camp_in_her_state_line_for_a_day(self):
        """Кадр пробуждения обещает «про слепоту говорю через 24ч», а строка состояния
        вешала «не могу спросить» ПЕРВЫМ же неудачным ответом — на целые сутки."""
        self._task()
        with self._remote(self.OLD_HEAD):          # точка отсчёта уже есть
            forge.check_upstreams(force=True)
        forge._UPSTREAM_STATE["ts"] = 0.0
        with self._remote("fatal: not a git repository"):
            forge.check_upstreams(force=True)
        self.assertTrue(self._watch()["blind_since"], "слепота записана")
        self.assertEqual(forge.upstream_line(), "",
                         "внутри порога молчим — обещание и поведение обязаны совпасть")
        # …а за порогом — говорим, тем же порогом, что и check_upstreams.
        with mock.patch.object(forge, "UPSTREAM_BLIND_SEC", 0.001):
            self.assertIn(self.KEY, forge.upstream_line())

    def test_the_watch_she_puts_by_hand_survives_a_pass_that_started_earlier(self):
        """Гонка была живой: обход держит снимок реестра десятки секунд, пока ходит в
        сеть, и записывал его целиком — её только что поставленное наблюдение исчезало."""
        self._task()
        with self._remote(self.OLD_HEAD):
            forge.check_upstreams(force=True)
        stale = forge._load_upstreams()          # снимок «до» — как у обхода в памяти
        forge.upstream_lever("watch", task_id="", arg="https://github.com/a/late")
        forge._merge_upstreams(stale, set(stale))
        self.assertIn("github.com/a/late", forge._load_upstreams(),
                      "её жест старше нашего снимка и не имеет права исчезнуть")

    def test_she_can_list_arm_and_drop_the_watch_herself(self):
        """Рычага не было ни одного: ни перечислить, ни снять — снятие случалось только
        если ссылка трижды не ответит. Будильник, который не выключить, — не глаз."""
        self._task()
        forge._arm_upstreams(self._task())
        listed = forge.upstream_lever("watching")
        self.assertIn("papertrade-lab", listed)
        self.assertIn("PRAXIS_FORGE_UPSTREAM_KEEP", listed, "потолок назван ей, а не логу")
        gone = forge.upstream_lever("unwatch", arg="https://github.com/AreteLimen/papertrade-lab")
        self.assertIn("Сняла наблюдение", gone)
        self.assertNotIn(self.KEY, forge._load_upstreams())
        # …и обход не поднимает снятое обратно из той же старой цели.
        with self._remote(self.NEW_HEAD):
            forge.check_upstreams(force=True)
        self.assertNotIn(self.KEY, forge._load_upstreams())
        self.assertEqual(self.woken, [], "снятое наблюдение не будит")
        # …но её собственный новый жест сильнее прошлого снятия.
        back = forge.upstream_lever("watch", arg="https://github.com/AreteLimen/papertrade-lab")
        self.assertIn(self.KEY, forge._load_upstreams())
        self.assertIn("HEAD", back)

    def test_a_word_that_is_not_an_address_is_named_and_not_swallowed(self):
        out = forge.upstream_lever("watch", arg="посмотри у Арета")
        self.assertIn("не вижу адреса", out)
        self.assertEqual(forge._load_upstreams(), {})

    def test_eviction_by_the_ceiling_is_told_to_her_and_not_only_to_the_log(self):
        """Закон 2 наизнанку: предел применялся и назывался оператору в контейнерном логе,
        а в задаче не оставалось НИЧЕГО — «слежу, сдвигов нет» и «больше не слежу»
        становились неотличимы."""
        told: list[tuple] = []
        with mock.patch.object(forge, "UPSTREAM_KEEP", 1), \
                mock.patch.object(forge, "_upstream_trail",
                                  side_effect=lambda *a: told.append(a)):
            forge._arm_upstreams(self._task("hcode-old", goal="https://github.com/a/one"))
            forge._arm_upstreams(self._task("hcode-new", goal="https://github.com/b/two"))
        self.assertTrue(told, "вытеснение обязано оставить след в задаче, а не только в логе")
        kinds = {row[1] for row in told}
        self.assertIn("upstream_evicted", kinds)
        self.assertIn("PRAXIS_FORGE_UPSTREAM_KEEP", " ".join(row[2] for row in told))
