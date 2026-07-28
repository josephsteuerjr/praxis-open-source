"""PASS 23.2 v2: one-Forge root broker, hash-chain audit and durable operations."""

from __future__ import annotations

import inspect
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import concurrent.futures
import hashlib
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import auditlog
import advisor
import broker
import brokerops
import forge_intelligence
import hostproc
import hostrecovery
import hostverbs
import migrate_v1_tasks


class AuditChainCase(unittest.TestCase):
    def test_legacy_prefix_is_anchored_and_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            path.write_text('{"legacy":1}\n{"legacy":2}\n', encoding="utf-8")
            log = auditlog.AuditLog(path)
            first = log.verify()
            self.assertTrue(first["ok"], first)
            self.assertEqual(first["legacy_lines"], 2)
            log.append({"at": "now", "kind": "rpc", "verb": "op.run", "status": "ok"})
            self.assertTrue(log.verify()["ok"])
            raw = path.read_bytes().replace(b'"legacy":1', b'"legacy":9', 1)
            path.write_bytes(raw)
            self.assertFalse(log.verify()["ok"])

    def test_export_is_content_addressed_and_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            log = auditlog.AuditLog(base / "audit.jsonl")
            log.append({"at": "now", "kind": "rpc", "verb": "admin.status", "status": "ok"})
            exported = log.export(base / "exports")
            self.assertTrue(exported["ok"], exported)
            self.assertTrue(Path(exported["path"]).is_file())
            self.assertTrue(log.verify()["ok"])


class WorkspacePrimitiveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        brokerops.configure(self.base / "state")
        self.project = self.base / "project"
        self.project.mkdir()
        (self.project / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
        (self.project / "test_app.py").write_text("import app\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_workspace_semantics_edit_and_hash_conflict(self):
        orient = brokerops.inspect(str(self.project), "orientation")
        self.assertTrue(orient["ok"], orient)
        self.assertIn("HOST ROOT", orient["text"])
        symbols = brokerops.inspect(str(self.project), "symbols", query="answer")
        self.assertIn("answer", symbols["text"])
        read = brokerops.inspect(str(self.project), "read", path="app.py")
        old_hash = read["text"].split("sha256=", 1)[1].splitlines()[0]
        edited = brokerops.edit(str(self.project), "replace", path="app.py",
                                old="return 41", new="return 42", expected_sha256=old_hash)
        self.assertTrue(edited["ok"], edited)
        stale = brokerops.edit(str(self.project), "replace", path="app.py",
                               old="return 42", new="return 43", expected_sha256=old_hash)
        self.assertFalse(stale["ok"])
        self.assertIn("hash conflict", stale["error"])

    def test_orientation_states_the_daemon_numbers_she_would_otherwise_meet_as_a_refusal(self):
        """`admin.status.limits` до неё не доезжает: `state_line()` берёт из ответа только

        protocol/operations/audit, а тула на `admin.*` у неё нет. Значит числа обязаны быть
        в ориентировке — тексте, который она читает первым и целиком.
        """
        text = brokerops.inspect(str(self.project), "orientation")["text"]
        self.assertIn("пределы демона:", text)
        self.assertIn(str(broker.WORKSPACE_SLOTS), text)
        self.assertIn(f"{broker.WORKSPACE_BUDGET_SEC:.0f}с", text)
        self.assertIn(str(brokerops.MAX_OPERATIONS), text)
        self.assertIn("PRAXIS_FORGE_SCAN_SECONDS", text)
        # Без broker (одинокий brokerops) строка всё равно есть — просто без слотов демона.
        with mock.patch.object(brokerops, "_daemon_limits", None):
            alone = brokerops.inspect(str(self.project), "orientation")["text"]
        self.assertIn("пределы демона:", alone)
        self.assertIn(str(brokerops.MAX_OPERATIONS), alone)

    def test_gitignored_and_credentials_are_ordinary(self):
        secret = self.project / "prod.env"
        secret.write_text("HUSH_TOKEN_ABC=first-value\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=self.project, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"],
                       cwd=self.project, check=True)
        subprocess.run(["git", "add", "."], cwd=self.project, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.project, check=True)
        secret.write_text("HUSH_TOKEN_ABC=second-value\n", encoding="utf-8")

        read = brokerops.inspect(str(self.project), "read", path="prod.env")["text"]
        search = brokerops.inspect(str(self.project), "search", query="HUSH_TOKEN_ABC")["text"]
        refs = brokerops.inspect(str(self.project), "references", query="HUSH_TOKEN_ABC")["text"]
        diff = brokerops.inspect(str(self.project), "diff")["text"]
        joined = "\n".join([read, search, refs, diff])
        self.assertIn("second-value", joined)
        self.assertNotIn("redacted secret diffs", diff)

    def test_only_explicit_owner_roots_are_protected(self):
        with mock.patch.object(brokerops.advisor, "PROTECTED_ROOTS",
                               (str(self.base / "private-root"),)):
            protected = self.base / "private-root"
            protected.mkdir()
            (protected / "code.py").write_text("SECRET = 1\n", encoding="utf-8")
            denied = brokerops.inspect(str(protected), "read", path="code.py")
            self.assertFalse(denied["ok"], denied)
            parent = brokerops.inspect(str(self.base), "read", path="private-root/code.py")
            self.assertFalse(parent["ok"], parent)
            command = brokerops.op_start(str(self.base), "rm -rf private-root")
            self.assertFalse(command["ok"], command)
            (self.project / "prod.env").write_text("TOKEN=visible\n", encoding="utf-8")
            normal = brokerops.inspect(str(self.project), "read", path="prod.env")
            self.assertTrue(normal["ok"], normal)

    def test_operation_has_persistent_exit_and_log(self):
        started = brokerops.op_start(str(self.project), "printf 'forty-two\\n'", timeout=20)
        self.assertTrue(started["ok"], started)
        for _ in range(100):
            polled = brokerops.op_poll(started["id"])
            if polled.get("status") not in {"starting", "running", "finishing"}:
                break
            time.sleep(.03)
        self.assertEqual(polled.get("status"), "done", polled)
        self.assertEqual(polled.get("exit"), 0)
        self.assertIn("forty-two", polled.get("body", ""))
        self.assertTrue(Path(polled["log"]).is_file())


class ImportIndexCase(unittest.TestCase):
    """Индекс модулей заменил квадратичный перебор — но обязан отвечать ровно то же."""

    @staticmethod
    def _linear(modules, dep):
        return sorted({name for name in modules
                       if name == dep or name.startswith(dep + ".") or dep.startswith(name + ".")})

    def test_index_answers_exactly_what_the_linear_scan_answered(self):
        modules = ["a", "a.b", "a.b.c", "ab", "ab.c", "b", "b.a.x", "zz", ""]
        index = forge_intelligence._ModuleIndex(modules)
        for dep in [*modules, "a.b.c.d", "ab.c.d", "x", "a.", "b.a", "aa"]:
            self.assertEqual(sorted(set(index.resolve(dep))), self._linear(modules, dep),
                             f"расхождение на импорте {dep!r}")

    def test_resolution_is_no_longer_a_scan_of_every_module_name(self):
        names = [f"pkg{n // 40}.mod{n}" for n in range(4000)]
        index = forge_intelligence._ModuleIndex(names)
        deps = names[::10]
        start = time.monotonic()
        for dep in deps:
            index.resolve(dep)
        indexed = time.monotonic() - start
        start = time.monotonic()
        for dep in deps:
            self._linear(names, dep)
        linear = time.monotonic() - start
        # 400 импортов × 4000 модулей — ровно та форма, что жгла ядро 13 минут на проде.
        self.assertGreater(linear, indexed * 5,
                           f"индекс не быстрее перебора: {indexed:.4f}s против {linear:.4f}s")


class ScanBudgetCase(unittest.TestCase):
    """Правило 2: ни одного молчаливого усечения — сколько посмотрела, столько и сказала."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        for n in range(40):
            (self.project / f"mod{n}.py").write_text(f"import mod{(n + 1) % 40}\n", encoding="utf-8")
        (self.project / "test_mod0.py").write_text("import mod0\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_impact_admits_the_file_cap_and_stays_silent_about_nothing(self):
        tight = forge_intelligence.impact(self.project, ["mod0.py"],
                                          budget=forge_intelligence.Budget(files=5))
        self.assertFalse(tight["complete"], tight["limits"])
        self.assertTrue(tight["truncated"])
        files_stage = tight["limits"]["stages"][0]
        self.assertEqual(files_stage["taken"], 5)
        self.assertGreaterEqual(files_stage["seen"], 41)
        self.assertEqual(tight["limits"]["stopped_by"], "file-cap")
        self.assertIn("ЧАСТИЧНЫЙ", tight["limits"]["note"])
        self.assertIn("5", tight["limits"]["note"])

    def test_a_budget_that_covers_the_tree_reports_a_complete_answer(self):
        # Граница: 41 файл в дереве, кап ровно 41 — упора быть не должно.
        exact = forge_intelligence.impact(self.project, ["mod0.py"],
                                          budget=forge_intelligence.Budget(files=41))
        self.assertTrue(exact["complete"], exact["limits"])
        self.assertFalse(exact["truncated"])
        self.assertEqual(exact["limits"]["stopped_by"], "")
        self.assertIn("полный", exact["limits"]["note"])
        self.assertIn("test_mod0.py", exact["tests"])
        # Один файл вниз от границы — уже частичный, и это видно.
        edge = forge_intelligence.impact(self.project, ["mod0.py"],
                                         budget=forge_intelligence.Budget(files=40))
        self.assertFalse(edge["complete"], edge["limits"])

    def test_an_expired_budget_stops_the_walk_and_names_the_deadline(self):
        budget = forge_intelligence.Budget(seconds=1, files=10000)
        budget.deadline = budget.started  # срок вышел ещё до первого файла
        spent = forge_intelligence.impact(self.project, ["mod0.py"], budget=budget)
        self.assertEqual(spent["limits"]["stopped_by"], "deadline")
        self.assertFalse(spent["complete"])
        self.assertIn("срок", spent["limits"]["note"])

    def test_the_deadline_also_stops_the_import_parse_itself(self):
        # Разбор импортов — это ~2.3 мин из 700с замера; кап по файлам его не сторожит,
        # потому что файлы к этому моменту уже перечислены.
        budget = forge_intelligence.Budget(seconds=600, files=10000)
        real_parse = forge_intelligence.ast.parse
        calls = {"n": 0}

        def parse_and_run_out_of_time(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 3:
                budget.deadline = time.monotonic() - 1
            return real_parse(*args, **kwargs)

        with mock.patch.object(forge_intelligence.ast, "parse",
                               side_effect=parse_and_run_out_of_time):
            spent = forge_intelligence.impact(self.project, ["mod0.py"], budget=budget)
        walk = spent["limits"]["stages"][0]
        self.assertTrue(walk["exhausted"], walk)      # дерево обошли целиком
        parsed = next(row for row in spent["limits"]["stages"] if "импорт" in row["stage"])
        self.assertEqual(parsed["stopped_by"], "deadline")
        self.assertEqual(parsed["taken"], 3)
        self.assertGreater(parsed["seen"], 3)
        self.assertFalse(spent["complete"])
        self.assertIn("срок", spent["limits"]["note"])

    def test_diagnostics_ok_no_longer_means_ok_everywhere(self):
        clean = forge_intelligence.diagnostics(self.project,
                                               budget=forge_intelligence.Budget(files=5))
        # `ok: true` на оборванном обходе читалось как «чисто во всём дереве»; теперь третье
        # состояние — null, и оно ложно в любом наивном `if`, в отличие от строки «unknown».
        self.assertIsNone(clean["ok"])
        self.assertFalse(clean["complete"])    # просмотрено не всё, и это сказано
        self.assertFalse(clean["scan_complete"])
        self.assertIn("НЕИЗВЕСТНО", clean["ok_means"])
        self.assertIn("ЧАСТИЧНЫЙ", clean["limits"]["note"])
        # Граница: бюджет покрывает дерево — «чисто» снова разрешено сказать.
        whole = forge_intelligence.diagnostics(self.project,
                                               budget=forge_intelligence.Budget(files=10000))
        self.assertIs(whole["ok"], True)
        self.assertTrue(whole["scan_complete"])
        self.assertIn("чисто", whole["ok_means"])

    def test_review_that_checked_nothing_does_not_call_itself_ready(self):
        """Общий кошелёк съедал impact, diagnostics не открывал ни одного файла — и снимок

        всё равно отвечал `ready_for_human_review: true` при `checked: 0`. Решают булевы поля,
        а не признание длиной в 500 символов внутри risks[].
        """
        for argv in (["git", "init", "-q"], ["git", "config", "user.name", "test"],
                     ["git", "config", "user.email", "test@example.invalid"],
                     ["git", "add", "."], ["git", "commit", "-qm", "base"]):
            subprocess.run(argv, cwd=self.project, check=True)
        (self.project / "mod0.py").write_text("import mod1\nx = 1\n", encoding="utf-8")

        starved = forge_intelligence.review_snapshot(
            self.project, budget=forge_intelligence.Budget(seconds=0.001))
        self.assertEqual(starved["changed"], ["mod0.py"])
        self.assertEqual(starved["diagnostics"]["checked"], 0)
        self.assertIsNone(starved["diagnostics"]["ok"])
        self.assertIsNone(starved["ready_for_human_review"])
        self.assertIn("НЕИЗВЕСТНО", starved["ready_for_human_review_means"])
        self.assertFalse(starved["limits"]["complete"])
        self.assertTrue(any("диагностика синтаксиса не отработала" in row
                            for row in starved["risks"]), starved["risks"])

        # Обратная граница: полный бюджет — оба поля утверждают ровно то, что и раньше.
        full = forge_intelligence.review_snapshot(
            self.project, budget=forge_intelligence.Budget(seconds=120, files=10000))
        self.assertTrue(full["limits"]["complete"], full["limits"])
        self.assertEqual(full["diagnostics"]["checked"], 1)
        self.assertIs(full["diagnostics"]["ok"], True)
        self.assertIs(full["ready_for_human_review"], True)
        self.assertNotIn("НЕИЗВЕСТНО", full["ready_for_human_review_means"])
        self.assertFalse(any("диагностика синтаксиса не отработала" in row
                             for row in full["risks"]), full["risks"])

    def test_broker_answer_carries_the_budget_she_never_asked_about(self):
        with mock.patch.object(forge_intelligence, "SCAN_FILES", 5):
            answer = brokerops.inspect(str(self.project), "impact")
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["truncated"])
        self.assertIn("ЧАСТИЧНЫЙ", answer["note"])
        # И то же самое внутри text — клиент отдаёт ей именно его.
        payload = json.loads(answer["text"])
        self.assertFalse(payload["complete"])
        self.assertIn("ЧАСТИЧНЫЙ", payload["limits"]["note"])

    def test_python_search_without_rg_has_a_budget_and_says_when_it_stopped(self):
        with mock.patch.object(brokerops.shutil, "which", return_value=None), \
             mock.patch.object(brokerops, "SEARCH_FILE_CAP", 4):
            stopped = brokerops.inspect(str(self.project), "search", query="import")
        self.assertTrue(stopped["ok"], stopped)
        self.assertTrue(stopped["truncated"], stopped.get("limits"))
        self.assertIn("ЧАСТИЧНЫЙ", stopped["text"])
        with mock.patch.object(brokerops.shutil, "which", return_value=None):
            whole = brokerops.inspect(str(self.project), "search", query="import")
        self.assertTrue(whole["complete"], whole.get("limits"))
        self.assertNotIn("ЧАСТИЧНЫЙ", whole["text"])

    def test_search_names_its_own_cap_and_not_a_lever_that_does_not_move_it(self):
        """Обход рвал SEARCH_FILE_CAP, а объяснял усечение `Budget.note()`: он называл чужое

        число (бюджет обхода) и советовал PRAXIS_FORGE_SCAN_FILES, который на search не влияет
        вовсе. Предел назван неверно + рычаг предложен мёртвый. Демон обязан говорить о СВОЁМ
        пределе правду и либо советовать работающее, либо признать, что рычага нет.
        """
        with mock.patch.object(brokerops.shutil, "which", return_value=None), \
             mock.patch.object(brokerops, "SEARCH_FILE_CAP", 7):
            stopped = brokerops.inspect(str(self.project), "search", query="import")
        self.assertFalse(stopped["complete"], stopped.get("limits"))
        for place in (stopped["text"], stopped["note"]):
            self.assertIn("прочитано 7 файлов", place)      # СВОЁ число, не 12000 обхода
            self.assertIn("SEARCH_FILE_CAP", place)
            self.assertIn("Env-рычага у него НЕТ", place)
            # Чужой совет, который тут не работает, снимается прямым текстом, а не молчанием:
            # он всё ещё приезжает в приписке бюджета, и противоречие обязано быть названо.
            self.assertIn("к search НЕ относится", place)
        # Кап находок — тот же класс: число из константы, а не из литерала в цикле.
        with mock.patch.object(brokerops.shutil, "which", return_value=None), \
             mock.patch.object(brokerops, "SEARCH_HIT_CAP", 2):
            flooded = brokerops.inspect(str(self.project), "search", query="import")
        self.assertEqual(flooded["hits"], 2)
        self.assertIn("набрано 2 совпадений", flooded["text"])
        self.assertIn("SEARCH_HIT_CAP", flooded["note"])
        # Граница: обход уложился в оба капа — приписки про них нет вовсе.
        with mock.patch.object(brokerops.shutil, "which", return_value=None):
            whole = brokerops.inspect(str(self.project), "search", query="import")
        self.assertTrue(whole["complete"], whole.get("limits"))
        self.assertNotIn("SEARCH_FILE_CAP", whole["text"])
        self.assertNotIn("SEARCH_HIT_CAP", whole["text"])

    def test_the_manifest_states_the_search_caps_and_that_they_have_no_lever(self):
        """Оба числа были только ключами словаря: в тексте, который она читает, их не было,
        а рядом стоял совет про PRAXIS_FORGE_SCAN_FILES — читалось как рычаг и на search."""
        note = brokerops.limits_manifest()["note"]
        self.assertIn(str(brokerops.SEARCH_FILE_CAP), note)
        self.assertIn(str(brokerops.SEARCH_HIT_CAP), note)
        self.assertIn("env-рычага у них нет", note)
        self.assertEqual(brokerops.limits_manifest()["search_hit_cap"], brokerops.SEARCH_HIT_CAP)

    def test_text_cap_names_itself_instead_of_a_bare_cut_marker(self):
        capped = brokerops._cap("x" * 500, 100)
        self.assertIn("кап ответа 100", capped)
        self.assertIn("из 500", capped)
        self.assertEqual(brokerops._cap("short", 100), "short")


class WorkspaceConcurrencyCase(unittest.TestCase):
    """Поток на соединение без счётчика = три обхода на 4 ядрах и час голодания."""

    def tearDown(self):
        for key in list(broker._workspace_active):
            broker.workspace_release(key)

    def test_only_heavy_reads_take_a_slot(self):
        self.assertTrue(broker._heavy("workspace.inspect", {"action": "impact"}))
        self.assertFalse(broker._heavy("workspace.inspect", {"action": "read"}))
        self.assertFalse(broker._heavy("workspace.inspect", {}))
        self.assertFalse(broker._heavy("op.poll", {"action": "impact"}))
        # Дешёвое чтение слот не занимает и потому не может встать в очередь за обходом.
        deadline, busy, slot = broker.workspace_slot(
            "cheap", "workspace.inspect", {"action": "read"})
        self.assertIsNone(deadline)
        self.assertIsNone(busy)
        self.assertEqual(slot, "")

    def test_saving_her_work_never_queues_behind_someone_elses_tree_walk(self):
        """Единственный новый отказ волны стоял на пути СОХРАНЕНИЯ (`coding_checkpoint`).

        checkpoint дерево не обходит (git status/add/commit по индексу), а слот отнимал — и
        отнимал молча: в `limits()["heavy_actions"]` его не было. Проверяем оба следствия
        и то, что при полностью занятых слотах сохранение всё равно проходит без ожидания.
        """
        self.assertFalse(broker._heavy("workspace.checkpoint", {}))
        self.assertNotIn("checkpoint", " ".join(broker.limits()["heavy_actions"]))
        drained = threading.BoundedSemaphore(1)
        drained.acquire()                       # ни одного свободного разрешения
        with mock.patch.object(broker, "WORKSPACE_SLOTS", 1), \
             mock.patch.object(broker, "_workspace_slots", drained), \
             mock.patch.object(broker, "WORKSPACE_WAIT_SEC", 30):
            started = time.monotonic()
            deadline, busy, slot = broker.workspace_slot(
                "save", "workspace.checkpoint", {"root": "/opt/praxis"})
        self.assertIsNone(busy, "сохранение получило отказ из-за чужого обхода")
        self.assertIsNone(deadline)
        self.assertEqual(slot, "")
        self.assertLess(time.monotonic() - started, 5, "сохранение стояло в очереди за чтением")
        # И тяжёлое чтение при тех же условиях по-прежнему ждёт и отказывает — забор снят
        # ровно на записи, а не вообще.
        with mock.patch.object(broker, "WORKSPACE_SLOTS", 1), \
             mock.patch.object(broker, "_workspace_slots", drained), \
             mock.patch.object(broker, "WORKSPACE_WAIT_SEC", 0.05):
            _, refused, _ = broker.workspace_slot(
                "read", "workspace.inspect", {"action": "impact", "root": "/opt"})
        self.assertIsNotNone(refused)
        self.assertEqual(refused["code"], "busy")

    def test_limits_say_out_loud_that_checkpoint_does_not_queue(self):
        """Закон 2 наоборот: снятое ограничение тоже обязано быть названным, иначе она будет
        по-прежнему бояться, что сохранение упрётся в чужой обход."""
        self.assertIn("workspace.checkpoint", broker.limits()["note"])
        self.assertIn("слот НЕ занимает", broker.limits()["note"])

    def test_busy_answer_names_the_number_and_what_is_running(self):
        with mock.patch.object(broker, "WORKSPACE_SLOTS", 1), \
             mock.patch.object(broker, "_workspace_slots", threading.BoundedSemaphore(1)), \
             mock.patch.object(broker, "WORKSPACE_WAIT_SEC", 0.05):
            first, busy, one = broker.workspace_slot(
                "one", "workspace.inspect", {"action": "overview", "root": "/tmp/deep"})
            self.assertIsNone(busy)
            self.assertGreater(first, time.monotonic())      # срок счёта приехал в dispatch
            _, refused, empty = broker.workspace_slot(
                "two", "workspace.inspect", {"action": "overview", "root": "/tmp/other"})
            self.assertIsNotNone(refused)
            self.assertEqual(empty, "")
            self.assertEqual(refused["code"], "busy")
            self.assertIn("/tmp/deep", refused["error"])
            self.assertIn("Ничего не запускалось", refused["error"])
            self.assertFalse(refused["slots_unaccounted"])
            self.assertEqual(refused["active"][0]["action"], "overview")
            self.assertEqual(refused["active"][0]["request_id"], "one")
            broker.workspace_release(one)
            # Слот вернулся — следующая та же просьба проходит.
            third, free, three = broker.workspace_slot(
                "three", "workspace.inspect", {"action": "overview", "root": "/tmp/other"})
            self.assertIsNone(free)
            self.assertIsNotNone(third)
            broker.workspace_release(three)

    def test_release_of_a_slot_that_was_never_taken_does_not_leak_permits(self):
        with mock.patch.object(broker, "_workspace_slots", threading.BoundedSemaphore(1)):
            broker.workspace_release("never-acquired")
            broker.workspace_release("")
            deadline, busy, slot = broker.workspace_slot(
                "real", "workspace.inspect", {"action": "impact"})
            self.assertIsNone(busy)
            self.assertIsNotNone(deadline)
            broker.workspace_release(slot)

    def test_a_permit_taken_but_never_registered_is_given_back(self):
        """Окно между `acquire()` и записью в `_workspace_active`: до этой правки сбой в нём
        уносил разрешение молча — до `finally` в handle такой слот не доживает вовсе."""
        with mock.patch.object(broker, "WORKSPACE_SLOTS", 2), \
             mock.patch.object(broker, "_workspace_slots", threading.BoundedSemaphore(2)), \
             mock.patch.object(broker, "WORKSPACE_WAIT_SEC", 0.05):
            with mock.patch.object(broker.uuid, "uuid4", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    broker.workspace_slot("doomed", "workspace.inspect", {"action": "impact"})
            self.assertEqual(broker._workspace_active, {})
            taken = []
            for _ in range(2):                   # оба разрешения по-прежнему на месте
                _, busy, slot = broker.workspace_slot(
                    "after", "workspace.inspect", {"action": "impact"})
                self.assertIsNone(busy, "разрешение утекло в окне до регистрации")
                taken.append(slot)
            for slot in taken:
                broker.workspace_release(slot)

    def test_a_partly_leaked_semaphore_is_not_reported_as_healthy(self):
        """Инцидент 27.07 шёл ступенями 3 → 2 → 1. Признак стоял только на ПОЛНОЙ утечке,

        поэтому на первых двух ступенях ответ говорил `slots_unaccounted: false` при уже
        потерянном слоте — врал ровно тогда, когда починка нужна больше всего.
        """
        drained = threading.BoundedSemaphore(3)
        for _ in range(3):
            drained.acquire()
        with mock.patch.object(broker, "WORKSPACE_SLOTS", 3), \
             mock.patch.object(broker, "_workspace_slots", drained), \
             mock.patch.object(broker, "_workspace_active",
                               {"live:1": {"request_id": "live", "verb": "workspace.inspect",
                                           "action": "impact", "root": "/opt",
                                           "started": time.monotonic()}}), \
             mock.patch.object(broker, "WORKSPACE_WAIT_SEC", 0.05):
            _, refused, _ = broker.workspace_slot(
                "next", "workspace.inspect", {"action": "impact", "root": "/tmp"})
        self.assertIsNotNone(refused)
        self.assertTrue(refused["slots_unaccounted"], refused["error"])
        self.assertEqual(refused["slots_unaccounted_count"], 2)
        self.assertIn("/opt", refused["error"])              # живой обход всё равно назван
        self.assertIn("остальные 2 заняты без обхода", refused["error"])
        self.assertIn("перезапуска praxis-serverd", refused["error"])
        # Обратная граница: слоты сходятся с явью — обвинять счётчик не в чем.
        healthy = threading.BoundedSemaphore(1)
        healthy.acquire()
        with mock.patch.object(broker, "WORKSPACE_SLOTS", 1), \
             mock.patch.object(broker, "_workspace_slots", healthy), \
             mock.patch.object(broker, "_workspace_active",
                               {"live:1": {"request_id": "live", "verb": "workspace.inspect",
                                           "action": "impact", "root": "/opt",
                                           "started": time.monotonic()}}), \
             mock.patch.object(broker, "WORKSPACE_WAIT_SEC", 0.05):
            _, fair, _ = broker.workspace_slot(
                "next", "workspace.inspect", {"action": "impact", "root": "/tmp"})
        self.assertFalse(fair["slots_unaccounted"], fair["error"])
        self.assertEqual(fair["slots_unaccounted_count"], 0)
        self.assertNotIn("без обхода", fair["error"])

    def test_two_calls_under_one_request_id_do_not_burn_a_slot_forever(self):
        """`workspace.inspect` не в MUTATING — два хода с ОДНИМ номером идут параллельно.

        А такие ходы клиент делает штатно: `serverd_client.py:394` повторяет transport/eof тем
        же номером, `_take_in_doubt` переиспользует его после её таймаута. С ключом занятости
        по номеру второй захват затирал первый, второй release не отдавал ничего — репро в
        linux-образе давало 3 → 2 → 1 свободных слота и вечное «занято» при пустом списке.
        """
        slots = 3
        with mock.patch.object(broker, "WORKSPACE_SLOTS", slots), \
             mock.patch.object(broker, "_workspace_slots", threading.BoundedSemaphore(slots)), \
             mock.patch.object(broker, "WORKSPACE_WAIT_SEC", 0.05):
            args = {"action": "impact", "root": "/tmp"}
            rid = "one-and-the-same"
            for _ in range(4):                       # четыре потерянных ответа подряд
                _, first_busy, first = broker.workspace_slot(rid, "workspace.inspect", args)
                _, second_busy, second = broker.workspace_slot(rid, "workspace.inspect", args)
                self.assertIsNone(first_busy)
                self.assertIsNone(second_busy)
                self.assertNotEqual(first, second)
                broker.workspace_release(first)
                broker.workspace_release(second)
            self.assertEqual(broker._workspace_active, {})
            taken = []
            for _ in range(slots):
                deadline, busy, slot = broker.workspace_slot(
                    "after", "workspace.inspect", args)
                self.assertIsNone(busy, "разрешение утекло: слотов стало меньше объявленных")
                taken.append(slot)
            for slot in taken:
                broker.workspace_release(slot)

    def test_busy_admits_when_not_a_single_walk_is_accounted_for(self):
        """«Занято… Сейчас считают: —» — это отказ и признание, что отказывать не за что."""
        empty = threading.BoundedSemaphore(2)
        empty.acquire()
        empty.acquire()
        with mock.patch.object(broker, "WORKSPACE_SLOTS", 2), \
             mock.patch.object(broker, "_workspace_slots", empty), \
             mock.patch.object(broker, "WORKSPACE_WAIT_SEC", 0.05):
            _, refused, _ = broker.workspace_slot(
                "orphan", "workspace.inspect", {"action": "impact", "root": "/tmp"})
        self.assertIsNotNone(refused)
        self.assertEqual(refused["active"], [])
        self.assertTrue(refused["slots_unaccounted"])
        self.assertIn("НИ ОДНОГО живого обхода", refused["error"])
        self.assertIn("перезапуска praxis-serverd", refused["error"])
        self.assertNotIn("Сейчас считают: —", refused["error"])

    def test_daemon_limits_are_readable_without_provoking_a_refusal(self):
        row = broker.limits()
        self.assertEqual(row["max_operations"], brokerops.MAX_OPERATIONS)
        self.assertEqual(row["disk_floor_mib"], 512)
        self.assertEqual(row["text_cap_chars"], brokerops.TEXT_CAP)
        self.assertEqual(row["workspace_slots"], broker.WORKSPACE_SLOTS)
        self.assertIn("impact", row["heavy_actions"])
        self.assertIn(str(brokerops.MAX_OPERATIONS), row["note"])


class LoudRecoveryCase(unittest.TestCase):
    """Механизм отката остаётся; молчание — нет."""

    def test_armed_rollback_is_stated_in_the_text_she_actually_reads(self):
        show = "ActiveState=active\nSubState=running\nUnitFileState=enabled\nLoadState=loaded\n"
        with tempfile.TemporaryDirectory() as tmp:
            hostrecovery.configure(Path(tmp))
            with mock.patch.object(hostverbs, "_sh", side_effect=[(0, show), (0, "ok"), (0, show)]), \
                 mock.patch.object(hostrecovery.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 0, "", "")):
                result = hostverbs.systemctl("restart", "ssh", recover_after=90)
        self.assertTrue(result["ok"], result)
        self.assertIn("ОТКАЧУ ЭТО ЧЕРЕЗ 90с", result["text"])
        self.assertIn(result["recovery"]["id"], result["text"])
        self.assertIn("verb=confirm", result["text"])
        self.assertIn("советник:", result["text"])       # совет тоже доезжает, не только в поле
        self.assertTrue(result["text"].startswith("⏱"))  # и не срезается капом хвоста

    def test_failed_arming_says_that_there_is_no_rollback_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            hostrecovery.configure(Path(tmp))
            with mock.patch.object(hostrecovery.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 1, "", "no systemd")):
                receipt = hostrecovery.arm("file", "cp a b", delay=120)
        self.assertEqual(receipt["status"], "arm_failed")
        self.assertIn("НЕ взведён", receipt["notice"])
        self.assertNotIn("ОТКАЧУ", receipt["notice"])

    def test_a_failed_edit_still_announces_the_armed_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            hostrecovery.configure(Path(tmp))
            target = Path("/etc/ssh/praxis-test-does-not-exist.conf")
            with mock.patch.object(hostverbs, "_critical_path", return_value=True), \
                 mock.patch.object(hostrecovery, "arm", return_value={
                     "id": "recover-x", "status": "armed", "delay_s": 120,
                     "deadline_epoch": int(time.time()) + 120, "rollback_command": "rm -f x"}), \
                 mock.patch.object(hostverbs.os, "replace", side_effect=OSError("read-only fs")):
                failed = hostverbs.file("write", str(target), content="x")
        self.assertFalse(failed["ok"], failed)
        self.assertIn("ОТКАЧУ", failed["text"])
        self.assertIn("recover-x", failed["text"])

    def test_armed_receipts_are_listed_for_admin_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            hostrecovery.configure(Path(tmp))
            with mock.patch.object(hostrecovery.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 0, "", "")):
                receipt = hostrecovery.arm("docker", "docker start praxis", delay=300,
                                           description="restore praxis")
            rows = hostverbs.armed_recoveries()
        self.assertEqual([row["id"] for row in rows], [receipt["id"]])
        self.assertGreater(rows[0]["seconds_left"], 0)
        self.assertIn("ОТКАЧУ", rows[0]["notice"])


class AdvisorHonestyCase(unittest.TestCase):
    def test_manifest_says_plainly_that_no_floor_is_configured(self):
        with mock.patch.object(advisor, "PROTECTED_ROOTS", ()):
            row = advisor.manifest()
        self.assertFalse(row["protected_roots_configured"])
        self.assertIn("НЕТ ни одного", row["floor"])
        with mock.patch.object(advisor, "PROTECTED_ROOTS", ("/opt/private",)):
            row = advisor.manifest()
        self.assertTrue(row["protected_roots_configured"])
        self.assertIn("/opt/private", row["floor"])


class OperationCapCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        brokerops.configure(self.base / "state")
        self.project = self.base / "project"
        self.project.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_caps_ride_along_with_a_successful_start_not_only_with_the_refusal(self):
        started = brokerops.op_start(str(self.project), "sleep 5", timeout=30)
        self.assertTrue(started["ok"], started)
        self.assertEqual(started["caps"]["max_operations"], brokerops.MAX_OPERATIONS)
        self.assertGreaterEqual(started["caps"]["disk_free_mib"], 0)
        # `caps` — структурное поле, а `serverd_client.process('start')` печатает ей строку из
        # id/pid/cwd/log и до caps не дотягивается. `note` печатают все обёртки клиента.
        self.assertIn(str(brokerops.MAX_OPERATIONS), started["note"])
        self.assertIn("512 MiB", started["note"])
        with mock.patch.object(brokerops, "MAX_OPERATIONS", 1):
            refused = brokerops.op_start(str(self.project), "echo second", timeout=10)
        self.assertFalse(refused["ok"], refused)
        self.assertEqual(refused["code"], "operation_cap")
        self.assertIn("1", refused["error"])
        self.assertIn("op.list", refused["error"])
        brokerops.op_stop(started["id"])

    def test_disk_floor_is_named_in_the_refusal(self):
        low = mock.Mock(free=10 * 1024 * 1024, total=1, used=1)
        with mock.patch.object(brokerops.shutil, "disk_usage", return_value=low):
            refused = brokerops.op_start(str(self.project), "echo x", timeout=10)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["code"], "disk_low")
        self.assertIn("512 MiB", refused["error"])
        self.assertIn("10 MiB", refused["error"])


class LegacyTaskMigrationCase(unittest.TestCase):
    def test_v1_tasks_move_once_into_canonical_forge(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = base / "serverd" / "tasks" / "hcode-abcd1234"
            legacy.mkdir(parents=True)
            (legacy / "task.json").write_text(json.dumps({
                "id": "hcode-abcd1234", "goal": "continue me", "root": "/etc",
                "target": "/etc", "status": "active", "created": "2026-01-01T00:00:00+00:00",
            }), encoding="utf-8")
            (legacy / "events.jsonl").write_text('{"kind":"task_started"}\n', encoding="utf-8")
            (legacy / "orientation.txt").write_text("old orientation\n", encoding="utf-8")
            forge_state = base / "memory" / ".forge"

            first = migrate_v1_tasks.migrate(legacy.parent, forge_state)
            second = migrate_v1_tasks.migrate(legacy.parent, forge_state)
            self.assertEqual(first["migrated"], ["hcode-abcd1234"])
            self.assertEqual(second["skipped"], ["hcode-abcd1234"])
            target = forge_state / "tasks" / "hcode-abcd1234"
            task = json.loads((target / "task.json").read_text(encoding="utf-8"))
            self.assertEqual(task["backend"], "praxis.host.v2")
            self.assertEqual(task["migrated_from"], "praxis.host.v1")
            events = (target / "events.jsonl").read_text(encoding="utf-8")
            self.assertEqual(events.count("migrated_from_serverd_v1"), 1)
            self.assertEqual((target / "orientation.txt").read_text(encoding="utf-8"),
                             "old orientation\n")


class RecoveryCase(unittest.TestCase):
    def test_receipt_is_visible_and_confirmable(self):
        with tempfile.TemporaryDirectory() as tmp:
            hostrecovery.configure(Path(tmp))
            with mock.patch("hostrecovery.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, "", "")
                receipt = hostrecovery.arm("ssh", "systemctl start ssh", delay=60,
                                           verify_command="systemctl is-active ssh")
                self.assertEqual(receipt["status"], "armed")
                confirmed = hostrecovery.confirm(receipt["id"])
            self.assertTrue(confirmed["ok"], confirmed)
            self.assertEqual(confirmed["receipt"]["status"], "confirmed")
            stop_call = next(call for call in run.call_args_list
                             if call.args and call.args[0][:2] == ["systemctl", "stop"])
            self.assertEqual(len(stop_call.args[0]), 3)
            self.assertTrue(stop_call.args[0][-1].endswith(".timer"))

    def test_confirm_accepts_timer_already_stopped_and_garbage_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            hostrecovery.configure(Path(tmp))
            with mock.patch("hostrecovery.subprocess.run") as run:
                run.side_effect = [
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 5, "", "Unit x.timer not loaded."),
                    subprocess.CompletedProcess([], 3, "inactive\n", ""),
                ]
                receipt = hostrecovery.arm("file", "true", delay=60)
                confirmed = hostrecovery.confirm(receipt["id"])
            self.assertTrue(confirmed["ok"], confirmed)
            self.assertEqual(confirmed["receipt"]["status"], "confirmed")

    def test_structured_reboot_circuit_breaker_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            hostrecovery.configure(Path(tmp))
            for _ in range(3):
                self.assertTrue(hostrecovery.record_reboot(1)["ok"])
            denied = hostrecovery.record_reboot(1)
            self.assertFalse(denied["ok"])
            self.assertIn("circuit breaker", denied["reason"])

    def test_failed_confirmation_check_leaves_recovery_armed(self):
        with tempfile.TemporaryDirectory() as tmp:
            hostrecovery.configure(Path(tmp))
            with mock.patch("hostrecovery.subprocess.run") as run:
                run.side_effect = [
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 1, "bad", ""),
                ]
                receipt = hostrecovery.arm("ssh", "systemctl start ssh", delay=60,
                                           verify_command="systemctl is-active ssh")
                confirmed = hostrecovery.confirm(receipt["id"])
            self.assertFalse(confirmed["ok"])
            self.assertEqual(confirmed["receipt"]["status"], "armed")
            self.assertEqual(run.call_count, 2)  # arm + verify; no timer stop


class TypedVerbCase(unittest.TestCase):
    def test_typed_file_write_returns_before_after_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hostrecovery.configure(base / "state")
            path = base / "config.txt"
            path.write_text("old\n", encoding="utf-8")
            result = hostverbs.file("write", str(path), content="new\n")
            self.assertTrue(result["ok"], result)
            self.assertNotEqual(result["before"]["sha256"], result["after"]["sha256"])
            self.assertTrue(Path(result["backup"]).is_file())
            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")

    def test_critical_systemctl_action_returns_recovery_receipt_not_denial(self):
        show = "ActiveState=active\nSubState=running\nUnitFileState=enabled\nLoadState=loaded\n"
        with mock.patch.object(hostverbs, "_sh", side_effect=[(0, show), (0, ""), (0, show)]), \
             mock.patch.object(hostrecovery, "arm", return_value={"id": "recover-one", "status": "armed"}):
            result = hostverbs.systemctl("restart", "ssh", recover_after=90)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["recovery"]["id"], "recover-one")
        self.assertIn("SSH", result.get("advice", ""))

    def test_pkg_query_is_structured(self):
        with mock.patch.object(hostverbs, "_pkg_manager", return_value="apt-get"), \
             mock.patch.object(hostverbs, "_pkg_snapshot",
                               return_value={"git": {"installed": True, "version": "1"}}):
            result = hostverbs.pkg("query", "git")
        self.assertTrue(result["ok"])
        self.assertEqual(result["after"]["git"]["version"], "1")

    def test_compose_requires_explicit_project_directory(self):
        result = hostverbs.docker("compose", args="down")
        self.assertFalse(result["ok"])
        self.assertIn("absolute cwd", result["error"])

    def test_typed_file_verbs_honor_explicit_owner_scope(self):
        with mock.patch.object(advisor, "PROTECTED_ROOTS", ("/opt/private-root",)):
            read = hostverbs.file("stat", "/opt/private-root/.env")
            write = hostverbs.file("copy", "/tmp/free.txt",
                                   target="/opt/private-root/copied.txt")
        self.assertFalse(read["ok"], read)
        self.assertFalse(write["ok"], write)
        self.assertIn("owner-configured", read["error"])
        self.assertIn("owner-configured", write["error"])

    def test_typed_container_and_unit_verbs_honor_explicit_owner_scope(self):
        with mock.patch.object(advisor, "PROTECTED_ROOTS", ("/opt/private-root",)), \
             mock.patch.object(hostverbs, "_sh") as shell:
            container = hostverbs.docker("restart", "private-root-worker")
            unit = hostverbs.systemctl("stop", "private-root.service")
        self.assertFalse(container["ok"], container)
        self.assertFalse(unit["ok"], unit)
        shell.assert_not_called()

    def test_no_compiled_in_project_scope(self):
        with mock.patch.object(advisor, "PROTECTED_ROOTS", ()):
            self.assertFalse(advisor.contains_protected("/"))
            self.assertFalse(advisor.command_touches_protected("rm -rf /opt/hardbot"))

    def test_system_observation_is_not_blocked_merely_for_running_from_root(self):
        self.assertFalse(advisor.command_may_traverse("systemctl status ssh"))
        self.assertFalse(advisor.command_may_traverse("free -h && uptime"))
        self.assertTrue(advisor.command_may_traverse("docker ps"))
        self.assertTrue(advisor.command_may_traverse("rg TODO /opt"))

    def test_docker_ps_does_not_hide_projects_by_name(self):
        rows = {"exit": 0, "rows": ["praxis|Up|praxis:latest", "hardbot|Up|private:latest"]}
        with mock.patch.object(hostverbs, "_docker_snapshot", return_value=rows):
            result = hostverbs.docker("ps")
        self.assertTrue(result["ok"], result)
        self.assertIn("praxis", result["text"])
        self.assertIn("hardbot", result["text"].lower())

    def test_install_manifest_physically_removes_second_brain(self):
        install = (HERE / "install.sh").read_text(encoding="utf-8")
        service = (HERE / "praxis-serverd.service").read_text(encoding="utf-8")
        control = (HERE / "serverdctl").read_text(encoding="utf-8")
        self.assertIn("broker.py", install)
        self.assertIn("migrate_v1_tasks.py", install)
        self.assertIn("rm -f \"$LIB/hostforge.py\"", install)
        self.assertNotIn("PRAXIS_SERVERD_LLM", service)
        self.assertIn("KillMode=process", service)
        self.assertIn('m["protected_roots"]', control)
        self.assertNotIn('m["secret_globs"]', control)


class BrokerSocketCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "run").mkdir()
        (self.home / "state").mkdir()
        (self.home / "run" / "token").write_text("test-token", encoding="utf-8")
        self.project = self.home / "project"
        self.project.mkdir()
        (self.project / "hello.txt").write_text("hello\n", encoding="utf-8")
        self.sock = self.home / "run" / "serverd.sock"
        self.env = {**os.environ, "PRAXIS_SERVERD_HOME": str(self.home),
                    "PRAXIS_SERVERD_STATE": str(self.home / "state"),
                    "PRAXIS_SERVERD_RUN": str(self.home / "run"),
                    "PRAXIS_SERVERD_PIN_CGROUP": "0",
                    "PYTHONPATH": os.pathsep.join([str(ROOT), str(HERE)])}
        self.proc = self._spawn_broker()

    def _spawn_broker(self):
        proc = subprocess.Popen([sys.executable, str(HERE / "broker.py")], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # bind() создаёт файл сокета РАНЬШЕ listen(); ждать появления файла — это гонка,
        # которая под нагрузкой даёт ConnectionRefused в следующем же _call. Ждём приёма.
        ready = False
        for _ in range(300):
            if proc.poll() is not None:
                break
            if self.sock.exists():
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(1)
                try:
                    probe.connect(str(self.sock))
                    ready = True
                except OSError:
                    pass
                finally:
                    probe.close()
                if ready:
                    break
            time.sleep(.03)
        if not ready:
            output = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            self.fail("broker socket did not accept connections: " + output)
        return proc

    def tearDown(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self.proc.stdout:
            self.proc.stdout.close()
        self.tmp.cleanup()

    def _call(self, verb: str, args: dict, *, request_id: str = "", token: str = "test-token") -> dict:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(30)
        client.connect(str(self.sock))
        request = {"protocol": "praxis.host.v2", "token": token,
                   "request_id": request_id or os.urandom(8).hex(), "verb": verb, "args": args}
        client.sendall(json.dumps(request).encode("utf-8") + b"\n")
        buffer = b""
        while b"\n" not in buffer:
            buffer += client.recv(65536)
        line, _ = buffer.split(b"\n", 1)
        client.close()
        return json.loads(line.decode("utf-8"))

    @staticmethod
    def _args_digest(args: dict) -> str:
        raw = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str,
                         separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def test_protocol_workspace_operation_idempotency_and_audit(self):
        status = self._call("admin.status", {})
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["protocol"], "praxis.host.v2")
        read = self._call("workspace.inspect", {"root": str(self.project),
                                                 "action": "read", "path": "hello.txt"})
        self.assertIn("hello", read["text"])
        rid = "same-operation"
        args = {"root_value": str(self.project), "command": "echo once", "timeout": 10}
        first = self._call("op.start", args, request_id=rid)
        second = self._call("op.start", args, request_id=rid)
        self.assertEqual(first["id"], second["id"])
        listed = self._call("op.list", {"root_value": str(self.project)})
        self.assertEqual(sum(1 for row in listed["operations"] if row["id"] == first["id"]), 1)
        for _ in range(100):
            polled = self._call("op.poll", {"operation_id": first["id"]})
            if polled.get("status") not in {"starting", "running", "finishing"}:
                break
            time.sleep(.02)
        self.assertEqual(polled.get("status"), "done", polled)
        request_path = self.home / "state" / "requests" / f"{rid}.json"
        request_path.write_text(json.dumps({"request_id": rid, "verb": "op.start",
                                            "args_digest": self._args_digest(args),
                                            "state": "started"}), encoding="utf-8")
        after_cache_loss = self._call("op.start", args, request_id=rid)
        self.assertEqual(after_cache_loss["id"], first["id"])
        self.assertTrue(after_cache_loss.get("reused"), after_cache_loss)
        verified = self._call("admin.audit.verify", {})
        self.assertTrue(verified["verify"]["ok"], verified)

    def test_uncertain_nonoperation_is_not_blindly_repeated(self):
        rid = "uncertain-edit"
        args = {"root": str(self.project), "action": "write", "path": "hello.txt",
                "content": "must-not-run\n"}
        request_path = self.home / "state" / "requests" / f"{rid}.json"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text(json.dumps({"request_id": rid, "verb": "workspace.edit",
                                            "args_digest": self._args_digest(args),
                                            "state": "started"}), encoding="utf-8")
        result = self._call("workspace.edit", args, request_id=rid)
        self.assertEqual(result.get("code"), "in_doubt", result)
        self.assertEqual((self.project / "hello.txt").read_text(encoding="utf-8"), "hello\n")

    def test_status_and_heavy_reads_carry_the_daemon_limits_over_the_wire(self):
        status = self._call("admin.status", {})
        self.assertEqual(status["limits"]["workspace_slots"], broker.WORKSPACE_SLOTS)
        self.assertEqual(status["limits"]["max_operations"], brokerops.MAX_OPERATIONS)
        self.assertIn("protected_roots_configured", status["advisor"])
        self.assertEqual(status["recovery"], [])
        heavy = self._call("workspace.inspect", {"root": str(self.project), "action": "impact"})
        self.assertTrue(heavy["ok"], heavy)
        # Срок из broker.handle доехал до бюджета обхода: он меньше «сколько влезет».
        self.assertLessEqual(heavy["limits"]["time_budget_s"], broker.WORKSPACE_BUDGET_SEC)
        self.assertTrue(heavy["limits"]["complete"], heavy["limits"])
        self.assertIn("бюджет", heavy["limits"]["note"])
        # Её собственный срок сильнее серверного — иначе это молчаливый несдвигаемый предел.
        hers = self._call("workspace.inspect", {"root": str(self.project), "action": "impact",
                                                "budget_seconds": broker.WORKSPACE_BUDGET_SEC * 3})
        self.assertTrue(hers["ok"], hers)
        self.assertGreater(hers["limits"]["time_budget_s"], broker.WORKSPACE_BUDGET_SEC)

    def test_budget_handle_reaches_dispatch_and_says_what_it_did(self):
        """R7: ручка обязана быть живой НА ПРОВОДЕ, а не только в чистой функции.
        И мусор в ней ничего не отменяет — вызов идёт по серверному сроку с признанием."""
        hers = self._call("workspace.inspect", {"root": str(self.project), "action": "impact",
                                                "budget_seconds": broker.WORKSPACE_BUDGET_SEC + 5})
        self.assertTrue(hers["ok"], hers)
        self.assertIn("ТВОЕМУ сроку", hers.get("budget_note", ""))
        junk = self._call("workspace.inspect", {"root": str(self.project), "action": "impact",
                                                "budget_seconds": "вечность"})
        self.assertTrue(junk["ok"], junk)          # не запрет, а откат на серверный срок
        self.assertIn("не число секунд", junk.get("budget_note", ""))

    def test_the_daemon_reports_whether_the_handle_ever_arrived_over_the_wire(self):
        """Свежий демон ещё не видел ни одной заявки с `budget_seconds` — и говорит именно

        это, а не «сдвигай, если хочешь». После первой же настоящей заявки утверждение
        меняется на противоположное. Вопрос «жив ли рычаг» решается фактом, а не верой.
        """
        before = self._call("admin.status", {})["limits"]
        self.assertEqual(before["workspace_budget_handle_seen"], 0)
        self.assertIn("не привезла ни одна заявка", before["note"])
        self._call("workspace.inspect", {"root": str(self.project), "action": "impact",
                                          "budget_seconds": 30})
        after = self._call("admin.status", {})["limits"]
        self.assertEqual(after["workspace_budget_handle_seen"], 1)
        self.assertIn("ручка живая", after["note"])
        # Лёгкое чтение ручку не привозит — счётчик не имеет права расти сам по себе.
        self._call("workspace.inspect", {"root": str(self.project), "action": "read",
                                          "path": "hello.txt"})
        self.assertEqual(self._call("admin.status", {})["limits"]
                         ["workspace_budget_handle_seen"], 1)

    def test_checkpoint_still_routes_to_git_after_it_stopped_taking_a_slot(self):
        """Смок на снятый забор: убрав checkpoint из `_heavy`, он перестал получать `deadline`

        от `workspace_slot` — путь обязан остаться живым и доезжать до самого git.
        """
        saved = self._call("workspace.checkpoint", {"root": str(self.project),
                                                    "message": "smoke"})
        self.assertNotEqual(saved.get("code"), "busy", saved)
        # git в этом каталоге не заведён — ответ пришёл от checkpoint, а не от очереди.
        self.assertIn("not in git", saved.get("error", ""), saved)

    def test_bad_token_does_not_enter_broker(self):
        result = self._call("admin.status", {}, token="wrong")
        self.assertEqual(result["code"], "unauth")

    def test_operation_survives_broker_restart_and_reconciles(self):
        started = self._call("op.start", {"root_value": str(self.project),
                                           "command": "sleep .2; echo survived", "timeout": 10})
        self.assertTrue(started["ok"], started)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        if self.proc.stdout:
            self.proc.stdout.close()
        try:
            self.sock.unlink()
        except OSError:
            pass
        self.proc = self._spawn_broker()
        for _ in range(100):
            polled = self._call("op.poll", {"operation_id": started["id"]})
            if polled.get("status") not in {"starting", "running", "finishing"}:
                break
            time.sleep(.03)
        self.assertEqual(polled.get("status"), "done", polled)
        self.assertIn("survived", polled.get("body", ""))

    def test_concurrent_same_idempotency_key_executes_once(self):
        marker = self.project / "marker.txt"
        args = {"root_value": str(self.project),
                "command": "printf 'x\\n' >> marker.txt; sleep .1", "timeout": 10}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self._call, "op.start", args, request_id="parallel-same")
                       for _ in range(2)]
            first, second = [future.result() for future in futures]
        self.assertEqual(first["id"], second["id"])
        for _ in range(100):
            polled = self._call("op.poll", {"operation_id": first["id"]})
            if polled.get("status") not in {"starting", "running", "finishing"}:
                break
            time.sleep(.02)
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["x"])


class WorkspaceBudgetHandleCase(unittest.TestCase):
    """R7: 420с назывались «советом» при мёртвой ручке.

    В комментарии у `workspace.inspect` стояло «её собственная ручка — budget_seconds, и
    она сильнее этого срока (совет, не забор)», а `grep -rn budget_seconds --include=*.py`
    вне `serverd/` давал пусто: из её рук аргумент не передавался ниоткуда. Здесь
    проверяется всё, что принадлежит демону: ручка живая, её потолок назван, мусор в ней
    не молчит и ничего не запрещает, а отсутствие ручки признаётся в ответе тогда, когда
    именно серверный срок обход и обрезал.
    """

    def test_absent_handle_is_not_an_excuse_to_say_anything(self):
        self.assertEqual(broker.workspace_budget(None), (None, ""))
        self.assertEqual(broker.workspace_budget(""), (None, ""))

    def test_her_number_wins_over_the_server_deadline(self):
        seconds, note = broker.workspace_budget(broker.WORKSPACE_BUDGET_SEC + 7, 1.0)
        self.assertEqual(seconds, broker.WORKSPACE_BUDGET_SEC + 7)
        self.assertIn("ТВОЕМУ сроку", note)

    def test_light_read_does_not_claim_a_server_deadline_it_never_had(self):
        """Мой срок стоит только на тяжёлом чтении. Сказать про `read`/`list` «считала по
        своему сроку 420с» было бы новой ложью на месте вылеченной: там его нет вовсе."""
        _, heavy = broker.workspace_budget("вечность", 1.0)
        _, light = broker.workspace_budget("вечность", None)
        self.assertIn(f"{broker.WORKSPACE_BUDGET_SEC:.0f}с", heavy)
        self.assertNotIn(f"{broker.WORKSPACE_BUDGET_SEC:.0f}с", light)
        self.assertIn("не из тяжёлых", light)

    def test_over_the_ceiling_is_clipped_and_said_out_loud(self):
        asked = broker.WORKSPACE_BUDGET_MAX_SEC * 2
        seconds, note = broker.workspace_budget(asked, 1.0)
        self.assertEqual(seconds, broker.WORKSPACE_BUDGET_MAX_SEC)
        self.assertIn(f"{asked:.0f}с", note)                       # что просила
        self.assertIn(f"{broker.WORKSPACE_BUDGET_MAX_SEC:.0f}с", note)   # что посчитала
        self.assertIn("PRAXIS_SERVERD_WORKSPACE_BUDGET_MAX_SEC", note)   # чем сдвинуть

    def test_ceiling_boundary_is_not_clipped(self):
        seconds, _ = broker.workspace_budget(broker.WORKSPACE_BUDGET_MAX_SEC, 1.0)
        self.assertEqual(seconds, broker.WORKSPACE_BUDGET_MAX_SEC)

    def test_garbage_falls_back_loudly_and_blocks_nothing(self):
        for bad in ("вечность", -5, 0):
            seconds, note = broker.workspace_budget(bad, 1.0)
            self.assertIsNone(seconds, bad)
            self.assertIn(f"{broker.WORKSPACE_BUDGET_SEC:.0f}с", note)
            self.assertTrue(note.strip(), bad)

    def test_limits_name_the_handle_its_ceiling_and_the_honest_unknown(self):
        row = broker.limits()
        self.assertEqual(row["workspace_budget_handle"], "budget_seconds")
        self.assertEqual(row["workspace_budget_max_s"], broker.WORKSPACE_BUDGET_MAX_SEC)
        self.assertIn("budget_seconds", row["note"])
        self.assertIn(f"{broker.WORKSPACE_BUDGET_MAX_SEC:.0f}с", row["note"])

    def test_limits_admit_whether_the_handle_has_ever_actually_arrived(self):
        """27.07 `grep -rn budget_seconds --include=*.py` вне serverd/ был ПУСТ: ручка

        объявлялась рычагом, не существуя в клиенте. «Отсюда не видно» было честно про
        исходники и всё же оставляло рычаг обещанным. Приезжала ли ручка хоть раз — демон
        знает точно, и обязан сказать: несдвигаемый предел, названный сдвигаемым, — ложь.
        """
        with mock.patch.object(broker, "_budget_handle_seen", 0):
            silent = broker.limits()
        self.assertEqual(silent["workspace_budget_handle_seen"], 0)
        self.assertIn("не привезла ни одна заявка", silent["note"])
        self.assertIn("НЕСДВИГАЕМЫ", silent["note"])
        self.assertIn("serverd_client", silent["note"])     # где чинить, а не «где-то»
        with mock.patch.object(broker, "_budget_handle_seen", 4):
            alive = broker.limits()
        self.assertEqual(alive["workspace_budget_handle_seen"], 4)
        self.assertIn("ручка живая", alive["note"])
        self.assertNotIn("НЕСДВИГАЕМЫ", alive["note"])

    def test_server_deadline_confesses_itself_when_it_was_the_one_that_cut(self):
        """Без ручки и с неполным обходом ответ обязан сказать, ЧЕЙ срок его обрезал."""
        cut = broker._with_budget_note({"ok": True, "truncated": True, "note": "обход неполон"},
                                       None, "", 1.0)
        self.assertIn("МОЙ срок", cut["budget_note"])
        self.assertIn("budget_seconds", cut["budget_note"])
        self.assertIn("обход неполон", cut["note"])          # чужую приписку не затёрли
        self.assertIn("МОЙ срок", cut["note"])
        # Причину усечения себе не приписываем: связать обход мог и файловый кап.
        self.assertIn("если обход связало ВРЕМЯ", cut["budget_note"])
        # Полный ответ признаваться не обязан — это была бы приписка ни о чём.
        whole = broker._with_budget_note({"ok": True, "limits": {"complete": True}},
                                         None, "", 1.0)
        self.assertNotIn("budget_note", whole)
        # А на лёгком чтении моего срока не было — и признаваться в нём нельзя.
        light = broker._with_budget_note({"ok": True, "truncated": True}, None, "", None)
        self.assertNotIn("budget_note", light)


LINUX_ONLY = unittest.skipUnless(
    sys.platform.startswith("linux"),
    "hostproc — про /proc, killpg и группы процессов; прод linux, стенд гоняем в образе")


class _HostProcStand(unittest.TestCase):
    """Общий стенд для hostproc: настоящие процессы, ничего не мокается."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.groups: list[int] = []
        self.procs: list[subprocess.Popen] = []

    def tearDown(self):
        for pgid in self.groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
        for proc in self.procs:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:      # noqa: BLE001 — уборка стенда, не предмет теста
                pass
        self.tmp.cleanup()

    @staticmethod
    def _proc_state(pid: int) -> str:
        """Буква состояния из /proc/<pid>/stat; "" — процесса нет.

        `kill(pid, 0)` здесь не годится: в контейнере PID 1 — сам процесс тестов, орфаны
        репарентятся на него и, будучи убитыми, остаются ЗОМБИ. Зомби для kill() жив —
        то есть проверка живости через kill прошла бы и на убитом постороннем.
        """
        try:
            data = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        except OSError:
            return ""
        return data[data.rfind(")") + 1:].split()[0]

    def _assert_alive(self, pid: int, why: str = ""):
        self.assertNotIn(self._proc_state(pid), ("Z", "X", ""), why)

    def _wait_dead(self, pid: int, timeout: float = 6.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self._proc_state(pid)
            if state in ("Z", "X", ""):
                return state
            time.sleep(.05)
        return self._proc_state(pid)

    def _dead_pid(self) -> int:
        for candidate in range(999_999, 999_800, -1):
            if not hostproc.pid_alive(candidate):
                return candidate
        self.skipTest("не нашла заведомо свободный номер процесса")
        raise AssertionError

    def _leaderless_group(self) -> tuple[int, int]:
        """Двойная вилка: лидер выходит, /proc/<pgid> пустеет, группа жива за счёт потомка.

        Именно этот стенд ловит дыру. Прошлый заход брал ЖИВОГО лидера и был зелёным
        на коде, который сносил чужую безлидерную группу в 3 сценариях из 3.
        """
        marker = self.base / f"victim-{len(self.groups)}.pid"
        leader = subprocess.Popen(["sh", "-c", f"sleep 300 & echo $! > {marker}; exit 0"],
                                  start_new_session=True)
        pgid = os.getpgid(leader.pid)
        leader.wait()
        self.groups.append(pgid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (marker.exists() and marker.read_text().strip()):
            time.sleep(.02)
        victim = int(marker.read_text().strip())
        while time.monotonic() < deadline and Path(f"/proc/{pgid}").exists():
            time.sleep(.02)
        self.assertFalse(Path(f"/proc/{pgid}").exists(), "лидер не исчез — стенд не собрался")
        os.killpg(pgid, 0)                      # группа жива: есть кого снести по ошибке
        self._assert_alive(victim, "посторонний не дожил до стенда")
        return pgid, victim

    def _live_group(self, command: str = "sleep 300") -> int:
        proc = subprocess.Popen(["sh", "-c", command], start_new_session=True)
        self.procs.append(proc)
        pgid = os.getpgid(proc.pid)
        self.groups.append(pgid)
        return pgid

    def _unit(self, name: str, **state) -> Path:
        directory = self.base / name
        directory.mkdir(parents=True, exist_ok=True)
        if state:
            (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
        return directory

    def _result(self, directory: Path) -> dict:
        path = directory / "result.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@LINUX_ONLY
class RootDoesNotKillStrangersCase(_HostProcStand):
    """[P0] Демон работает от рута рядом с praxis/mailbot/relay/ollama Егора.

    Стенд ревью 27.07: три сценария, в каждом рут сносил ЧУЖУЮ безлидерную группу и
    докладывал «остановлен (SIGKILL)». Тождество должно доказываться, а недоказанное —
    называться недоказанным.
    """

    def _refusal_is_honest(self, message: str, pgid: int, victim: int):
        self.assertIn(f"/proc/{pgid}/cmdline", message)      # чем посмотреть
        self.assertIn(f"kill -TERM -{pgid}", message)        # как снять руками
        time.sleep(hostproc.TERM_GRACE_SEC + .3)             # переживём любое отложенное добивание
        self._assert_alive(victim, f"посторонний {victim} убит рутом: {message}")

    def test_a_unit_from_a_previous_boot_never_touches_the_number_it_remembers(self):
        pgid, victim = self._leaderless_group()
        unit = self._unit("op-prev-boot", status="running", pgid=pgid, child_pid=pgid,
                          supervisor_pid=self._dead_pid(), supervisor_starttime="424242",
                          child_starttime="123456", boot_id="0000-чужая-загрузка")
        message = hostproc.stop(unit)
        self.assertIn("ПРОШЛУЮ загрузку", message)
        self._refusal_is_honest(message, pgid, victim)
        self.assertEqual(self._result(unit), {}, "надгробие поверх недоказанного")

    def test_a_leaderless_group_whose_label_no_longer_matches_is_left_alone(self):
        pgid, victim = self._leaderless_group()
        unit = self._unit("op-wrong-label", status="running", pgid=pgid, child_pid=pgid,
                          supervisor_pid=self._dead_pid(), supervisor_starttime="424242",
                          child_starttime="999999999", boot_id=hostproc.boot_id())
        message = hostproc.stop(unit)
        # Метку сверять не с чем — лидера нет; значит и доказательства нет.
        self.assertIn("не убила группу", message)
        self._refusal_is_honest(message, pgid, victim)
        self.assertEqual(self._result(unit), {})

    def test_a_live_number_taken_over_by_a_stranger_is_named_as_such(self):
        """Тот же номер, но лидер ЖИВ и это чужой процесс: метка рождения его выдаёт."""
        pgid = self._live_group()
        unit = self._unit("op-reused-number", status="running", pgid=pgid, child_pid=pgid,
                          supervisor_pid=self._dead_pid(), supervisor_starttime="424242",
                          child_starttime="999999999", boot_id=hostproc.boot_id())
        message = hostproc.stop(unit)
        self.assertIn("занят ДРУГИМ процессом", message)
        self.assertIn(hostproc._starttime(pgid), message)   # называет, что увидела на самом деле
        self._refusal_is_honest(message, pgid, pgid)
        self.assertEqual(self._result(unit), {})

    def test_a_legacy_unit_without_a_label_and_without_a_supervisor_proves_nothing(self):
        pgid, victim = self._leaderless_group()
        unit = self._unit("op-legacy-dead", status="running", pgid=pgid, child_pid=pgid,
                          supervisor_pid=self._dead_pid(), boot_id=hostproc.boot_id())
        message = hostproc.stop(unit)
        self.assertIn("не убила группу", message)
        self.assertIn("Ни жив, ни мёртв не утверждаю", message)
        self._refusal_is_honest(message, pgid, victim)
        self.assertEqual(self._result(unit), {})
        # poll не подхватывает выдуманный исход: раз мы ничего не решили — статус прежний
        self.assertEqual(hostproc.poll(unit).get("status"), "lost")

    def test_the_third_proof_cannot_be_degenerated_by_an_empty_path(self):
        """[P2] В откаченной версии `unit_dir` был необязательным и вырождался в Path("."):

        `"" in cmdline` истинно всегда, третье доказательство доказывало что угодно.
        """
        pgid = self._live_group()
        state = {"supervisor_pid": os.getpid(), "boot_id": hostproc.boot_id()}
        self.assertFalse(hostproc._supervisor_holds(state, Path(".")))
        self.assertFalse(hostproc._supervisor_holds(state, Path("")))
        self.assertFalse(hostproc._supervisor_holds(state, Path("/")))
        # и доказательства нельзя не передать: обязательные позиционные, без значений по умолчанию
        params = inspect.signature(hostproc._kill_tree).parameters
        for name in ("state", "unit_dir"):
            self.assertIs(params[name].default, inspect.Parameter.empty)
        self.assertIn("не убила группу", hostproc._kill_doubt(pgid, {}, self.base / "op-x"))


@LINUX_ONLY
class RootStillStopsWhatIsProvablyHersCase(_HostProcStand):
    """Обратная сторона: доказательства не должны превратиться в забор."""

    def test_a_running_operation_of_hers_is_still_stopped(self):
        unit = self._unit("op-real")
        hostproc.spawn(unit, "sleep 300", "/tmp", timeout=0)
        pgid = 0
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not pgid:
            pgid = int(hostproc.poll(unit).get("pgid") or 0)
            time.sleep(.05)
        self.assertTrue(pgid, "супервизор не опубликовал группу — стенд не собрался")
        self.groups.append(pgid)
        message = hostproc.stop(unit)
        self.assertIn("остановлен", message)
        self.assertIn(self._wait_dead(pgid), ("Z", "X", ""))
        # исход записан и не выдуман: либо наше надгробие, либо правда супервизора (SIGTERM = -15)
        result = self._result(unit)
        self.assertIn(result.get("status"), {"stopped", "failed"}, result)
        if result.get("status") == "failed":
            self.assertEqual(result.get("exit"), -signal.SIGTERM, result)

    def test_a_legacy_unit_is_stoppable_while_its_own_supervisor_still_holds_the_number(self):
        """На проде 7 юнитов из 450 без метки рождения. Пока жив их супервизор, номер

        ребёнка держит ядро (даже мёртвый ребёнок — зомби), и это доказательство тождества.
        """
        unit = self._unit("op-legacy-alive")
        supervisor = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", "--supervise", str(unit)])
        self.procs.append(supervisor)
        pgid = self._live_group()
        (unit / "state.json").write_text(json.dumps({
            "status": "running", "pgid": pgid, "child_pid": pgid,
            "supervisor_pid": supervisor.pid, "boot_id": hostproc.boot_id()}), encoding="utf-8")
        message = hostproc.stop(unit)
        self.assertIn("остановлен", message)
        self.assertIn(self._wait_dead(pgid), ("Z", "X", ""))
        self.assertEqual(self._result(unit).get("status"), "stopped")

    def test_a_group_that_ignores_sigterm_is_killed_and_the_deadline_is_named(self):
        pgid = self._live_group("trap '' TERM; sleep 300")
        unit = self._unit("op-stubborn", status="running", pgid=pgid, child_pid=pgid,
                          supervisor_pid=self._dead_pid(), boot_id=hostproc.boot_id(),
                          child_starttime=hostproc._starttime(pgid))
        started = time.monotonic()
        message = hostproc.stop(unit)
        spent = time.monotonic() - started
        self.assertIn("SIGKILL", message)
        # закон 2: срок, который сработал, назван в тексте, который она читает
        self.assertIn(f"{hostproc._secs(hostproc.TERM_GRACE_SEC)}с", message)
        self.assertGreaterEqual(spent, hostproc.TERM_GRACE_SEC * .8)
        self.assertLess(spent, hostproc.TERM_GRACE_SEC + 3)
        self.assertIn(self._wait_dead(pgid), ("Z", "X", ""))


@LINUX_ONLY
class StopNeverOverwritesTheTruthCase(_HostProcStand):
    """[P1] Закон 3: команда отработала с кодом 7 — значит ей говорят «код 7»."""

    def test_exit_code_survives_a_stop_that_arrived_just_too_late(self):
        for attempt in range(10):
            unit = self._unit(f"op-fast-{attempt}")
            hostproc.spawn(unit, "exit 7", "/tmp", timeout=30)
            message = hostproc.stop(unit)
            # Первое, что она увидит следом за op.stop. Надгробие «stopped» здесь — это уже
            # ложь: команда отработала сама и с кодом 7. «Ещё бежит» ложью не является.
            immediately = hostproc.poll(unit)
            self.assertNotEqual(immediately.get("status"), "stopped", (attempt, immediately))
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                result = self._result(unit)
                if "exit" in result or result.get("status") == "stopped":
                    break
                time.sleep(.02)
            result = self._result(unit)
            self.assertEqual(result.get("exit"), 7, (attempt, message, result))
            self.assertEqual(result.get("status"), "failed", (attempt, message, result))
            self.assertNotIn("note", result)      # надгробия поверх правды нет
        self.assertIn("код выхода 7", message)    # и в тексте ей — тот же код, а не «остановлено»

    def test_a_crashed_supervisor_is_evidence_and_stop_does_not_erase_it(self):
        """`status: error` пишет сам `_supervise` (например «no space left on device»).

        Раньше он не входил в терминальные — и первый же op.stop затирал улику надгробием.
        """
        pgid = self._live_group()
        unit = self._unit("op-crashed", status="running", pgid=pgid, child_pid=pgid,
                          supervisor_pid=self._dead_pid(), boot_id=hostproc.boot_id(),
                          child_starttime=hostproc._starttime(pgid))
        (unit / "result.json").write_text(json.dumps({
            "status": "error", "error": "OSError: [Errno 28] no space left on device",
            "finished": "2026-07-27T00:00:00+00:00"}), encoding="utf-8")
        message = hostproc.stop(unit)
        self.assertIn("супервизор этой операции сам упал", message)
        self.assertIn("no space left on device", message)
        result = self._result(unit)
        self.assertEqual(result.get("status"), "error")
        self.assertIn("no space left on device", result.get("error", ""))
        self.assertEqual(hostproc.poll(unit).get("status"), "error")
        self.assertIn("error", hostproc.TERMINAL_STATUSES)
        # доказуемо своя, живая группа рядом — и всё равно не тронута: исход уже записан
        self._assert_alive(pgid)

    def test_a_kill_that_failed_is_not_written_down_as_a_stop(self):
        """SIGKILL может не пройти (EPERM: цель в другом user-ns, у неё стоит SIGKILL-иммунитет).

        Тогда группа скорее всего ЖИВА. Написать в result.json «stopped» — соврать про смерть
        команды; в первой редакции этой правки здесь стояло «исход записать можно» = True.
        """
        pgid = self._live_group("trap '' TERM; sleep 300")
        unit = self._unit("op-eperm", status="running", pgid=pgid, child_pid=pgid,
                          supervisor_pid=self._dead_pid(), boot_id=hostproc.boot_id(),
                          child_starttime=hostproc._starttime(pgid))
        real_killpg = os.killpg

        def refuse_the_last_blow(target, sig):
            if sig == signal.SIGKILL:
                raise PermissionError(1, "Operation not permitted")
            return real_killpg(target, sig)

        with mock.patch.object(hostproc.os, "killpg", refuse_the_last_blow):
            message = hostproc.stop(unit)
        self.assertIn("не остановлен", message)
        self.assertIn("PermissionError", message)
        self.assertEqual(self._result(unit), {}, "надгробие поверх провалившегося SIGKILL")
        self._assert_alive(pgid, "команда жива, а ей записали исход")

    def test_when_the_supervisor_records_the_outcome_the_signal_is_not_called_pointless(self):
        """Сигнал послан, а исход записал супервизор — оба факта верны, и оба сказаны.

        Приписка «останавливать нечего, надгробие не пишу» верна только там, где сигнала
        НЕ было. После посланного SIGTERM она превращается во второе враньё подряд.
        """
        pgid = self._live_group("trap '' TERM; sleep 300")
        unit = self._unit("op-both", status="running", pgid=pgid, child_pid=pgid,
                          supervisor_pid=self._dead_pid(), boot_id=hostproc.boot_id(),
                          child_starttime=hostproc._starttime(pgid))
        payload = json.dumps({"status": "failed", "exit": -signal.SIGTERM,
                              "finished": "2026-07-27T00:00:00+00:00"})
        recorder = threading.Timer(
            .4, lambda: (unit / "result.json").write_text(payload, encoding="utf-8"))
        recorder.start()
        self.addCleanup(recorder.cancel)
        message = hostproc.stop(unit)
        self.assertIn("остановлен", message)
        self.assertIn(f"код выхода {-signal.SIGTERM}", message)
        self.assertNotIn("останавливать нечего", message)
        # правда супервизора цела, надгробия поверх неё нет
        self.assertEqual(self._result(unit).get("exit"), -signal.SIGTERM)
        self.assertEqual(self._result(unit).get("status"), "failed")

    def test_a_finished_run_is_reported_with_its_own_code_not_with_a_tombstone(self):
        unit = self._unit("op-done")
        (unit / "result.json").write_text(json.dumps(
            {"status": "done", "exit": 0, "finished": "2026-07-27T00:00:00+00:00"}),
            encoding="utf-8")
        message = hostproc.stop(unit)
        self.assertIn("код выхода 0", message)
        self.assertEqual(self._result(unit).get("status"), "done")


@LINUX_ONLY
class StopNamesItsOwnDeadlineCase(_HostProcStand):
    """START_GRACE_SEC: ждать номер группы можно, молчать об этом — нет."""

    def test_a_number_that_never_arrived_is_admitted_not_invented(self):
        unit = self._unit("op-no-number")
        started = time.monotonic()
        message = hostproc.stop(unit)
        spent = time.monotonic() - started
        self.assertIn(f"{hostproc._secs(hostproc.START_GRACE_SEC)}с", message)
        self.assertIn("op.poll", message)
        self.assertGreaterEqual(spent, hostproc.START_GRACE_SEC * .8)
        self.assertEqual(self._result(unit), {}, "надгробие вместо признания незнания")

    def test_a_number_published_late_is_still_caught(self):
        """Закон 4: потерять её намерение молча хуже, чем выполнить поздно."""
        unit = self._unit("op-late")
        pgid = self._live_group()
        payload = json.dumps({"status": "running", "pgid": pgid, "child_pid": pgid,
                              "supervisor_pid": self._dead_pid(), "boot_id": hostproc.boot_id(),
                              "child_starttime": hostproc._starttime(pgid)})
        publisher = threading.Timer(
            .5, lambda: (unit / "state.json").write_text(payload, encoding="utf-8"))
        publisher.start()
        self.addCleanup(publisher.cancel)
        message = hostproc.stop(unit)
        self.assertIn("остановлен", message)
        self.assertIn(self._wait_dead(pgid), ("Z", "X", ""))

    def test_a_shorter_deadline_admits_it_instead_of_killing_blind(self):
        """Та же граница с другой стороны: срок истёк раньше публикации — и это сказано."""
        unit = self._unit("op-late-refused")
        pgid = self._live_group()
        payload = json.dumps({"status": "running", "pgid": pgid, "child_pid": pgid,
                              "supervisor_pid": self._dead_pid(), "boot_id": hostproc.boot_id(),
                              "child_starttime": hostproc._starttime(pgid)})
        publisher = threading.Timer(
            .6, lambda: (unit / "state.json").write_text(payload, encoding="utf-8"))
        publisher.start()
        self.addCleanup(publisher.cancel)
        started = time.monotonic()
        with mock.patch.object(hostproc, "START_GRACE_SEC", 0.2):
            message = hostproc.stop(unit)
        spent = time.monotonic() - started
        self.assertIn("не остановлен", message)
        self.assertLess(spent, .5, "срок не соблюдён — ждали дольше собственного предела")
        self.assertEqual(self._result(unit), {})
        time.sleep(.6)
        self._assert_alive(pgid, "команду сняли, хотя сказали «не остановлен»")


if __name__ == "__main__":
    unittest.main()
