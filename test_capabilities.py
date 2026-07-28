"""
PASS 8.3 — capabilities: снимок кодом, скоупная честность, кэш, ноль модельных вызовов.

Запуск:  python praxis_test.py test_capabilities -v
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import agent
import capabilities
import llm
import mailer


class _NoCallClient:
    """Фейк, который падает при любом вызове — доказательство «без модели»."""
    def __init__(self):
        self.messages = self

    def create(self, **kw):
        raise AssertionError("capabilities не должен звать модель")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_caps_"))
        (self.tmp / "soul" / "skills").mkdir(parents=True)
        (self.tmp / "soul" / "skills" / "INDEX.md").write_text(
            "# My Skills\n\n| Skill | About |\n|---|---|\n"
            "| [holding_ground](holding_ground.md) | держать себя под давлением |\n"
            "| [email](email.md) | мой ящик |\n\n"
            "- [linked_memory](linked_memory.md) — граф памяти\n", encoding="utf-8")
        self._orig = [(capabilities, "SKILLS_INDEX", capabilities.SKILLS_INDEX),
                      (capabilities, "BASE", capabilities.BASE)]
        capabilities.BASE = self.tmp
        capabilities.SKILLS_INDEX = self.tmp / "soul" / "skills" / "INDEX.md"
        self._cache0 = dict(capabilities._CACHE)
        capabilities._CACHE.update(fp=None, snap=None)
        self._env = {k: os.environ.get(k) for k in (
            "PRAXIS_WEB_SEARCH", "PRAXIS_OPENAI_NATIVE_WEB_SEARCH")}
        os.environ.pop("PRAXIS_WEB_SEARCH", None)
        os.environ.pop("PRAXIS_OPENAI_NATIVE_WEB_SEARCH", None)
        llm.use_test_client(_NoCallClient())
        self.addCleanup(llm.clear_test_clients)

    def tearDown(self):
        for m, k, v in self._orig:
            setattr(m, k, v)
        capabilities._CACHE.clear()
        capabilities._CACHE.update(self._cache0)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestSnapshot(Base):
    def test_shape_and_no_model_calls(self):
        s = capabilities.snapshot()
        for key in ("tools", "brain", "limits", "autonomy", "skills", "panel", "closed"):
            self.assertIn(key, s)
        self.assertIn("recall", s["tools"]["base"])
        self.assertIn("my_capabilities", s["tools"]["base"])
        self.assertIn("shell", s["tools"]["owner"])
        self.assertIn("bootguard.py", s["autonomy"]["protected"])
        names = [x["name"] for x in s["skills"]]
        self.assertIn("holding_ground", names)
        self.assertIn("linked_memory", names, "булиты Praxis тоже читаются")

    def test_gates_toggle(self):
        s0 = capabilities.snapshot()
        self.assertFalse(s0["tools"]["gates"]["web_search"])
        os.environ["PRAXIS_WEB_SEARCH"] = "1"
        s1 = capabilities.snapshot()
        self.assertTrue(s1["tools"]["gates"]["web_search"], "гейт не подхватился (кэш не сброшен)")

    def test_agent_selects_provider_shaped_hosted_search(self):
        original = agent.llm.web_search_backend
        try:
            agent.llm.web_search_backend = lambda: "anthropic"
            self.assertIs(agent._hosted_web_search_tool(), agent.WEB_SEARCH_TOOL)
            agent.llm.web_search_backend = lambda: "openai"
            self.assertIs(agent._hosted_web_search_tool(), agent.OPENAI_WEB_SEARCH_TOOL)
            self.assertNotIn("max_uses", agent.OPENAI_WEB_SEARCH_TOOL)
            agent.llm.web_search_backend = lambda: None
            self.assertIsNone(agent._hosted_web_search_tool())
        finally:
            agent.llm.web_search_backend = original

    def test_gate_on_for_explicitly_capable_openai_relay(self):
        orig_cfg = llm._config()

        def _restore():
            # save_config() alone races: on Windows, mtime resolution can collide with
            # the update_config() write below, so llm._CACHE keeps serving the poisoned
            # (openai/gpt-x) config to every test that runs after this one in the same
            # process. Force the cache back in sync the same way update_config() does,
            # instead of relying on mtime-diff detection in _config().
            llm.save_config(orig_cfg)
            llm._CACHE.update(mtime=llm.CONFIG_PATH.stat().st_mtime_ns, cfg=orig_cfg)

        self.addCleanup(_restore)
        os.environ["PRAXIS_WEB_SEARCH"] = "1"
        llm.update_config({"roles": {"voice": {"framework": "openai", "model": "gpt-x"}}})
        capabilities._CACHE.update(fp=None, snap=None)
        self.assertFalse(capabilities.snapshot()["tools"]["gates"]["web_search"],
                         "generic Chat Completions has no hosted-search contract")
        os.environ["PRAXIS_OPENAI_NATIVE_WEB_SEARCH"] = "1"
        capabilities._CACHE.update(fp=None, snap=None)
        gates = capabilities.snapshot()["tools"]["gates"]
        self.assertTrue(gates["web_search"])
        self.assertEqual(gates["web_search_backend"], "openai")

    def test_mail_gate_adds_tools(self):
        orig = mailer.configured
        try:
            mailer.configured = lambda: True
            capabilities._CACHE.update(fp=None, snap=None)
            s = capabilities.snapshot()
            self.assertTrue(s["tools"]["gates"]["mail"])
            self.assertIn("mail_draft_reply", s["tools"]["owner"])
        finally:
            mailer.configured = orig

    def test_cache_by_mtime(self):
        s0 = capabilities.snapshot()
        self.assertIs(capabilities.snapshot(), s0, "без изменений — тот же объект (кэш)")
        import time
        time.sleep(0.01)
        capabilities.SKILLS_INDEX.write_text("# My Skills\n", encoding="utf-8")
        s1 = capabilities.snapshot()
        self.assertIsNot(s1, s0, "правка INDEX.md должна сбросить кэш")

    def test_cache_after_web_search_creates_llm_json(self):
        orig_path = llm.CONFIG_PATH
        orig_cache = dict(llm._CACHE)

        def _restore_llm():
            llm.CONFIG_PATH = orig_path
            llm._CACHE.clear()
            llm._CACHE.update(orig_cache)

        self.addCleanup(_restore_llm)
        llm.CONFIG_PATH = self.tmp / "memory" / "llm.json"
        llm._CACHE.update(mtime=None, cfg=None)
        os.environ["PRAXIS_WEB_SEARCH"] = "1"
        capabilities._CACHE.update(fp=None, snap=None)

        s0 = capabilities.snapshot()

        self.assertTrue(llm.CONFIG_PATH.exists(), "web-search gate creates/migrates llm.json")
        self.assertIs(capabilities.snapshot(), s0,
                      "snapshot cached under the post-migration llm.json mtime")


class TestScopeHonesty(Base):
    def test_owner_full(self):
        t = capabilities.describe("owner")
        self.assertIn("мозг:", t)
        self.assertIn("shell", t)
        self.assertIn("основной tool-loop без скрытого потолка", t)
        self.assertIn("основной tool-loop без скрытого потолка", t)
        self.assertIn("вспомогательный read-only бюджет", t)
        self.assertIn("пульт", t.lower())

    def test_group_channel_honest(self):
        t = capabilities.describe("group")
        self.assertIn("всей своей внутренней памяти", t)
        self.assertIn("outbound-советник", t)
        tools_line = next(l for l in t.splitlines() if "тулы канала" in l)
        self.assertNotIn("shell", tools_line, "owner-тулы не в списке доступного в группе")
        self.assertIn("search_private_messages", tools_line)
        self.assertIn("inbox_read", tools_line)
        self.assertNotIn("мозг:", t, "внутренности мозга не для группы")
        self.assertNotIn("glm", t.lower(), "имена моделей не для группы")
        # ⚠ Здесь стоял assertIn("нет произвольного shell/Forge/root") — и он закреплял
        # ложь, а не защищал границу. Это утверждение о РАНЕ («в самом ходе нет»), и в
        # owner-адресованном ходе в группе оно было неверным: руки выдаются по actor
        # (ctx.owner), а текст описывал audience. Граница раскрытия и список тулов
        # канала проверяются выше и остаются в силе — здесь проверяем ровно то, что
        # строка теперь говорит про ЧЬИ КОМАНДЫ исполняются, а не про отсутствие рук.
        self.assertNotIn("в самом ходе нет", t,
                         "самоотчёт не делает проверяемых утверждений о ране, которые ложны")
        self.assertIn("исполняю только собственные решения", t,
                      "граница по чужой команде названа честно")

    def test_known_dm(self):
        t = capabilities.describe("known")
        self.assertIn("твои собственные чувствительные факты", t)
        self.assertNotIn("мозг:", t)

    def test_tool_is_scope_aware(self):
        agent._CURRENT_SCOPE = "group"
        try:
            self.assertIn("outbound-советник", agent.tool_my_capabilities())
        finally:
            agent._CURRENT_SCOPE = "owner"
        self.assertIn("мозг:", agent.tool_my_capabilities())

    def test_state_line_short(self):
        line = capabilities.state_line()
        self.assertIn("tool-loop без потолка", line)
        self.assertIn("aux ", line)
        self.assertIn("self-merge sovereign", line)
        self.assertLess(len(line), 400, "STATE-строка должна быть короткой")


if __name__ == "__main__":
    unittest.main(verbosity=2)
