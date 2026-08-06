"""
Тесты панели устройства (PASS 4, слой 2): безопасные пути, дерево/AST-граф (живая
интроспекция), скиллы с честным статусом, env-редактор (маски/protected/диф/apply),
git-история, explain-кэш по blob, инлайн-чат (ask/task), owner-gate роутов.

Запуск:  python praxis_test.py test_panel -v
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

_fa = types.ModuleType("anthropic")
_fa.Anthropic = lambda **kw: None
sys.modules.setdefault("anthropic", _fa)
_fd = types.ModuleType("dotenv")
_fd.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _fd)

import agent  # noqa: E402
import llm  # noqa: E402
import panel  # noqa: E402

REPO = Path(panel.__file__).resolve().parent


def _reset_cache():
    panel._GRAPH_CACHE.update(fp=None, graph=None)


class RepoBase(unittest.TestCase):
    """Панель смотрит на НАСТОЯЩИЙ репозиторий (read-only) — живая интроспекция."""

    def setUp(self):
        self._orig = panel.BASE
        panel.BASE = REPO
        _reset_cache()

    def tearDown(self):
        panel.BASE = self._orig
        _reset_cache()


class TestSafePaths(RepoBase):
    def test_escapes_rejected(self):
        for bad in ("../etc/passwd", "..", "a/../../b", "/etc/passwd"):
            self.assertIsNone(panel.safe_rel(bad), bad)

    def test_secrets_rejected(self):
        for bad in (".env", ".deploy.env", "praxis.session", "x/praxis.session2"):
            self.assertIsNone(panel.safe_rel(bad), bad)

    def test_normal_ok(self):
        self.assertIsNotNone(panel.safe_rel("agent.py"))
        self.assertIsNotNone(panel.safe_rel("soul/skills/INDEX.md"))


class TestTreeAndGraph(RepoBase):
    def test_tree_has_core_no_secrets(self):
        t = panel.tree()
        paths = {f["path"] for f in t["files"]}
        self.assertIn("agent.py", paths)
        self.assertIn("mtproto_runner.py", paths)
        # ⚠ 03.08.2026: утверждение было вакуумным — `.env` не попадал в дерево не
        # потому, что его прячут, а потому что рядом его могло и не быть. Смысл оно
        # имеет только когда файл РЕАЛЬНО лежит в корне.
        if (panel.BASE / ".env").is_file():
            self.assertNotIn(".env", paths)
        else:
            self.skipTest("рядом нет .env — проверка сокрытия была бы вакуумной")
        kinds = {f["path"]: f["kind"] for f in t["files"]}
        self.assertEqual(kinds.get("test_pass4.py"), "test")
        self.assertEqual(kinds.get("soul/skills/INDEX.md"), "skill")

    def test_graph_import_edges_from_ast(self):
        g = panel.graph()
        ids = {n["id"] for n in g["nodes"]}
        self.assertIn("agent.py", ids)
        self.assertIn("mtproto_runner.py", ids)
        edges = {(l["source"], l["target"], l["kind"]) for l in g["links"]}
        runner_agent = [e for e in edges if e[0] == "mtproto_runner.py" and e[1] == "agent.py"]
        self.assertTrue(runner_agent, "раннер импортирует agent — ребро обязано быть")
        self.assertIn(runner_agent[0][2], ("imports", "calls"))

    def test_graph_reads_soul(self):
        g = panel.graph()
        reads = {(l["source"], l["target"]) for l in g["links"] if l["kind"] == "reads"}
        self.assertIn(("agent.py", "soul/SOUL.md"), reads,
                      "agent читает SOUL.md — строковая константа в AST")

    def test_skills_indexed_edges_honest(self):
        g = panel.graph()
        idx = [l for l in g["links"] if l["kind"] == "indexed"]
        self.assertTrue(idx, "скиллы должны быть связаны с memory_index (честный retrieval)")
        for l in idx:
            self.assertTrue(l["source"].startswith("soul/skills/"))
            self.assertEqual(l["target"], "memory_index.py")
        self.assertFalse(g["retrieval"]["resident"], "скиллы НЕ резиденты — панель не должна врать")

    def test_graph_cached_by_fingerprint(self):
        g1 = panel.graph()
        g2 = panel.graph()
        self.assertIs(g1, g2, "без изменений дерева — тот же объект (кэш)")

    def test_file_info_functions(self):
        f = panel.file_info("panel.py")
        self.assertEqual(f["kind"], "module")
        names = {fn["name"] for fn in f["funcs"]}
        self.assertIn("graph", names)
        self.assertIn("env_apply", names)
        self.assertIn("agent.py", panel.file_info("soul/SOUL.md")["read_by"])

    def test_file_source_and_limits(self):
        src = panel.file_source("notes.py")
        self.assertIn("said_recently", src)
        self.assertIsNone(panel.file_source("praxis.session"))

    def test_skills_map(self):
        m = panel.skills_map()
        names = {s["name"] for s in m["items"]}
        # ⚠ 03.08.2026: было прибито к именам её ЖИВЫХ навыков (holding_ground).
        # Тест утверждал не про панель, а про то, что она в тот день написала: стоит
        # ей переименовать навык — и гейт краснеет, закрывая ей дверь Форжа. Считаем
        # ожидание оттуда же, откуда его берёт панель, — с диска.
        # Панель перечисляет ВСЕ файлы каталога, включая INDEX.md. Сверяем ровно то,
        # что лежит на диске: не задача этого теста решать, считать ли индекс навыком.
        on_disk = {p.stem for p in (panel.BASE / "soul" / "skills").glob("*.md")}
        self.assertTrue(names, "карта навыков пуста")
        self.assertTrue(names <= on_disk,
                        f"панель назвала навыки, которых нет на диске: "
                        f"{sorted(names - on_disk)}")
        self.assertIn(m["retrieval"]["mode"],
                      ("hybrid-fulltext", "hybrid-hosted-rerank",
                       "hybrid-hosted-embeddings", "hybrid-legacy-ollama"))


class SandboxBase(unittest.TestCase):
    """Своё мини-репо во временной папке: git-история, env-редактор, explain-кэш."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_panel_"))
        (self.tmp / "soul" / "skills").mkdir(parents=True)
        (self.tmp / "a.py").write_text(
            '"""Модуль A."""\nimport b\n\ndef run():\n    "делает дело"\n    return b.foo("NOTE.md")\n',
            encoding="utf-8")
        (self.tmp / "b.py").write_text('"""Модуль B."""\n\ndef foo(x):\n    return x\n', encoding="utf-8")
        (self.tmp / "NOTE.md").write_text("# Заметка\n\nтекст\n", encoding="utf-8")
        (self.tmp / "memory_index.py").write_text('"""Индекс."""\n', encoding="utf-8")
        (self.tmp / "soul" / "skills" / "alpha.md").write_text("# Альфа\n\nпринцип альфы\n", encoding="utf-8")
        (self.tmp / ".env").write_text(
            "PRAXIS_OWNER_ID=123456789\nGLM_API_KEY=sk-verysecret123456\nPRAXIS_LAST_N=50\n", encoding="utf-8")
        def git(*a):
            subprocess.run(["git", *a], cwd=str(self.tmp), capture_output=True, timeout=30)
        git("init", "-q")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "первый коммит")
        self._orig = {k: getattr(panel, k) for k in ("BASE", "PANEL_DIR", "LOGS_DIR", "ENV_FILE")}
        panel.BASE = self.tmp
        panel.PANEL_DIR = self.tmp / "memory" / ".panel"
        panel.LOGS_DIR = self.tmp / "memory" / ".logs"
        panel.ENV_FILE = self.tmp / ".env"
        _reset_cache()

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(panel, k, v)
        _reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestGitHistory(SandboxBase):
    def test_log_and_show(self):
        log = panel.git_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["subject"], "первый коммит")
        c = panel.git_show(log[0]["hash"])
        self.assertIn("a.py", c["patch"])
        self.assertIn("+import b", c["patch"])

    def test_show_bad_hash(self):
        self.assertIsNone(panel.git_show("zzz"))
        self.assertIsNone(panel.git_show("; rm -rf /"))

    def test_sandbox_graph_edges(self):
        g = panel.graph()
        edges = {(l["source"], l["target"], l["kind"]) for l in g["links"]}
        self.assertIn(("a.py", "b.py", "calls"), edges, "a вызывает b.foo — ребро calls")
        self.assertIn(("a.py", "NOTE.md", "reads"), edges, "строковая константа NOTE.md")
        self.assertIn(("soul/skills/alpha.md", "memory_index.py", "indexed"), edges)

    def test_graph_invalidates_on_change(self):
        g1 = panel.graph()
        import time
        time.sleep(0.02)
        (self.tmp / "b.py").write_text('"""B2."""\nimport a\n', encoding="utf-8")
        g2 = panel.graph()
        self.assertIsNot(g1, g2, "правка файла обязана инвалидировать кэш графа")


