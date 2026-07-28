"""Волна «перестать ей врать», участок демона: brokerops + broker.

Один вопрос ко всем тестам ниже: может ли ответ демона утверждать исход, которого не было,
и может ли «я не смог спросить» доехать до неё как «там ничего нет». Каждый тест сломан на
старом коде — проверено запуском на `git stash`.

Linux-only (hostproc: /proc, killpg, setsid) — как и весь serverd/.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import broker
import brokerops
import hostproc


class OpStopHonestyCase(unittest.TestCase):
    """`op_stop` больше не пишет «stopped» константой."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        brokerops.configure(self.base / "state")
        self.project = self.base / "project"
        self.project.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _start(self, command="sleep 30"):
        started = brokerops.op_start(str(self.project), command, timeout=120)
        self.assertTrue(started["ok"], started)
        directory = brokerops._op_dir(started["id"])
        for _ in range(200):                     # ждём, пока супервизор пропишет ребёнка
            if hostproc.poll(directory).get("status") == "running":
                break
            time.sleep(.05)
        return started["id"], directory

    def test_a_refused_stop_is_not_reported_as_stopped(self):
        operation_id, directory = self._start()
        try:
            # hostproc, который честно ОТКАЗАЛ (так он умеет после правки соседнего участка):
            # ничего не убил и надгробия не написал.
            with mock.patch.object(hostproc, "stop",
                                   return_value="не остановлен: не могу доказать, что эта "
                                                "группа моя"):
                answer = brokerops.op_stop(operation_id)
            self.assertFalse(answer["ok"], answer)
            self.assertNotEqual(answer["status"], "stopped")
            self.assertEqual(answer["status"], "running")
            self.assertIn("не могу доказать", answer["text"])
            self.assertIn("ЕЩЁ РАБОТАТЬ", answer["text"])
            self.assertIs(answer["child_alive"], True)
        finally:
            hostproc.stop(directory)

    def test_a_refusal_that_leaves_the_unit_lost_is_not_counted_as_a_stop(self):
        """hostproc умеет отказать словами «не знаю, что останавливать», и статус после этого
        бывает `lost`. `lost` — это «потеряла из виду», а не «команда завершилась»."""
        operation_id, directory = self._start()
        try:
            (directory / "state.json").write_text(json.dumps({"status": "starting"}),
                                                  encoding="utf-8")
            with mock.patch.object(hostproc, "stop",
                                   return_value="не остановлен: номер группы не опубликован"):
                answer = brokerops.op_stop(operation_id)
            self.assertEqual(answer["status"], "lost")
            self.assertFalse(answer["ok"], answer)
            self.assertIn("потеряла её из виду", answer["text"])
        finally:
            hostproc.stop(directory)

    def test_a_structured_refusal_is_taken_as_a_refusal(self):
        operation_id, directory = self._start()
        try:
            with mock.patch.object(hostproc, "stop",
                                   return_value={"ok": False, "text": "чужая группа"}):
                answer = brokerops.op_stop(operation_id)
            self.assertFalse(answer["ok"], answer)
            self.assertIs(answer["stop_verdict"], False)
            self.assertIn("чужая группа", answer["text"])
        finally:
            hostproc.stop(directory)

    def test_a_tombstone_written_over_a_living_process_is_contradicted(self):
        """Даже если result.json говорит «stopped», живой процесс с той же меткой рождения
        делает эту запись неправдой — и ответ это называет."""
        operation_id, directory = self._start()
        try:
            # Надгробие пишется ДОСЛОВНО так, как его пишет живой `hostproc.stop` (ветка
            # SIGKILL в `_kill_tree` + запись result.json). Литерал собирается из той же
            # константы, а не переписан от руки: «остановлен (SIGKILL)» здесь стоял ещё до
            # того, как текст обрёл срок внутри, — тест был зелёный на вчерашней строке.
            killed = (f"остановлен (SIGKILL: за {hostproc._secs(hostproc.TERM_GRACE_SEC)}с "
                      f"после SIGTERM группа не вышла сама)")
            # Сторож от повторного протухания: макет обязан быть тем, что живой код ещё
            # умеет написать. Формулу из константы подстановка держит, а смену самих слов —
            # только эта строка (обойти её живым вызовом нельзя: до SIGKILL доходит лишь
            # процесс, игнорирующий SIGTERM, и стоит это TERM_GRACE_SEC на каждом прогоне).
            self.assertTrue("остановлен (SIGKILL:" in inspect.getsource(hostproc._kill_tree),
                            "живой hostproc так больше не пишет — надгробие в тесте протухло")

            def tombstone_only(unit_dir):
                (Path(unit_dir) / "result.json").write_text(
                    json.dumps({"status": "stopped", "note": killed,
                                "finished": hostproc._now()}),
                    encoding="utf-8")
                return killed

            with mock.patch.object(hostproc, "stop", tombstone_only):
                answer = brokerops.op_stop(operation_id)
            self.assertFalse(answer["ok"], answer)
            self.assertIs(answer["child_alive"], True)
            self.assertIn("ЖИВ", answer["text"])
            # и само надгробие доезжает целиком — иначе литерал выше был бы декорацией
            self.assertIn(killed, answer["text"])
        finally:
            hostproc.stop(directory)

    def test_a_real_stop_is_still_a_plain_success(self):
        """И статус берётся наблюдением: убитая команда часто успевает записать СВОЙ исход
        (`failed`, exit 143), и раньше константа `"status": "stopped"` его затирала."""
        operation_id, directory = self._start()
        answer = brokerops.op_stop(operation_id)
        self.assertTrue(answer["ok"], answer)
        self.assertNotIn(answer["status"], brokerops.LIVE_STATUSES)
        self.assertIn(answer["status"], brokerops.SETTLED_STATUSES)
        self.assertIs(answer["child_alive"], False)
        self.assertIn("больше нет", answer["text"])

    def test_a_crashed_supervisor_is_an_outcome_and_not_lost_sight_of(self):
        """`error` пишет сам супервизор, когда падает сам. Это улика, а не «потеряла из виду».

        hostproc называет причину верно («супервизор этой операции сам упал — …»), а op_stop
        следом перебивал её словами «чем она кончилась, сказать нельзя»: `error` не входил в
        `SETTLED_STATUSES`. Улика об упавшем супервизоре терялась ровно в том ответе, который
        она читает. При этом «остановлена» тут тоже не утверждается: супервизор мог упасть,
        уже запустив команду, а метки рождения ребёнка в result.json нет.
        """
        operation_id, directory = self._start()
        try:
            (directory / "result.json").write_text(
                json.dumps({"status": "error",
                            "error": "OSError: [Errno 28] No space left on device"}),
                encoding="utf-8")
            answer = brokerops.op_stop(operation_id)
            self.assertEqual(answer["status"], "error")
            self.assertNotIn("потеряла её из виду", answer["text"])
            self.assertIn("супервизор", answer["text"])
            self.assertIn("No space left on device", answer["text"])   # сама улика
            self.assertIn("исход супервизора, а не самой команды", answer["text"])
            self.assertTrue(answer["ok"], answer)
        finally:
            (directory / "result.json").unlink(missing_ok=True)
            hostproc.stop(directory)

    def test_an_unprovable_stop_says_so_instead_of_asserting(self):
        """Легаси-юнит без метки рождения: остановка засчитывается, но «доказано» не пишется."""
        operation_id, directory = self._start()
        try:
            state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
            state.pop("child_starttime", None)
            (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
            answer = brokerops.op_stop(operation_id)
            self.assertIsNone(answer["child_alive"])
            self.assertIn("проверить нечем", answer["text"])
        finally:
            hostproc.stop(directory)


class RunWaitHonestyCase(unittest.TestCase):
    """`run_wait` больше не глотает текст остановки и не молчит о собственном сроке."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        brokerops.configure(self.base / "state")
        self.project = self.base / "project"
        self.project.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_wait_that_ran_out_is_named_and_the_stop_text_survives(self):
        with mock.patch.object(brokerops, "WAIT_GRACE_SEC", 1):
            answer = brokerops.run_wait(str(self.project), "sleep 30", timeout=1)
        self.assertTrue(answer["timed_out_waiting"])
        self.assertGreaterEqual(answer["waited_s"], 1.5)
        self.assertTrue(answer["stop_ok"], answer)
        note = answer["note"]
        self.assertIn("я ждала", note)
        self.assertIn("оборвала команду сама", note)
        # текст самой остановки доезжает целиком, а не теряется на break
        self.assertIn("больше нет", note)

    def test_a_stop_that_failed_warns_that_the_command_may_still_burn_the_host(self):
        with mock.patch.object(brokerops, "WAIT_GRACE_SEC", 1), \
                mock.patch.object(hostproc, "stop", return_value="не остановлен: EPERM"):
            answer = brokerops.run_wait(str(self.project), "sleep 30", timeout=1)
        try:
            self.assertTrue(answer["timed_out_waiting"])
            self.assertFalse(answer["stop_ok"], answer)
            self.assertIn("не остановлен: EPERM", answer["note"])
            self.assertIn("может продолжать работать", answer["note"])
        finally:
            hostproc.stop(brokerops._op_dir(answer["id"]))

    def test_a_command_that_finished_on_its_own_gets_no_timeout_note(self):
        with mock.patch.object(brokerops, "WAIT_GRACE_SEC", 5):
            answer = brokerops.run_wait(str(self.project), "echo hi", timeout=10)
        self.assertNotIn("timed_out_waiting", answer)
        self.assertIn("hi", answer["body"])
        self.assertEqual(answer["exit"], 0)


class OperationListHonestyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        brokerops.configure(self.base / "state")

    def tearDown(self):
        self.tmp.cleanup()

    def _unit(self, name: str, meta: str | None, status: str = "running") -> Path:
        directory = brokerops.OPERATIONS / name
        directory.mkdir(parents=True, exist_ok=True)
        if meta is not None:
            (directory / "meta.json").write_text(meta, encoding="utf-8")
        (directory / "result.json").write_text(json.dumps({"status": status}), encoding="utf-8")
        return directory

    def test_an_operation_with_an_unreadable_receipt_does_not_vanish_from_the_root_filter(self):
        self._unit("op-good", json.dumps({"root": "/srv/app", "command": "make"}))
        self._unit("op-broken", "{это не json")
        rows = brokerops.op_list("/srv/app")
        names = {row["id"] for row in rows["operations"]}
        # именно это исчезало: forge._finish_unlocked видел «активных операций нет»
        self.assertIn("op-broken", names)
        self.assertIn("op-good", names)
        broken = next(row for row in rows["operations"] if row["id"] == "op-broken")
        self.assertTrue(broken["root_unknown"])
        self.assertIn("meta.json", broken["meta_error"])
        self.assertIn("не доказано", rows["note"])

    def test_a_truncated_list_says_it_is_truncated(self):
        for index in range(5):
            self._unit(f"op-{index}", json.dumps({"root": "/srv/app", "created": f"2026-07-2{index}"}))
        rows = brokerops.op_list("", limit=2)
        self.assertEqual(len(rows["operations"]), 2)
        self.assertEqual(rows["total"], 5)
        self.assertIn("из 5", rows["note"])
        self.assertIn("НЕ полный", rows["note"])

    def test_a_root_that_did_not_resolve_is_not_answered_with_an_empty_list(self):
        self._unit("op-good", json.dumps({"root": "/srv/app"}))
        with mock.patch.object(brokerops.Path, "resolve", side_effect=OSError("ELOOP")):
            rows = brokerops.op_list("/srv/app")
        self.assertFalse(rows["ok"], rows)
        self.assertEqual(rows["code"], "root_unresolved")
        self.assertIn("ELOOP", rows["error"])

    def test_a_log_that_could_not_be_read_is_not_an_empty_output(self):
        directory = self._unit("op-nolog", json.dumps({"root": "/srv/app"}), status="done")
        (directory / "command.log").mkdir()          # чтение упрётся в IsADirectoryError
        answer = brokerops.op_poll("op-nolog")
        self.assertEqual(answer["body"], "")
        self.assertIn("IsADirectoryError", answer["log_error"])
        self.assertIn("НЕ значит", answer["note"])

    def test_an_unreadable_operations_directory_is_not_answered_as_no_operations(self):
        """Худшее молчание этого файла: каталог операций не прочитан, а ответ — «операций нет».
        Его читает `forge._finish_unlocked`, решая, можно ли закрывать задачу."""
        wall = self.base / "wall"
        wall.write_text("я файл, а не каталог", encoding="utf-8")
        with mock.patch.object(brokerops, "OPERATIONS", wall):
            rows = brokerops.op_list()
            self.assertFalse(rows["ok"], rows)
            self.assertEqual(rows["code"], "operations_unreadable")
            self.assertIn("не знаю", rows["error"])
            self.assertEqual(rows["operations"], [])
            # та же правда — в сверке при старте демона, иначе «operations: 0» в логе
            self.assertIn("listing_error", brokerops.reconcile())

    def test_an_absent_operations_directory_is_still_plainly_empty(self):
        with mock.patch.object(brokerops, "OPERATIONS", self.base / "never-created"):
            rows = brokerops.op_list()
        self.assertTrue(rows["ok"], rows)
        self.assertEqual(rows["operations"], [])
        self.assertNotIn("note", rows)

    def test_a_start_that_could_not_count_live_operations_does_not_claim_a_number(self):
        """Отказать в запуске из-за несосчитанного списка было бы забором. Назвать
        несосчитанное числом — ложью. Остаётся третье: запустить и признаться."""
        project = self.base / "project"
        project.mkdir()
        with mock.patch.object(brokerops, "_operation_rows",
                               return_value=([], "каталог операций /x не прочитан (OSError: EIO)")):
            answer = brokerops.op_start(str(project), "true", timeout=5)
        self.assertTrue(answer["ok"], answer)
        self.assertIn("НЕ ЗНАЮ", answer["note"])
        self.assertIn("EIO", answer["caps"]["listing_error"])
        self.assertNotIn(f"живых host-операций 1 из {brokerops.MAX_OPERATIONS}", answer["note"])

    def test_an_operation_that_cannot_be_looked_up_is_not_called_unknown(self):
        wall = self.base / "wall"
        wall.write_text("я файл, а не каталог", encoding="utf-8")
        with mock.patch.object(brokerops, "OPERATIONS", wall):
            polled = brokerops.op_poll("op-ghost")
            stopped = brokerops.op_stop("op-ghost")
        for answer in (polled, stopped):
            self.assertFalse(answer["ok"], answer)
            self.assertIn(answer["code"], {"operation_unreadable", "operations_unreadable"})
            self.assertIn("НЕ «такой операции нет»", answer["error"])

    def test_an_operation_that_really_never_existed_is_still_unknown(self):
        answer = brokerops.op_poll("op-nosuchthing")
        self.assertEqual(answer["code"], "unknown_operation")
        self.assertIn("unknown operation", answer["error"])

    def test_a_tail_that_cut_the_beginning_of_the_log_says_so(self):
        directory = self._unit("op-loud", json.dumps({"root": "/srv/app"}), status="done")
        (directory / "command.log").write_text("x" * 9000, encoding="utf-8")
        answer = brokerops.op_poll("op-loud", tail=brokerops.LOG_TAIL_MIN)
        self.assertEqual(len(answer["body"]), brokerops.LOG_TAIL_MIN)
        self.assertEqual(answer["body_cut_chars"], 9000 - brokerops.LOG_TAIL_MIN)
        self.assertIn("ХВОСТ лога", answer["note"])
        self.assertIn(str(9000 - brokerops.LOG_TAIL_MIN), answer["note"])

    def test_a_log_that_fits_entirely_gets_no_truncation_note(self):
        directory = self._unit("op-quiet", json.dumps({"root": "/srv/app"}), status="done")
        (directory / "command.log").write_text("y" * (brokerops.LOG_TAIL_MIN - 1), encoding="utf-8")
        answer = brokerops.op_poll("op-quiet", tail=brokerops.LOG_TAIL_MIN)
        self.assertEqual(answer["body_cut_chars"], 0)
        self.assertNotIn("ХВОСТ", str(answer.get("note") or ""))

    def test_a_reused_operation_is_not_reported_as_just_started(self):
        project = self.base / "project"
        project.mkdir()
        first = brokerops.op_start(str(project), "echo one", timeout=30,
                                   operation_id="op-samenumber")
        self.assertTrue(first["ok"], first)
        again = brokerops.op_start(str(project), "echo two", timeout=30,
                                   operation_id="op-samenumber")
        self.assertTrue(again["reused"])
        self.assertIn("СЕЙЧАС не запускала", again["note"])
        self.assertIn("echo one", again["note"])


class GitProbeHonestyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        brokerops.configure(self.base / "state")
        self.project = self.base / "project"
        self.project.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_git_that_did_not_answer_is_not_a_root_without_git(self):
        with mock.patch.object(brokerops, "_sh", return_value=(-1, "TimeoutExpired: 15s")):
            answer = brokerops.inspect(str(self.project), action="diff")
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["code"], "git_unavailable")
        self.assertIn("не то же самое", answer["error"])

    def test_a_root_really_without_git_still_answers_plainly(self):
        answer = brokerops.inspect(str(self.project), action="diff")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["text"], "No git; diff unavailable.")

    def test_checkpoint_does_not_call_a_silent_git_a_root_outside_git(self):
        with mock.patch.object(brokerops, "_sh", return_value=(-1, "TimeoutExpired: 15s")):
            answer = brokerops.checkpoint(str(self.project))
        self.assertFalse(answer["ok"], answer)
        self.assertIn("НЕ значит", answer["error"])
        self.assertNotIn("host root is not in git", answer["error"])

    def _repo(self) -> None:
        os.system(f'git init -q "{self.project}" && git -C "{self.project}" '
                  f'-c user.email=t@t -c user.name=t commit -q --allow-empty -m x')

    def _mute_status(self):
        """`git status --porcelain` не отвечает (свой таймаут), остальные вызовы git живые."""
        real_git = brokerops._git

        def muted(root, *args, **kwargs):
            if args[:2] == ("status", "--porcelain"):
                return "[git exit -1] TimeoutExpired: Command 'git status' timed out after 60s"
            return real_git(root, *args, **kwargs)
        return mock.patch.object(brokerops, "_git", muted)

    def test_checkpoint_still_saves_her_work_when_git_status_did_not_answer(self):
        """Главное на этом пути: спросить не вышло — сохранить всё равно ПОПРОБОВАЛИ.

        Здесь стоял ранний `ok:false` без единой попытки коммита: при простом таймауте git
        её работа оставалась незафиксированной, а ответ звучал как приговор корню. Спросить
        «что изменилось» и сохранить сделанное — два разных дела.
        """
        self._repo()
        (self.project / "work.txt").write_text("её работа за час\n", encoding="utf-8")
        with self._mute_status():
            answer = brokerops.checkpoint(str(self.project), "спасаю работу")
        self.assertTrue(answer["ok"], answer)
        # (1) работа ДЕЙСТВИТЕЛЬНО в git, а не «ответ звучит бодро»
        code, out = brokerops._sh(["git", "-C", str(self.project), "show", "--name-only",
                                   "--format=%s", "HEAD"])
        self.assertEqual(code, 0, out)
        self.assertIn("work.txt", out)
        self.assertIn("спасаю работу", out)
        # (2) названы ОБА исхода: и что спросить состояние не вышло, и что вышло из попытки
        text = answer["text"] + " " + str(answer.get("note") or "")
        self.assertIn("TimeoutExpired", text)
        self.assertIn("вслепую", text)
        self.assertIn(answer["sha"], answer["text"])
        self.assertIn("TimeoutExpired", answer["status_error"])

    def test_a_checkpoint_that_could_neither_ask_nor_save_names_both_and_says_what_to_do(self):
        """Второй исход той же развилки: и спросить не вышло, и сохранить не вышло.

        Ответ обязан назвать обе причины и не выглядеть как «сохранено»/«нечего сохранять».
        """
        self._repo()
        (self.project / "work.txt").write_text("её работа за час\n", encoding="utf-8")
        real_sh = brokerops._sh

        def refuse_add(argv, *args, **kwargs):
            if "add" in argv:
                return 128, "fatal: unable to write new index file"
            return real_sh(argv, *args, **kwargs)

        with self._mute_status(), mock.patch.object(brokerops, "_sh", refuse_add):
            answer = brokerops.checkpoint(str(self.project), "спасаю работу")
        self.assertFalse(answer["ok"], answer)
        self.assertIn("TimeoutExpired", answer["error"])              # первый исход
        self.assertIn("unable to write new index", answer["error"])   # второй исход
        self.assertIn("Сохранения не произошло", answer["error"])
        self.assertIn("повтори", answer["error"])                     # что делать дальше
        # и ни слова о том, чего мы не знаем: было ли вообще что сохранять
        self.assertNotIn("Работа НЕ сохранена", answer["error"])

    def test_a_failed_commit_is_not_printed_as_an_empty_error(self):
        """`{"ok": False, "text": out}` без `error` клиент печатал как «[serverd] None»:
        провалившееся сохранение выглядело пустотой (serverd_client.py:634)."""
        self._repo()
        (self.project / "work.txt").write_text("её работа\n", encoding="utf-8")
        real_sh = brokerops._sh

        def refuse_commit(argv, *args, **kwargs):
            if "commit" in argv:
                return 128, "fatal: cannot lock ref HEAD"
            return real_sh(argv, *args, **kwargs)

        with mock.patch.object(brokerops, "_sh", refuse_commit):
            answer = brokerops.checkpoint(str(self.project))
        self.assertFalse(answer["ok"], answer)
        self.assertIn("cannot lock ref HEAD", answer["error"])
        self.assertIn("Работа НЕ сохранена", answer["error"])
        # спросить-то состояние вышло — «вслепую» тут было бы неправдой в другую сторону
        self.assertNotIn("вслепую", answer["error"])
        # А вслепую то же самое утверждать уже нельзя: «сохранять было нечего» и «сохранить
        # не вышло» без git status неразличимы, и выбирать за неё одно из двух — то же враньё.
        with self._mute_status(), mock.patch.object(brokerops, "_sh", refuse_commit):
            blind = brokerops.checkpoint(str(self.project))
        self.assertFalse(blind["ok"], blind)
        self.assertIn("cannot lock ref HEAD", blind["error"])
        self.assertIn("не различаю", blind["error"])
        self.assertNotIn("Работа НЕ сохранена", blind["error"])

    def test_a_plain_checkpoint_stays_plain(self):
        """Граница: git ответил. Ни одной оговорки про слепоту, и «нечего сохранять»
        по-прежнему отвечается без коммита."""
        self._repo()
        empty = brokerops.checkpoint(str(self.project))
        self.assertTrue(empty["ok"], empty)
        self.assertEqual(empty["text"], "No changes; checkpoint not needed.")
        (self.project / "work.txt").write_text("её работа\n", encoding="utf-8")
        answer = brokerops.checkpoint(str(self.project), "обычное сохранение")
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["text"].startswith("Checkpoint "), answer["text"])
        self.assertNotIn("вслепую", answer["text"])
        self.assertEqual(answer["status_error"], "")

    def test_a_git_diff_that_failed_is_not_parsed_as_a_list_of_changed_files(self):
        """`git diff --name-only` с чужой меткой отдаёт «fatal: bad revision» — и этот текст
        уезжал дальше как ИМЯ ФАЙЛА, а ответ оставался ok:true."""
        real_sh = brokerops._sh

        def refuse(argv, *args, **kwargs):
            if "--name-only" in argv:
                return 128, "fatal: bad revision 'nosuchref'"
            return real_sh(argv, *args, **kwargs)

        os.system(f'git init -q "{self.project}" && git -C "{self.project}" '
                  f'-c user.email=t@t -c user.name=t commit -q --allow-empty -m x')
        with mock.patch.object(brokerops, "_sh", refuse):
            answer = brokerops.inspect(str(self.project), action="diff", base="nosuchref")
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["code"], "git_diff_failed")
        self.assertIn("bad revision", answer["error"])
        self.assertIn("НЕ знаю", answer["error"])

    def test_a_working_diff_is_still_a_plain_answer(self):
        os.system(f'git init -q "{self.project}" && git -C "{self.project}" '
                  f'-c user.email=t@t -c user.name=t commit -q --allow-empty -m x')
        (self.project / "new.txt").write_text("hello\n", encoding="utf-8")
        answer = brokerops.inspect(str(self.project), action="diff")
        self.assertTrue(answer["ok"], answer)
        self.assertIn("new.txt", answer["text"])
        self.assertNotIn("git exit", answer["text"])

    def test_orientation_admits_an_unreadable_top_level_instead_of_showing_it_empty(self):
        (self.project / "a.txt").write_text("x", encoding="utf-8")
        real_iterdir = brokerops.Path.iterdir

        def refuse(self):
            if self.name == "project":
                raise PermissionError("EACCES")
            return real_iterdir(self)

        with mock.patch.object(brokerops.Path, "iterdir", refuse):
            text = brokerops.inspect(str(self.project), action="status")["text"]
        self.assertIn("каталог не прочитан", text)
        self.assertIn("НЕ значит", text)


class SearchAndEditHonestyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        brokerops.configure(self.base / "state")
        self.project = self.base / "project"
        self.project.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_nothing_found_is_not_said_when_part_of_the_tree_was_never_read(self):
        (self.project / "plain.txt").write_text("ничего похожего\n", encoding="utf-8")
        (self.project / "locked.txt").write_text("иголка\n", encoding="utf-8")
        real_read_text = brokerops.Path.read_text

        def refuse(self, *args, **kwargs):
            if self.name == "locked.txt":
                raise PermissionError("EACCES")
            return real_read_text(self, *args, **kwargs)

        with mock.patch.object(brokerops.shutil, "which", return_value=None), \
                mock.patch.object(brokerops.Path, "read_text", refuse):
            answer = brokerops.inspect(str(self.project), action="search", query="иголка")
        self.assertEqual(answer["hits"], 0)
        self.assertEqual(answer["unreadable"], 1)
        self.assertNotIn("Nothing found.", answer["text"])
        self.assertIn("прочитано НЕ всё", answer["text"])
        self.assertIn("locked.txt", answer["text"])

    def test_a_directory_that_never_opened_is_not_hidden_behind_nothing_found(self):
        """`Path.glob` глотает отказ на КАТАЛОГЕ молча (`except PermissionError: return`
        в селекторах CPython) — целая ветка дерева могла не открыться, а ответ оставался
        «Nothing found.». Счётчик `unreadable` считает только файлы и этого не ловил."""
        (self.project / "plain.txt").write_text("ничего похожего\n", encoding="utf-8")
        real_walk = brokerops.os.walk

        def blind(path, onerror=None):
            if onerror:
                onerror(PermissionError(13, "Permission denied", str(Path(path) / "locked")))
            return real_walk(path)

        with mock.patch.object(brokerops.shutil, "which", return_value=None), \
                mock.patch.object(brokerops.os, "walk", blind):
            answer = brokerops.inspect(str(self.project), action="search", query="иголка")
        self.assertEqual(answer["hits"], 0)
        self.assertEqual(answer["unreadable_dirs"], 1)
        self.assertNotIn("Nothing found.", answer["text"])
        self.assertIn("каталогов не открылись", answer["text"])
        self.assertIn("locked", answer["text"])

    def test_a_custom_mask_admits_that_silent_skips_cannot_be_counted(self):
        """Свою маску по-прежнему обходит glob (повторять её семантику опаснее). Тогда честно
        то, что про пропуски я НЕ ЗНАЮ — и это оговорка, а не утверждение о неполноте."""
        (self.project / "plain.txt").write_text("ничего похожего\n", encoding="utf-8")
        with mock.patch.object(brokerops.shutil, "which", return_value=None):
            answer = brokerops.inspect(str(self.project), action="search", query="иголка",
                                       glob="**/*.py")
        self.assertIn("Nothing found.", answer["text"])     # утверждения о неполноте нет
        self.assertIn("пропускает МОЛЧА", answer["text"])
        self.assertNotIn("прочитано НЕ всё", answer["text"])

    def test_a_clean_search_still_says_nothing_found(self):
        (self.project / "plain.txt").write_text("ничего похожего\n", encoding="utf-8")
        with mock.patch.object(brokerops.shutil, "which", return_value=None):
            answer = brokerops.inspect(str(self.project), action="search", query="иголка")
        self.assertIn("Nothing found.", answer["text"])
        self.assertEqual(answer["unreadable"], 0)

    def test_an_overwritten_file_that_could_not_be_hashed_is_not_called_new(self):
        target = self.project / "conf.yml"
        target.write_text("old\n", encoding="utf-8")
        real_read_bytes = brokerops.Path.read_bytes

        def refuse(self):
            if self.name == "conf.yml":
                raise PermissionError("EACCES")
            return real_read_bytes(self)

        with mock.patch.object(brokerops.Path, "read_bytes", refuse):
            answer = brokerops.edit(str(self.project), "write", path="conf.yml", content="new\n")
        self.assertTrue(answer["ok"], answer)
        self.assertNotIn("new →", answer["text"])
        self.assertIn("не прочитан", answer["text"])
        self.assertIn("PermissionError", answer["before_error"])

    def test_a_hash_that_could_not_be_read_is_not_reported_as_a_version_conflict(self):
        target = self.project / "conf.yml"
        target.write_text("old\n", encoding="utf-8")
        real_read_bytes = brokerops.Path.read_bytes

        def refuse(self):
            if self.name == "conf.yml":
                raise PermissionError("EACCES")
            return real_read_bytes(self)

        with mock.patch.object(brokerops.Path, "read_bytes", refuse):
            answer = brokerops.edit(str(self.project), "write", path="conf.yml",
                                    content="new\n", expected_sha256="deadbeef")
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["code"], "hash_unknown")
        self.assertIn("не конфликт версий", answer["error"])
        self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

    def test_a_write_without_a_backup_says_there_is_nothing_to_roll_back_to(self):
        target = self.project / "conf.yml"
        target.write_text("old\n", encoding="utf-8")
        with mock.patch.object(brokerops.shutil, "copy2", side_effect=OSError("ENOSPC")):
            answer = brokerops.edit(str(self.project), "write", path="conf.yml", content="new\n")
        self.assertTrue(answer["ok"], answer)          # правку не отменяем — это был бы забор
        self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
        self.assertIn("ENOSPC", answer["backup_error"])
        self.assertIn("копии прежнего файла НЕТ", answer["text"])

    def test_the_list_cap_names_itself(self):
        many = self.project / "many"
        many.mkdir()
        for index in range(brokerops.LIST_ENTRY_CAP + 3):
            (many / f"f{index:04d}").write_text("x", encoding="utf-8")
        answer = brokerops.inspect(str(self.project), action="list", path="many")
        self.assertEqual(answer["entries_total"], brokerops.LIST_ENTRY_CAP + 3)
        self.assertIn(f"первые {brokerops.LIST_ENTRY_CAP} из "
                      f"{brokerops.LIST_ENTRY_CAP + 3}", answer["text"])


class BrokerReceiptHonestyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self._requests = broker.REQUESTS
        broker.REQUESTS = self.base / "requests"
        broker.REQUESTS.mkdir(parents=True)

    def tearDown(self):
        broker.REQUESTS = self._requests
        self.tmp.cleanup()

    def test_an_unreadable_receipt_does_not_mean_the_mutation_never_happened(self):
        rid = "req-broken"
        broker._request_path(rid).write_text("{оборванный", encoding="utf-8")
        answer = broker._cached(rid, "host.systemctl", "digest")
        self.assertIsNotNone(answer)                  # раньше здесь был None → повтор вслепую
        self.assertTrue(answer["in_doubt"])
        self.assertIn("НЕ ЗНАЮ", answer["error"])

    def test_a_receipt_that_is_simply_absent_still_lets_the_call_through(self):
        self.assertIsNone(broker._cached("req-fresh", "host.systemctl", "digest"))

    def test_a_completed_mutation_is_not_reported_as_failed_when_the_receipt_cannot_be_written(self):
        broker.REQUESTS = self.base / "wall"
        broker.REQUESTS.write_text("я файл, а не каталог", encoding="utf-8")
        error = broker._store_request("req-x", "host.systemctl", "digest", {"ok": True})
        self.assertTrue(error)                        # раньше это летело исключением наружу
        answer = broker._with_receipt_warning({"ok": True, "text": "restarted"}, "", error, "req-x")
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["text"], "restarted")
        self.assertIn("ВЫПОЛНЕНО", answer["note"])
        self.assertIn("второй раз", answer["note"])

    def test_a_successful_receipt_adds_no_noise(self):
        error = broker._store_request("req-ok", "host.systemctl", "digest", {"ok": True})
        self.assertEqual(error, "")
        answer = broker._with_receipt_warning({"ok": True}, "", error, "req-ok")
        self.assertNotIn("note", answer)


class BrokerAuthHonestyCase(unittest.TestCase):
    def test_a_token_file_that_cannot_be_read_is_not_a_wrong_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(broker, "TOKEN_FILE", Path(tmp) / "absent"):
                accepted, reason = broker._token_ok("whatever")
            self.assertFalse(accepted)
            self.assertNotEqual(reason, "token mismatch")
            self.assertIn("сверять не с чем", reason)

    def test_a_wrong_token_is_still_plainly_a_wrong_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token"
            path.write_text("real", encoding="utf-8")
            with mock.patch.object(broker, "TOKEN_FILE", path):
                self.assertEqual(broker._token_ok("fake"), (False, "token mismatch"))
                self.assertEqual(broker._token_ok("real"), (True, ""))

    def test_a_container_that_could_not_be_identified_is_not_cached_as_a_verdict(self):
        real_read_text = broker.Path.read_text

        def fake(self, *args, **kwargs):
            if str(self).startswith("/proc/") and self.name == "cgroup":
                return "0::/system.slice/docker-" + "a" * 40 + ".scope\n"
            return real_read_text(self, *args, **kwargs)

        broker._container_cache.clear()
        broker._container_error_cache.clear()
        with mock.patch.object(broker.Path, "read_text", fake), \
                mock.patch.object(broker.subprocess, "run",
                                  side_effect=TimeoutError("docker hung")):
            name, reason = broker._peer_container(4242)
            self.assertEqual(name, "")
            self.assertIn("docker hung", reason)
            # неудача не превращается в шестидесятисекундный приговор её контейнеру:
            # в кэше ответов её нет, а в кэше причин лежит именно ПРИЧИНА
            self.assertEqual(broker._container_cache, {})
            again = broker._peer_container(4242)
            self.assertEqual(again[0], "")
            self.assertIn("docker hung", again[1])
        broker._container_error_cache.clear()

    def test_an_unidentified_container_refusal_says_it_is_not_a_verdict(self):
        class FakeConn:
            def getsockopt(self, *_args):
                import struct
                return struct.pack("3i", 4242, 0, 0)

        real_read_text = broker.Path.read_text

        def fake(self, *args, **kwargs):
            if str(self).startswith("/proc/") and self.name == "cgroup":
                return "0::/system.slice/docker-" + "a" * 40 + ".scope\n"
            return real_read_text(self, *args, **kwargs)

        broker._container_cache.clear()
        broker._container_error_cache.clear()
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "token"
            token.write_text("t", encoding="utf-8")
            with mock.patch.object(broker, "TOKEN_FILE", token), \
                    mock.patch.object(broker, "PIN_CGROUP", True), \
                    mock.patch.object(broker.Path, "read_text", fake), \
                    mock.patch.object(broker.subprocess, "run",
                                      side_effect=TimeoutError("docker hung")):
                peer, error = broker.authorize(FakeConn(), {"token": "t"})
        self.assertIsNone(peer)
        self.assertIn("выяснить не вышло", error)
        self.assertIn("НЕ значит", error)


class BrokerDispatchHonestyCase(unittest.TestCase):
    def test_a_legacy_verb_with_an_unknown_task_says_which_thing_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(broker, "STATE", Path(tmp)):
                answer = broker.dispatch("forge.inspect", {"task": "hcode-ghost"})
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["code"], "legacy_task_unknown")
        self.assertIn("hcode-ghost", answer["error"])
        self.assertNotIn("unknown broker verb", answer["error"])

    def test_a_truly_unknown_verb_is_still_an_unknown_verb(self):
        answer = broker.dispatch("workspace.teleport", {})
        self.assertEqual(answer["code"], "unknown_verb")


class LimitsManifestCase(unittest.TestCase):
    def test_every_cap_introduced_here_is_readable_without_provoking_a_refusal(self):
        row = brokerops.limits_manifest()
        for key in ("top_entry_cap", "list_entry_cap", "operation_list_cap", "run_wait_grace_s",
                    "log_tail_cap_chars"):
            self.assertIn(key, row)
        note = row["note"]
        for number in (brokerops.TOP_ENTRY_CAP, brokerops.LIST_ENTRY_CAP,
                       brokerops.OPERATION_LIST_CAP, brokerops.WAIT_GRACE_SEC,
                       brokerops.LOG_TAIL_CAP, brokerops.LOG_TAIL_MIN):
            self.assertIn(str(number), note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