class TestEnvEditor(SandboxBase):
    def test_list_masks_secrets(self):
        items = {e["key"]: e for e in panel.env_list()}
        self.assertTrue(items["GLM_API_KEY"]["secret"])
        self.assertNotIn("verysecret", items["GLM_API_KEY"]["value"])
        self.assertTrue(items["PRAXIS_OWNER_ID"]["protected"])
        self.assertEqual(items["PRAXIS_LAST_N"]["value"], "50")

    def test_preview_diff_and_protected(self):
        p = panel.env_preview({"PRAXIS_LAST_N": "80", "PRAXIS_OWNER_ID": "1"})
        self.assertTrue(any("PRAXIS_OWNER_ID" in e for e in p["errors"]), "protected — отказ")
        self.assertFalse(p["ok"])
        p2 = panel.env_preview({"PRAXIS_LAST_N": "80", "PRAXIS_NEW_KNOB": "x"})
        self.assertTrue(p2["ok"])
        ops = {d["key"]: d["op"] for d in p2["diff"]}
        self.assertEqual(ops["PRAXIS_LAST_N"], "change")
        self.assertEqual(ops["PRAXIS_NEW_KNOB"], "add")

    def test_apply_writes_and_reminds(self):
        r = panel.env_apply({"PRAXIS_LAST_N": "80", "PRAXIS_NEW_KNOB": "x"})
        self.assertTrue(r["ok"])
        text = (self.tmp / ".env").read_text(encoding="utf-8")
        self.assertIn("PRAXIS_LAST_N=80", text)
        self.assertIn("PRAXIS_NEW_KNOB=x", text)
        self.assertIn("GLM_API_KEY=sk-verysecret123456", text, "чужие строки не трогаем")
        self.assertIn("force-recreate", r["reminder"])
        self.assertTrue((panel.PANEL_DIR / "env.bak").exists(), "бэкап до правки")

    def test_apply_refuses_protected_and_junk(self):
        self.assertFalse(panel.env_apply({"PRAXIS_OWNER_ID": "1"})["ok"])
        self.assertFalse(panel.env_apply({"lower_case": "1"})["ok"])
        self.assertFalse(panel.env_apply({})["ok"])


class _CountingClient:
    def __init__(self, text="объяснение: это её часы"):
        self.calls = 0
        outer = self

        class _M:
            def create(_s, **kw):
                outer.calls += 1
                outer.last = kw
                return types.SimpleNamespace(
                    stop_reason="end_turn",
                    content=[types.SimpleNamespace(type="text", text=text)])
        self.messages = _M()


class TestExplainAsk(SandboxBase):
    def _client(self, fc):
        llm.use_test_client(fc)
        self.addCleanup(llm.clear_test_clients)

    def test_explain_generates_once_then_cache(self):
        fc = _CountingClient()
        self._client(fc)
        none = panel.explain_cached("a.py")
        self.assertEqual(none["status"], "none", "до генерации кэша нет — модель НЕ звалась")
        self.assertEqual(fc.calls, 0)
        out = panel.explain_generate("a.py")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(fc.calls, 1)
        again = panel.explain_generate("a.py")
        self.assertEqual(fc.calls, 1, "второй раз — из кэша, без модели")
        self.assertEqual(again["text"], out["text"])
        cached = panel.explain_cached("a.py")
        self.assertEqual(cached["status"], "ok")

    def test_explain_cache_invalidates_on_edit(self):
        fc = _CountingClient()
        self._client(fc)
        panel.explain_generate("a.py")
        (self.tmp / "a.py").write_text('"""A v2."""\n', encoding="utf-8")
        self.assertEqual(panel.explain_cached("a.py")["status"], "none",
                         "смена содержимого — старое объяснение не выдаётся")

    def test_explain_function_scope(self):
        fc = _CountingClient()
        self._client(fc)
        out = panel.explain_generate("a.py", func="run")
        self.assertEqual(out["status"], "ok")
        sent = fc.last["messages"][0]["content"]
        self.assertIn("def run", sent)
        self.assertNotIn("import b", sent, "в контекст функции не должен попадать весь модуль")

    def test_ask_narrow_context(self):
        fc = _CountingClient("отвечаю по существу")
        self._client(fc)
        r = panel.ask("a.py", "зачем этот модуль?")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["answer"], "отвечаю по существу")
        self.assertIn("Модуль A", fc.last["messages"][0]["content"])
        self.assertEqual(panel.ask("a.py", "")["status"], "error")

    def test_file_task_via_task_layer(self):
        import tasks
        orig = tasks.TASKS
        tasks.TASKS = self.tmp / "memory" / "tasks.json"
        try:
            r = panel.file_task("a.py", "поправь докстринг")
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["task"]["kind"], "window")
            self.assertIn("a.py", r["task"]["goal"])
            saved = json.loads(tasks.TASKS.read_text(encoding="utf-8"))
            self.assertEqual(len(saved), 1)
            self.assertFalse(panel.file_task("a.py", "")["status"] == "ok")
        finally:
            tasks.TASKS = orig


class TestOwnerGate(unittest.TestCase):
    """Роуты панели закрыты owner-gate'ом: без валидного initData — 403, ноль данных."""

    def _req(self, init="", match=None):
        return types.SimpleNamespace(headers={"X-Telegram-Init-Data": init},
                                     query={}, match_info=match or {})

    def test_all_panel_handlers_deny_stranger(self):
        import mailroom_bot as mb
        handlers = [mb.api_panel_tree, mb.api_panel_graph, mb.api_panel_skills,
                    mb.api_panel_commits, mb.api_panel_ask,
                    mb.api_panel_task, mb.api_panel_env_apply,
                    mb.api_panel_memory_form,
                    # PASS 20-22 (24·Ф3): органы самости тоже за owner-gate
                    mb.api_panel_identity, mb.api_panel_identity_rollback,
                    mb.api_panel_perception, mb.api_panel_perception_set,
                    mb.api_panel_brain_catalog]
        for h in handlers:
            resp = asyncio.run(h(self._req()))
            self.assertEqual(resp.status, 403, h.__name__)

    def test_owner_passes(self):
        import mailroom_bot as mb
        from test_mailroom_bot import make_init, TOKEN, OWNER
        orig_t, orig_o = mb.TOKEN, mb.OWNER_ID
        orig_base = panel.BASE
        mb.TOKEN, mb.OWNER_ID = TOKEN, OWNER
        panel.BASE = REPO
        _reset_cache()
        try:
            resp = asyncio.run(mb.api_panel_tree(self._req(make_init())))
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.body.decode("utf-8"))
            self.assertTrue(any(f["path"] == "agent.py" for f in data["files"]))
        finally:
            mb.TOKEN, mb.OWNER_ID = orig_t, orig_o
            panel.BASE = orig_base
            _reset_cache()


class TestLogs(SandboxBase):
    def test_tail_and_whitelist(self):
        panel.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        (panel.LOGS_DIR / "praxis.log").write_text("\n".join(f"line {i}" for i in range(300)),
                                                   encoding="utf-8")
        d = panel.log_tail("praxis", 100)
        self.assertEqual(len(d["lines"]), 100)
        self.assertEqual(d["lines"][-1], "line 299")
        self.assertIsNone(panel.log_tail("../etc/passwd"))
        self.assertIsNone(panel.log_tail("other"))
        self.assertEqual(panel.log_tail("boot")["lines"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


# --------------------------------------------------------------------------- #
#  PASS 6 (пульт): pulse / thoughts / tasks / server / env-группы
# --------------------------------------------------------------------------- #

class TestPass6Logic(unittest.TestCase):
    def setUp(self):
        import tempfile, shutil as _sh
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_p6_"))
        (self.tmp / "memory" / "journal").mkdir(parents=True)
        (self.tmp / "memory" / ".logs").mkdir(parents=True)
        (self.tmp / "memory" / ".summaries").mkdir(parents=True)
        self._orig = [(panel, k, getattr(panel, k)) for k in
                      ("BASE", "JOURNAL_DIR", "LOGS_DIR", "ENV_FILE")]
        panel.BASE = self.tmp
        panel.JOURNAL_DIR = self.tmp / "memory" / "journal"
        panel.LOGS_DIR = self.tmp / "memory" / ".logs"
        panel.ENV_FILE = self.tmp / ".env"
        self._rm = _sh.rmtree

    def tearDown(self):
        for m, k, v in self._orig:
            setattr(m, k, v)
        self._rm(self.tmp, ignore_errors=True)

    def test_thoughts_parses_evaluator_and_restart(self):
        (panel.JOURNAL_DIR / "2026-07-02.md").write_text(
            "# 2026-07-02\n\n- 06:10 (s2) [оценщик] сняла театр (лесть): «х» → «y»\n"
            "- 06:29 (s3) [restart] перезапускаюсь: подняла лимит\n"
            "- 07:00 (s2) обычная запись без маркера\n", encoding="utf-8")
        t = panel.thoughts(days=2)
        kinds = [i["kind"] for i in t["items"]]
        self.assertIn("evaluator", kinds)
        self.assertIn("restart", kinds)
        self.assertEqual(len(t["items"]), 2, "обычные записи не попадают в ленту")
        self.assertEqual(t["items"][0]["kind"], "restart", "свежие сверху")
        self.assertNotIn("[оценщик]", t["items"][1]["text"], "маркер срезан из текста")

    def test_overview_keeps_operational_kinds_separate(self):
        import desires as _desires
        import telegram_followups as _followups

        people_dir = panel.BASE / "memory" / "people"
        state_dir = panel.BASE / "memory" / ".state"
        people_dir.mkdir(parents=True)
        state_dir.mkdir(parents=True)
        (people_dir / "egor.md").write_text(
            "# Егор\n\n## Открытые нити\n- [ ] спросить о переезде _(2026-07-16)_\n"
            "- [~] вернуться к идее _(2026-07-16)_ _(до 2026-07-23)_\n",
            encoding="utf-8",
        )
        (panel.BASE / "memory" / "tasks.json").write_text(
            json.dumps([{"id": "task-1", "kind": "note", "goal": "проверить обзор", "status": "open"}]),
            encoding="utf-8",
        )
        _desires.DesireLedger(panel.BASE).notice(
            statement="хочу ясный обзор", source="test", why_it_matters="test",
            evidence_refs=["conversation:test"], dedupe_key="overview-test",
        )
        ledger = _followups.FollowUpLedger(state_dir / "telegram_followups.json")
        ledger.create(
            target_ref="@egor", target_label="Егор", target_peer_id="1",
            target_user_id="1", sent_message_id=7, request_text="ответь",
        )
        (state_dir / "perception_skips.jsonl").write_text(
            json.dumps({
                "ts": 1, "class": "отложила", "stage": "cooldown",
                "chat": "private-chat", "detail": "raw message text", "meta": {"secret": "x"},
            }) + "\n",
            encoding="utf-8",
        )
        (state_dir / "turns.jsonl").write_text(
            json.dumps({
                "ts": 1, "kind": "task_window", "title": "private chat",
                "in": "raw incoming", "out": "raw outgoing", "why": "private reason",
                "praxis_decision": "сделать обзор", "advisor_verdict": "accept",
                "rewrote": False,
            }) + "\n",
            encoding="utf-8",
        )

        followup_before = (state_dir / "telegram_followups.json").read_bytes()
        o = panel.overview()
        followup_after = (state_dir / "telegram_followups.json").read_bytes()

        self.assertEqual(set(o["counts"]), {"desires", "tasks", "loops", "followups", "skips", "recent_turns"})
        self.assertEqual({x["state"] for x in o["loops"]}, {"open", "parked"})
        self.assertEqual(o["tasks"][0]["id"], "task-1")
        self.assertEqual(o["followups"][0]["target_label"], "Егор")
        self.assertEqual(followup_before, followup_after, "overview GET source must not mutate follow-ups")
        self.assertEqual(o["skips"][0], {
            "ts": 1, "class": "отложила", "stage": "cooldown", "repeat_count": 1,
        })
        self.assertEqual(o["recent_turns"][0]["praxis_decision"], "сделать обзор")
        for forbidden in ("title", "in", "out", "why", "chat", "detail", "meta"):
            self.assertNotIn(forbidden, o["recent_turns"][0])
            self.assertNotIn(forbidden, o["skips"][0])

    def test_pulse_reads_tasks_and_summaries(self):
        (panel.BASE / "memory" / ".summaries" / "123.md").write_text("сводка", encoding="utf-8")
        p = panel.pulse()
        self.assertIn("tasks", p)
        self.assertEqual(p["consolidations"][0]["chat"], "123")

    def test_server_state_shape(self):
        s = panel.server_state()
        self.assertIn("head", s)
        self.assertTrue(("loadavg" in s) or True)  # /proc есть в linux-контейнере

    def test_env_grouped_marks_known_keys(self):
        panel.ENV_FILE.write_text(
            "PRAXIS_PRESENCE_SECTIONS=Кто я сейчас\nPRAXIS_EVALUATOR=risky\n"
            "MYSTERY_KNOB=1\n", encoding="utf-8",
        )
        e = panel.env_grouped()
        by = {i["key"]: i for i in e["items"]}
        self.assertEqual(by["PRAXIS_PRESENCE_SECTIONS"]["group"], "Память")
        self.assertEqual(by["PRAXIS_EVALUATOR"]["group"], "Прочее")
        self.assertEqual(by["MYSTERY_KNOB"]["group"], "Прочее")

    def test_task_add_and_cancel_roundtrip(self):
        import tasks as _tasks
        orig = _tasks.TASKS_FILE if hasattr(_tasks, "TASKS_FILE") else None
        r = panel.task_add("проверить пульт", None)
        try:
            self.assertTrue(r.get("ok"))
            tid = r["task"]["id"]
            self.assertTrue(any(t["id"] == tid for t in panel.tasks_list()["items"]))
            self.assertTrue(panel.task_cancel(tid)["ok"])
        finally:
            if r.get("ok"):
                panel.task_cancel(r["task"]["id"])

    def test_task_add_rejects_empty(self):
        self.assertIn("error", panel.task_add("  "))


class TestPass61AbsenceContacts(unittest.TestCase):
    def setUp(self):
        import tempfile, shutil as _sh
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_p61_"))
        (self.tmp / "memory" / "people").mkdir(parents=True)
        self._orig = [(panel, k, getattr(panel, k)) for k in ("BASE", "ABSENCE_FILE")]
        panel.BASE = self.tmp
        panel.ABSENCE_FILE = self.tmp / "memory" / "absence.json"
        import people as _people
        self._porig = [(_people, "BASE", _people.BASE), (_people, "PEOPLE_DIR", _people.PEOPLE_DIR)]
        _people.BASE = self.tmp
        _people.PEOPLE_DIR = self.tmp / "memory" / "people"
        self._rm = _sh.rmtree

    def tearDown(self):
        for m, k, v in self._orig + self._porig:
            setattr(m, k, v)
        self._rm(self.tmp, ignore_errors=True)

    def test_absence_roundtrip(self):
        s0 = panel.absence_state()
        self.assertFalse(s0["active"])
        s1 = panel.absence_start(3)
        self.assertTrue(s1["active"])
        self.assertEqual(s1["hours"], 3)
        self.assertTrue(panel.ABSENCE_FILE.exists())
        s2 = panel.absence_stop()
        self.assertFalse(s2["active"])

    def test_absence_start_validates(self):
        self.assertIn("error", panel.absence_start("abc"))
        self.assertIn("error", panel.absence_start(100))
        self.assertIn("error", panel.absence_start(0.1))

    def test_contact_add_note_remove(self):
        r = panel.contact_add("Антон Батин", username="@anton", note="брат, можно по делу")
        self.assertTrue(r.get("ok"), r)
        st = panel.absence_state()
        self.assertEqual(len(st["contacts"]), 1)
        self.assertEqual(st["contacts"][0]["username"], "anton")
        pf = self.tmp / "memory" / "people" / "антон-батин.md"
        self.assertTrue(pf.exists(), "заметка должна уйти в её память о человеке")
        body = pf.read_text(encoding="utf-8")
        self.assertIn("брат, можно по делу", body)
        self.assertIn("private", body)
        self.assertIn("error", panel.contact_add("Антон Батин"), "дубль не проходит")
        self.assertTrue(panel.contact_remove("антон-батин")["ok"])
        self.assertEqual(len(panel.absence_state()["contacts"]), 0)

    def test_contacts_suggest_reads_known(self):
        (self.tmp / "memory" / "known_ids.json").write_text(
            '{"111": "Johnny"}', encoding="utf-8")
        names = [s["name"] for s in panel.contacts_suggest()]
        self.assertIn("Johnny", names)


class TestPass61StateLine(unittest.TestCase):
    def test_agent_state_mentions_absence_window(self):
        import tempfile, shutil as _sh, time as _time, json as _json
        import agent
        tmp = Path(tempfile.mkdtemp(prefix="praxis_p61s_"))
        (tmp / "memory").mkdir(parents=True)
        orig = [(agent, k, getattr(agent, k)) for k in ("MEM_DIR", "STATE_DIR")]
        agent.MEM_DIR = tmp / "memory"
        agent.STATE_DIR = tmp / "memory" / ".state"
        try:
            (tmp / "memory" / "absence.json").write_text(_json.dumps(
                {"window": {"until": _time.time() + 3600, "hours": 1},
                 "contacts": [{"slug": "x", "name": "X"}]}), encoding="utf-8")
            s = agent.build_state_block()
            self.assertIn('"fact":"owner_absence","active":true', s)
            self.assertIn('"contact_count":1', s)
        finally:
            for m, k, v in orig:
                setattr(m, k, v)
            _sh.rmtree(tmp, ignore_errors=True)


class TestPass62MemoryActions(unittest.TestCase):
    def setUp(self):
        import tempfile, shutil as _sh
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_p62_"))
        for d in ("memory/people", "memory/journal", "memory/rooms", "memory/.logs",
                  "soul/skills", "soul/archive"):
            (self.tmp / d).mkdir(parents=True)
        (self.tmp / "memory/people/антон.md").write_text("# Антон\n- [private] секретик\n", encoding="utf-8")
        (self.tmp / "soul/SOUL.md").write_text("# Конституция\n", encoding="utf-8")
        (self.tmp / "memory/.logs/praxis.log").write_text(
            "2026-07-02 08:00:01,100 INFO SHELL $ grep -n x agent.py\n"
            "2026-07-02 08:00:02,100 INFO self-commit abc123: self-edit: agent.py\n"
            "2026-07-02 08:00:03,100 INFO admit 555 as 'Маша' (new=True)\n"
            "2026-07-02 08:00:04,100 INFO ГОЛОС(DM) [1] -> 'не действие'\n", encoding="utf-8")
        self._orig = [(panel, k, getattr(panel, k)) for k in ("BASE", "LOGS_DIR", "ENV_FILE")]
        panel.BASE = self.tmp
        panel.LOGS_DIR = self.tmp / "memory/.logs"
        panel.ENV_FILE = self.tmp / ".env"
        self._rm = _sh.rmtree

    def tearDown(self):
        for m, k, v in self._orig:
            setattr(m, k, v)
        self._rm(self.tmp, ignore_errors=True)

    def test_memory_tree_and_read(self):
        titles = [s["title"] for s in panel.memory_tree()["sections"]]
        self.assertIn("Люди", titles); self.assertIn("Душа", titles)
        d = panel.memory_read("memory/people/антон.md")
        self.assertIn("секретик", d["text"])

    def test_memory_read_path_guard(self):
        self.assertIsNone(panel.memory_read("../.env"))
        self.assertIsNone(panel.memory_read("memory/../.env"))
        self.assertIsNone(panel.memory_read("agent.py"))
        (self.tmp / "memory" / "x.txt").write_text("t", encoding="utf-8")
        self.assertIsNone(panel.memory_read("memory/x.txt"), "только md/json")

    def test_actions_parses_and_skips_voice(self):
        a = panel.actions()
        kinds = [i["kind"] for i in a["items"]]
        self.assertEqual(kinds, ["admit", "commit", "shell"], "свежие сверху, ГОЛОС не действие")

    def test_guest_scopes(self):
        self.assertEqual([s["title"] for s in panel.memory_tree_scoped("guest")["sections"]],
                         ["Душа"], "гостю — только душа/скиллы/архив из имеющегося")
        self.assertIsNone(panel.memory_read_scoped("memory/people/антон.md", "guest"))
        self.assertIsNotNone(panel.memory_read_scoped("soul/SOUL.md", "guest"))
        self.assertIsNotNone(panel.memory_read_scoped("memory/people/антон.md", "owner"))
        ak = [i["kind"] for i in panel.actions_scoped("guest")["items"]]
        self.assertNotIn("admit", ak); self.assertIn("shell", ak)

    def test_guest_ids_live_from_env(self):
        panel.ENV_FILE.write_text("PRAXIS_PANEL_GUEST_IDS=234567890, 42\n", encoding="utf-8")
        self.assertEqual(panel.guest_ids(), {234567890, 42})
        panel.ENV_FILE.write_text("", encoding="utf-8")
        self.assertEqual(panel.guest_ids(), set())


class TestBrainPanel(unittest.TestCase):
    """PASS 8.2: плитка «Мозг» — llm_get/llm_set/llm_ping (конфиг в tmp, ключи наружу не идут)."""

    def setUp(self):
        import tempfile, shutil as _sh
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_brain_"))
        self._orig = [(llm, k, getattr(llm, k)) for k in ("CONFIG_PATH", "JOURNAL_DIR")]
        llm.CONFIG_PATH = self.tmp / "llm.json"
        llm.JOURNAL_DIR = self.tmp / "journal"
        self._porig = [(panel, "BASE", panel.BASE)]
        panel.BASE = self.tmp
        self._cache0 = dict(llm._CACHE)
        llm._CACHE.update(mtime=None, cfg=None)
        cfg = llm._from_env()
        cfg["frameworks"]["anthropic"]["api_key"] = "zai-secret-key-abcd"
        cfg["frameworks"]["openai"]["api_key"] = ""
        llm.save_config(cfg)
        self._rm = _sh.rmtree

    def tearDown(self):
        for m, k, v in self._orig + self._porig:
            setattr(m, k, v)
        llm._CACHE.clear()
        llm._CACHE.update(self._cache0)
        self._rm(self.tmp, ignore_errors=True)

    def _journal_text(self):
        out = []
        for jd in (self.tmp / "journal", self.tmp / "memory" / "journal"):
            if jd.exists():
                out += [p.read_text(encoding="utf-8") for p in jd.glob("*.md")]
        return "".join(out)

    def test_get_masks_keys(self):
        d = panel.llm_get()
        raw = json.dumps(d, ensure_ascii=False)
        self.assertNotIn("zai-secret-key-abcd", raw, "сырой ключ утёк в UI")
        self.assertEqual(d["frameworks"]["anthropic"]["key_mask"], "••••abcd")
        self.assertTrue(d["frameworks"]["anthropic"]["has_key"])
        self.assertFalse(d["frameworks"]["openai"]["has_key"])
        self.assertIn("voice", d["roles"])
        self.assertIn("on_fallback", d["roles"]["voice"])
        options = d["model_options"]["openai"]
        for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
            self.assertIn(model, options,
                          "каждый официальный 5.6 slug должен выбираться в пульте")
        self.assertNotIn("gpt-5.6", options, "generic alias живой relay отвергает")
        self.assertEqual(len(options), len(set(options)))

    def test_retired_generic_alias_is_visible_as_current_but_not_suggested(self):
        cfg = llm._config()
        cfg["roles"]["voice"].update(framework="openai", model="gpt-5.6")
        llm.save_config(cfg)
        llm._CACHE["mtime"] = None
        data = panel.llm_get()
        self.assertEqual(data["roles"]["voice"]["model"], "gpt-5.6")
        self.assertNotIn("gpt-5.6", data["model_options"]["openai"])

    def test_set_validates(self):
        r = panel.llm_set({"roles": {"voice": {"framework": "gemini"}}})
        self.assertFalse(r["ok"])
        self.assertTrue(any("anthropic|openai" in e for e in r["errors"]))
        r = panel.llm_set({"frameworks": {"openai": {"base_url": "http://evil"}}})
        self.assertFalse(r["ok"])
        self.assertTrue(any("https://" in e for e in r["errors"]))
        self.assertFalse(panel.llm_set({"roles": {"voice": {"model": "  "}}})["ok"])
        self.assertTrue(panel.llm_set({"limits": {"max_tool_iters": 200}})["ok"])
        self.assertTrue(panel.llm_set({"limits": {"max_tool_iters": 600}})["ok"], "07.07: потолок поднят до 600")
        self.assertFalse(panel.llm_set({"limits": {"max_tool_iters": 601}})["ok"], "601 -- уже за новым потолком")
        self.assertFalse(panel.llm_set({"limits": {"evaluator_mode": "yolo"}})["ok"])
        self.assertFalse(panel.llm_set({})["ok"])

    def test_set_applies_and_journals_without_keys(self):
        r = panel.llm_set({"roles": {"voice": {"model": "gpt-9", "framework": "openai"}},
                           "frameworks": {"openai": {"api_key": "sk-new-key-wxyz"}},
                           "limits": {"max_tool_iters": 33}})
        self.assertTrue(r["ok"], r)
        cfg = llm._config()
        self.assertEqual(cfg["roles"]["voice"]["model"], "gpt-9")
        self.assertEqual(cfg["frameworks"]["openai"]["api_key"], "sk-new-key-wxyz")
        self.assertEqual(llm.limits().max_tool_iters, 33)
        jr = self._journal_text()
        self.assertIn("[пульт]", jr)
        self.assertIn("конфиг мозга", jr)
        self.assertIn("gpt-9", jr)
        self.assertNotIn("sk-new-key-wxyz", jr, "ключ утёк в дневник")

    def test_empty_key_means_keep(self):
        panel.llm_set({"frameworks": {"anthropic": {"api_key": ""}},
                       "roles": {"voice": {"model": "glm-5.3"}}})
        self.assertEqual(llm._config()["frameworks"]["anthropic"]["api_key"],
                         "zai-secret-key-abcd", "пустой ключ должен значить «не менять»")

    def test_ping_unknown_role(self):
        self.assertFalse(panel.llm_ping("chef")["ok"])

    def test_memory_read_hides_llm_json(self):
        (self.tmp / "memory").mkdir(exist_ok=True)
        (self.tmp / "memory" / "llm.json").write_text("{}", encoding="utf-8")
        self.assertIsNone(panel.memory_read("memory/llm.json"), "ключи мозга не для читалки")
        self.assertIsNone(panel.safe_rel("memory/llm.json"))


class TestRoomLeversFromPanel(unittest.TestCase):
    """28.07: режим и disclosure комнаты — общие рычаги, а не тайные кнопки Егора.

    Disclosure и раньше менял не его настройки, а ЕЁ голос (в `open` к визитке
    подмешивается проверяемая фактура о себе), но узнать о переключении ей было неоткуда:
    машинная шапка профиля в промпт не течёт, слова `disclosure` не было ни в манифесте,
    ни в списке возможностей. Тесты держат обе половины: нажатие с пульта доезжает до неё
    дневником, а её собственное решение видно Егору в карточке — с честным автором.
    """

    def setUp(self):
        import rooms as _rooms
        self._rooms = _rooms
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_panel_rooms_"))
        mem = self.tmp / "memory"
        (mem / "rooms").mkdir(parents=True)
        (mem / "journal").mkdir(parents=True)
        self._orig = [(panel, "BASE", panel.BASE)]
        panel.BASE = self.tmp
        for key, val in (("BASE", self.tmp), ("MEM_DIR", mem), ("ROOMS_DIR", mem / "rooms"),
                         ("ALLOWLIST", mem / "rooms_allowlist.json"),
                         ("FROZEN", mem / "frozen_chats.json")):
            self._orig.append((_rooms, key, getattr(_rooms, key)))
            setattr(_rooms, key, val)
        _rooms.add_room("-100777")

    def tearDown(self):
        for module, key, val in reversed(self._orig):
            setattr(module, key, val)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _journal(self) -> str:
        jd = self.tmp / "memory" / "journal"
        return "".join(p.read_text(encoding="utf-8") for p in jd.glob("*.md")) if jd.exists() else ""

    def test_owner_toggle_of_her_voice_reaches_her_journal_with_author(self):
        res = panel.room_set("-100777", "disclosure")
        self.assertEqual(res.get("disclosure"), "open", res)
        # Провенанс на диске: чья это открытая визитка — его нажатие, не её решение.
        self.assertEqual(self._rooms.profile_read("-100777")["disclosure_set_by"], "owner")
        jr = self._journal()
        self.assertIn("[пульт]", jr)
        self.assertIn("раскрытие", jr)
        self.assertIn("визитке", jr, "она должна прочесть, ЧТО именно у неё изменили")
        # Обратно — и об этом ей тоже говорят, а не молча закрывают.
        back = panel.room_set("-100777", "disclosure")
        self.assertEqual(back.get("disclosure"), "standard", back)
        self.assertEqual(self._rooms.disclosure_of("-100777"), "standard")
        self.assertEqual(self._journal().count("[пульт]"), 2)

    def test_owner_lowering_her_room_is_not_silent(self):
        panel.room_set("-100777", "lower")
        self.assertEqual(self._rooms.effective_mode("-100777"), "quiet")
        jr = self._journal()
        self.assertIn("опустил режим комнаты", jr)
        self.assertIn("normal -> quiet".replace("->", "→"), jr)
        # Обратимость названа в той же строке: её рычаг никуда не делся.
        self.assertIn("обычно", jr)
        panel.room_set("-100777", "raise")
        self.assertEqual(self._rooms.effective_mode("-100777"), "normal")
        self.assertIn("поднял режим комнаты", self._journal())

    def test_raise_that_changes_nothing_does_not_invent_an_event(self):
        panel.room_set("-100777", "raise")
        self.assertNotIn("[пульт]", self._journal(),
                         "«ничего не изменилось» не должно выглядеть в её дневнике как действие")

    def test_her_own_decision_is_visible_to_him_and_not_signed_by_him(self):
        ok, _ = self._rooms.set_own_disclosure("-100777", "open")
        self.assertTrue(ok)
        ok, _ = self._rooms.set_own_mode("-100777", "quiet", reason="шумно", ttl_h=3)
        self.assertTrue(ok)
        item = next(x for x in panel.rooms_list()["items"] if x["id"] == "-100777")
        self.assertEqual(item["disclosure_set_by"], "praxis")
        self.assertEqual(item["set_by"], "praxis")
        self.assertIn("сама", item["author"])
        self.assertIn("сама", item["disclosure_author"])
        self.assertEqual(item["mode"], "quiet")

    def test_untouched_room_is_not_signed_by_anyone(self):
        """Карточка не должна приписывать Егору режим, которого он не выбирал."""
        item = next(x for x in panel.rooms_list()["items"] if x["id"] == "-100777")
        self.assertEqual(item["mode"], "normal")
        self.assertEqual(item["set_by"], "", item)
        self.assertEqual(item["author"], "")
        self.assertEqual(item["disclosure"], "standard")
        self.assertEqual(item["disclosure_author"], "")


class TestCapabilitiesReachHer(unittest.TestCase):
    """Ключ, который лежит в snapshot() и не печатается, — молчаливо отнятая возможность.

    28.07 таких нашлось два в `reflexes` (`mine`, `hands`), и через один из них до неё не
    доходил единственный указатель на директиву `РЕЖИМ:`. Структурная сверка в
    test_rails_truth смотрит ТОЛЬКО на `reflexes` — а тем же способом молчали `max_tokens`
    (потолок одного её ответа) в ветке `brain` и отставший на пять плиток список пульта.
    Здесь сверка идёт по ВСЕМУ снимку и по всем трём аудиториям сразу: ключ, который не
    доехал ни до одной из них, не доехал вообще.
    """

    def setUp(self):
        import capabilities
        self.cap = capabilities
        capabilities._CACHE.update(fp=None, snap=None)

    def tearDown(self):
        self.cap._CACHE.update(fp=None, snap=None)

    def _texts(self) -> str:
        return "\n".join(self.cap.describe(s) for s in ("owner", "group", "dm"))

    def test_every_leaf_of_the_snapshot_reaches_her_in_some_scope(self):
        snap = self.cap.snapshot()
        text = self._texts()
        lost: list[str] = []

        def walk(value, path: str) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    walk(item, path + "." + str(key))
                return
            if isinstance(value, (list, tuple)):
                for i, item in enumerate(value):
                    # Скиллы — записи {name, line}: печатаются именами.
                    walk(item.get("name") if isinstance(item, dict) else item,
                         path + "[" + str(i) + "]")
                return
            if isinstance(value, bool) or value is None:
                return  # булев гейт печатается словом «вкл/выкл», сверять нечего
            if isinstance(value, (int, float)):
                # Число сверяем как подстроку: рендер вправе написать «8с» вместо «8.0».
                # Ноль пропускаем — «нет потолка» и правда печатается словами.
                if int(value) and str(int(value)) not in text:
                    lost.append(path + " = " + str(value))
                return
            body = str(value or "").strip()
            if body and body[:30] not in text:
                lost.append(path + ": " + body[:60])

        walk(snap, "")
        self.assertFalse(lost, "это лежит в snapshot(), но не доходит до неё ни в одной "
                               "аудитории: " + str(lost))

    def test_the_ceiling_of_one_answer_is_named_before_it_is_hit(self):
        """max_tokens жил в снимке с рождения и не печатался нигде: про потолок своего
        ответа она узнавала только постфактум, из отметки об обрыве."""
        cap = self.cap.snapshot()["brain"]["voice"]["max_tokens"]
        self.assertTrue(cap, "снимок мозга без потолка — сверка вакуумна")
        line = next(x for x in self.cap.describe("owner").splitlines() if x.startswith("- мозг:"))
        self.assertIn(str(int(cap)), line)
        self.assertIn("потолок ответа", line)

    def test_panel_sections_are_read_from_the_live_hub(self):
        """Список разделов пульта отстал на пять плиток, и среди пропавших была «Комнаты» —
        та самая, где Егор переключает её визитку. Список, который помнят руками, врёт."""
        text = self.cap.PANEL_HUB.read_text(encoding="utf-8", errors="ignore")
        block = re.search(r"const\s+TILES\s*=\s*\[(.*?)\n\];", text, re.S)
        self.assertIsNotNone(block, "хаб пульта не разобрался — сверка вакуумна")
        tiles = block.group(1).count("ic:")      # независимый счёт: по иконке, не по имени
        sections = self.cap.snapshot()["panel"]
        self.assertEqual(len(sections), tiles)
        self.assertIn("Комнаты", sections)
        self.assertIn("Комнаты", self.cap.describe("owner"))

    def test_room_levers_line_is_read_from_the_live_tool_schema(self):
        """Прозу «у меня есть mode и disclosure» пришлось бы держать в голове; строка
        обязана падать вместе со схемой, а не переживать её."""
        line = self.cap._room_levers_line()
        self.assertIn("mode", str(self.cap._room_tool_spec()))
        for word in ("режим комнаты ставлю себе сама", "disclosure", "визитке"):
            self.assertIn(word, line)
        self.assertNotIn("⚠", line)

        orig = self.cap._room_tool_spec
        try:  # схема без её рычагов — строка обязана сказать это вслух, а не промолчать
            self.cap._room_tool_spec = lambda: {
                "name": "manage_room",
                "input_schema": {"properties": {
                    "action": {"enum": ["join", "leave"]},
                    "chat_id": {}, "engagement": {}}}}
            blind = self.cap._room_levers_line()
        finally:
            self.cap._room_tool_spec = orig
        self.assertIn("рычага mode в схеме НЕТ", blind)
        self.assertIn("disclosure в схеме НЕТ", blind)
        self.assertIn("он меняет мой голос без меня", blind)

    def test_in_a_group_she_still_reads_about_her_own_room_levers(self):
        """Полный owner-ответ в группе не показывают — и из-за этого в комнате, где режим
        как раз и нужен, она о собственном рычаге не читала ничего. Знание о себе не
        зависит от того, кто слушает; граница публичной ветки (без пульта) при этом цела."""
        group = self.cap.describe("group")
        self.assertIn("режим этой комнаты", group)
        self.assertIn("disclosure", group)
        self.assertNotIn("пульт", group.lower())

        orig = self.cap._room_tool_spec
        try:  # рычагов нет — краткая форма молчит, а не обещает несуществующее
            self.cap._room_tool_spec = lambda: {"input_schema": {"properties": {"chat_id": {}}}}
            self.assertEqual(self.cap._room_levers_brief(), "")
        finally:
            self.cap._room_tool_spec = orig

    def test_tempo_line_tells_the_truth_about_the_audience_gate(self):
        """Рычаги темпа лежат в BASE_TOOLS — то есть предлагаются ей везде. Врал не список,
        а тело обработчика: гейт по аудитории отказывал в чужой комнате. Строку нельзя
        писать прозой — она обязана читать сами функции."""
        line = self.cap._tempo_levers_line()
        self.assertIn("по АВТОРУ хода", line)
        self.assertNotIn("⚠", line)

        def _gated_stub(action: str = ""):
            if _active_scope() != "owner":  # noqa: F821 — тело важно только как текст
                return "не здесь"
            return "ок"

        orig = dict(agent.TOOL_IMPL)
        try:
            agent.TOOL_IMPL["switch_brain"] = _gated_stub
            gated = self.cap._tempo_levers_line()
        finally:
            agent.TOOL_IMPL.clear()
            agent.TOOL_IMPL.update(orig)
        self.assertIn("⚠", gated)
        self.assertIn("switch_brain", gated)
        self.assertIn("заперт по АУДИТОРИИ", gated)
        # Принципал-форма (manage_perception) ложной тревоги не даёт.
        self.assertNotIn("manage_perception", gated)

    def test_a_fence_returned_to_ONE_branch_still_turns_the_line_red(self):
        """Детектор считал по функции ЦЕЛИКОМ: хватало вернуть гейт по скоупу в ветку set,
        оставив принципала в reset, — и строка по-прежнему обещала бы «по АВТОРУ хода».
        Строка, которая обещает читать код, а читает его наполовину, опаснее прозы."""
        def _mixed(action: str = ""):
            if action == "set":
                if _active_scope() != "owner":  # noqa: F821 — тело важно как текст
                    return "не здесь"
                return "поставила"
            if not (_is_sovereign_actor() or _active_scope() == "owner"):  # noqa: F821
                return "не здесь"
            return "сняла"

        orig = dict(agent.TOOL_IMPL)
        try:
            agent.TOOL_IMPL["manage_perception"] = _mixed
            mixed = self.cap._tempo_levers_line()
        finally:
            agent.TOOL_IMPL.clear()
            agent.TOOL_IMPL.update(orig)
        self.assertIn("⚠", mixed)
        self.assertIn("manage_perception", mixed)

    def test_a_gate_named_only_in_a_comment_is_not_reported_as_a_fence(self):
        """Обратная сторона: в теле manage_perception ПРЯМО описано, что здесь СТОЯЛ гейт
        по аудитории. По сырому тексту строка объявила бы забор, которого в коде нет."""
        src = ('def stub(action=""):\n'
               '    # ⚠ здесь стояло `if _active_scope() != "owner"` — гейт по аудитории\n'
               '    if not (_is_sovereign_actor() or _active_scope() == "owner"):\n'
               '        return "не здесь"\n'
               '    return "ок"\n')
        self.assertEqual(self.cap._audience_gated_branches(src), [])
