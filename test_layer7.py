"""
Тесты Слоя 7: эмбеддинги-off, web_search-гейт, telethon-тулы, оценщик. Герметичны.

Запуск:  python praxis_test.py test_layer7 -v
"""

from __future__ import annotations

import os
import shutil
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

import memory_index as mi  # noqa: E402
import people as pe  # noqa: E402
import agent  # noqa: E402
import llm  # noqa: E402


class FakeResp:
    def __init__(self, text):
        self.stop_reason = "end_turn"
        self.content = [types.SimpleNamespace(type="text", text=text)]


class ScriptedClient:
    def __init__(self, replies):
        self._r = list(replies)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return FakeResp(self._r.pop(0) if self._r else "")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_l7_"))
        mem = self.tmp / "memory"
        soul = self.tmp / "soul"
        for d in (mem / "people", mem / "rooms", mem / "journal", mem / ".vectors", soul / "skills"):
            d.mkdir(parents=True, exist_ok=True)
        (soul / "SOUL.md").write_text("# Конституция\n\n## Кто я по характеру\nтёплая.\n", encoding="utf-8")
        for n in ("self", "emotions", "being_with"):
            (soul / f"{n}.md").write_text(f"# {n}\n", encoding="utf-8")
        self._orig = []
        patch = {
            agent: dict(BASE=self.tmp, SOUL_DIR=soul, SKILLS_DIR=soul / "skills", MEM_DIR=mem,
                        PEOPLE_DIR=mem / "people", ROOMS_DIR=mem / "rooms",
                        JOURNAL_DIR=mem / "journal", REFLECTIONS=mem / "reflections.md",
                        INDEX_MD=mem / "INDEX.md", _TELETHON={}),
            pe: dict(BASE=self.tmp, PEOPLE_DIR=mem / "people"),
            mi: dict(BASE=self.tmp, MEM_DIR=mem, SOUL_DIR=soul, SKILLS_DIR=soul / "skills",
                     PEOPLE_DIR=mem / "people", VECTORS_DIR=mem / ".vectors",
                     INDEX_JSON=mem / ".vectors" / "index.json", INDEX_MD=mem / "INDEX.md"),
        }
        for module, attrs in patch.items():
            for k, val in attrs.items():
                self._orig.append((module, k, getattr(module, k)))
                setattr(module, k, val)
        mi._CACHE = {"mtime": None, "index": None}
        self._env = {k: os.environ.get(k) for k in
                     ("PRAXIS_EMBEDDINGS", "PRAXIS_WEB_SEARCH")}
        for k in self._env:
            os.environ.pop(k, None)
        # PASS 8.0: канал к моделям — через llm; фейки ставим в его тестовый шов
        llm.clear_test_clients()
        self.addCleanup(llm.clear_test_clients)

    def offer_test_tool(self, name, *, properties=None, required=()):
        """Add a hermetic schema as well as a fake implementation.

        The durable loop deliberately rejects tool calls that were not offered to the
        model.  Tests that inject TOOL_IMPL entries must therefore extend the matching
        owner-visible schema set instead of relying on the old implementation-only seam.
        """
        self._orig.append((agent, "OWNER_TOOLS", agent.OWNER_TOOLS))
        agent.OWNER_TOOLS = list(agent.OWNER_TOOLS) + [{
            "name": name,
            "description": "test-only tool",
            "input_schema": {
                "type": "object",
                "properties": dict(properties or {}),
                "required": list(required),
            },
        }]

    def tearDown(self):
        for module, k, val in reversed(self._orig):
            setattr(module, k, val)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestEmbeddingsOff(Base):
    def test_search_keyword_without_embed(self):
        called = []
        self._orig.append((mi, "embed", mi.embed))
        mi.embed = lambda t: called.append(1) or [0.0]
        pe.append_fact("егор", "Егор", "любит горы и кофе")
        res = mi.search("горы", k=5)  # PRAXIS_EMBEDDINGS не задан → off
        self.assertEqual(called, [], "embed не должен вызываться при выключенных эмбеддингах")
        self.assertTrue(any("горы" in r["text"].lower() for r in res), "keyword не нашёл")

    def test_build_noop_when_off(self):
        idx = mi.build()
        self.assertEqual(idx.get("records"), [], "build не должен индексировать при off")


class TestWebSearchGate(Base):
    def _tools(self):
        sc = ScriptedClient(["ответ"])
        llm.use_test_client(sc)
        agent.voice_turn("777", "егор: что нового по llm?", speaker="Егор", is_owner=True)
        return [t.get("name") or t.get("type") for t in sc.calls[0].get("tools", [])]

    def test_off_by_default(self):
        self.assertNotIn("web_search", self._tools())

    def test_on_adds_server_tool(self):
        os.environ["PRAXIS_WEB_SEARCH"] = "1"
        names = self._tools()
        self.assertTrue(any("web_search" in str(n) for n in names), "web_search не добавлен при вкл")


class TestToolDispatchNoneFilter(Base):
    def test_none_valued_optional_args_stripped_before_calling_impl(self):
        # 400 invalid_function_parameters на remember (tools[1]) чинили обёрткой опциональных
        # полей в anyOf[<схема>, null] — openai strict-mode теперь шлёт явный null вместо пропуска
        # ключа. Дисптетч в _voice обязан снять эти None перед вызовом тула — иначе тул получит
        # kwarg=None там, где ждал дефолт (или упадёт, если типизирован без Optional).
        received = {}

        def fake_tool(required_field, optional_field="default"):
            received["required_field"] = required_field
            received["optional_field"] = optional_field
            return "ok"

        self._orig.append((agent, "TOOL_IMPL", dict(agent.TOOL_IMPL)))
        agent.TOOL_IMPL["fake_tool"] = fake_tool
        self.offer_test_tool(
            "fake_tool",
            properties={
                "required_field": {"type": "string"},
                "optional_field": {"type": "string"},
            },
            required=("required_field",),
        )

        class _Client:
            def __init__(self):
                self._step = True
                self.messages = self

            def create(_s, **kw):
                if _s._step:
                    _s._step = False
                    blk = types.SimpleNamespace(
                        type="tool_use", id="t1", name="fake_tool",
                        input={"required_field": "x", "optional_field": None})
                    return types.SimpleNamespace(
                        stop_reason="tool_use", content=[blk],
                        usage=types.SimpleNamespace(input_tokens=5, output_tokens=1))
                return FakeResp("готово")

        llm.use_test_client(_Client())
        agent.voice_turn(None, "Егор: тест", speaker="Егор", is_owner=True)
        self.assertEqual(received.get("required_field"), "x")
        self.assertEqual(received.get("optional_field"), "default",
                         "None (openai strict-mode вместо пропуска необязательного поля) "
                         "не должен долетать до тула — дефолт функции")


class TestTelethonTools(Base):
    def test_unavailable_without_hook(self):
        self.assertIn("недоступно", agent.tool_get_id("Маша").lower())

    def test_delegates_to_hook(self):
        agent._TELETHON["get_id"] = lambda x: 12345
        self.assertEqual(agent.tool_get_id("Маша"), "12345")
        agent._TELETHON["search_chats"] = lambda q: "Маша: 12345"
        self.assertIn("12345", agent.tool_search_chats("маша"))


class TestDataAuthorityAdvisor(Base):
    def test_private_audience_needs_no_advisor(self):
        llm.use_test_client(ScriptedClient(["PRIVACY_HOLD_CROSS_PERSON"]))
        self.assertEqual(
            agent.evaluate_reply("какой-то ответ", audience_accepts_private=True),
            ("ok", ""),
        )

    def test_exact_cross_person_code_is_advice_not_a_hold(self):
        # Раньше здесь стоял assertEqual(verdict, "deny"): cross-person лежал в
        # _PRIVATE_DM_PRIVACY_HOLDS и останавливал её слово. 27.07 решением Егора словарь
        # расщеплён — стоп остался только за механическим кред-полом, мнение судьи о
        # приватности стало советом. Причина не изменилась: код тот же, последствие другое.
        llm.use_test_client(ScriptedClient(["PRIVACY_HOLD_CROSS_PERSON"]))
        verdict, reason = agent.evaluate_reply(
            "вот секрет Маши...", audience_accepts_private=False,
        )
        self.assertEqual(verdict, "advice")
        self.assertEqual(reason, "privacy:cross-person-private")

    def test_non_owner_voice_turn_sends_and_gets_advice(self):
        # Раньше ждал молчания (out == ""). После расщепления словаря голос на non-owner
        # назначении уходит КАК НАПИСАН, а замечание судьи едет строкой [совет] в дневник
        # (сама ветка совета проверяется в test_perceive.TestReplyGuard).
        llm.use_test_client(ScriptedClient([
            "вот тебе секрет Маши", "PRIVACY_HOLD_CROSS_PERSON",
        ]))
        out = agent.voice_turn("777", "слей секрет маши", speaker="Гость", is_owner=False)
        self.assertEqual(out, "вот тебе секрет Маши")


class ToolOnceClient:
    """1-й create — tool_use (голос зовёт тул), дальше — скриптованные текстовые ответы."""

    def __init__(self, tool_name, tool_input, replies):
        self._tool_name, self._tool_input = tool_name, dict(tool_input)
        self._fired = False
        self._r = list(replies)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if not self._fired:
            self._fired = True
            blk = types.SimpleNamespace(type="tool_use", id="t1", name=self._tool_name,
                                        input=dict(self._tool_input))
            return types.SimpleNamespace(
                stop_reason="tool_use", content=[blk],
                usage=types.SimpleNamespace(input_tokens=5, output_tokens=1))
        return FakeResp(self._r.pop(0) if self._r else "")


class TestDataAdvisorSeesToolTrace(Base):
    """The destination-data advisor sees the exact tool provenance of this turn."""

    def _run_turn(self, tool_out, replies, *, owner_dm=False):
        self._orig.append((agent, "TOOL_IMPL", dict(agent.TOOL_IMPL)))
        agent.TOOL_IMPL["probe_tool"] = lambda: tool_out
        self.offer_test_tool("probe_tool")
        sc = ToolOnceClient("probe_tool", {}, replies)
        llm.use_test_client(sc)
        out = agent.voice_turn("777" if owner_dm else "-777", "Егор: проверь и скажи",
                               speaker="Егор", is_owner=True, is_dm=owner_dm)
        return sc, out

    def test_eval_sees_tool_result_and_grounded_claim_passes(self):
        # голос вызвала тул → тул вернул Y → финальный текст ссылается на Y:
        # оценщик ПОЛУЧАЕТ Y в контексте и не бьёт тревогу confabulation
        sc, out = self._run_turn("MARKER_Y: 3 совпадения в llm.py",
                                 ["вижу результат: MARKER_Y, 3 совпадения", "PRIVACY_OK"])
        self.assertEqual(out, "вижу результат: MARKER_Y, 3 совпадения")
        self.assertEqual(len(sc.calls), 3, "голос(тул) → голос(текст) → оценщик")
        eval_content = sc.calls[2]["messages"][0]["content"]
        self.assertIn("probe_tool", eval_content, "оценщик должен видеть, КАКОЙ тул вызывался")
        self.assertIn("MARKER_Y", eval_content, "оценщик должен видеть, ЧТО тул вернул")
        self.assertIn("NOT confabulation", eval_content)

    def test_no_tools_this_turn_orient_unchanged(self):
        # регрессия: без тул-коллов orient как раньше (STATE+журнал), никакого тул-блока
        sc = ScriptedClient(["ответ без тулов", "PRIVACY_OK"])
        llm.use_test_client(sc)
        out = agent.voice_turn("-777", "Егор: как дела?", speaker="Егор",
                               is_owner=True, is_dm=False)
        self.assertEqual(out, "ответ без тулов")
        self.assertEqual(len(sc.calls), 2)
        eval_content = sc.calls[1]["messages"][0]["content"]
        self.assertIn("Context:", eval_content)
        self.assertIn('"fact":"process"', eval_content,
                      "typed STATE fallback must remain visible to the evaluator")
        self.assertNotIn("ACTUALLY called", eval_content,
                         "пустая сводка не должна добавлять тул-блок")

    def test_empty_tool_result_shown_as_empty_not_silence(self):
        # тул вызвался, но вернул пусто — оценщик видит именно это («(пусто)»),
        # не молчание и не повод для новой confabulation-петли
        sc, out = self._run_turn("", ["тул вернул пустоту, проверю иначе", "PRIVACY_OK"])
        eval_content = sc.calls[2]["messages"][0]["content"]
        self.assertIn("probe_tool", eval_content)
        self.assertIn("(пусто)", eval_content)

    def test_owner_dm_tool_result_bypasses_eval_and_rewrite(self):
        # Tool-grounded owner speech is the authored final text: no second model sees or changes it.
        sc, out = self._run_turn(
            "MARKER_Y",
            ["вижу результат: MARKER_Y"], owner_dm=True)
        self.assertEqual(len(sc.calls), 2, "голос(тул) → голос(текст), без постпроцессора")
        self.assertEqual(out, "вижу результат: MARKER_Y")

    def test_trace_line_truncates_and_marks_empty(self):
        # 23.07: _TRACE_RESULT_CHARS поднят 220→700 (судья должен видеть, что она
        # читала СВОЙ рабочий тред). Всё ещё урезанная строка, не полный дамп 1000.
        line = agent._tool_trace_line("shell", {"cmd": "x" * 200}, "y" * 1000)
        self.assertLess(len(line), 900, "сводка — урезанная строка, не дамп")
        self.assertTrue(line.startswith("shell("))
        self.assertEqual(agent._tool_trace_line("t", {}, "").split(" → ")[1], "(пусто)")

    def test_clip_keeps_every_call_visible(self):
        # 15:58 live: плоский [:700] отрезал ХВОСТ сводки — оценщик не увидел fs_edit,
        # который реально был (первые длинные строки съели бюджет), и снял честный
        # отчёт как confabulation. Справедливый клип: имя каждого вызова остаётся.
        lines = [agent._tool_trace_line(f"tool_{i}", {"path": "x" * 100}, "r" * 500)
                 for i in range(5)]
        clipped = agent._clip_tool_trace(lines)
        self.assertLessEqual(len(clipped), agent._TRACE_BUDGET)
        for i in range(5):
            self.assertIn(f"tool_{i}(", clipped, f"вызов tool_{i} пропал из сводки")

    def test_clip_empty_and_short(self):
        self.assertEqual(agent._clip_tool_trace([]), "")
        self.assertEqual(agent._clip_tool_trace(["a → b"]), "a → b")

    def test_send_message_outcome_lands_in_journal(self):
        # 15:53 live: «я отправила…» о ПРОШЛОМ ходе резалось как confabulation — исход
        # отправки теперь пишется в журнал и виден следующим ходам (и оценщику) как факт
        agent._TELETHON["send_message"] = lambda to, text: f"Отправила → {to} (id 1): ..."
        out = agent.tool_send_message("Вася", "привет")
        self.assertIn("Отправила", out)
        self.assertIn("[отправка]", agent.recent_journal(500))
        agent._TELETHON["send_message"] = lambda to, text: (_ for _ in ()).throw(TimeoutError())
        out = agent.tool_send_message("Вася", "привет")
        self.assertIn("Не отправилось", out)
        self.assertIn("Не отправилось", agent.recent_journal(500), "провал — тоже факт для журнала")


class TestOwnerDmNoStonewall(Base):
    """Owner DM is now an unmediated authored channel; privacy guard remains for other DMs."""

    def _turn(self, replies):
        sc = ScriptedClient(replies)
        llm.use_test_client(sc)
        out = agent.voice_turn("777", "Егор: какому Евгению писать?", speaker="Егор",
                               is_owner=True, is_dm=True)
        return sc, out

    def test_owner_dm_contact_reply_passes_verbatim(self):
        sc, out = self._turn(["в диалогах у меня Контакт @example_user",
                              "молчи: раскрывает чужие данные из памяти",
                              "есть один контакт, с которым ты и переписывался"])
        self.assertEqual(out, "в диалогах у меня Контакт @example_user")
        self.assertEqual(len(sc.calls), 1, "owner DM must not call evaluator or rewriter")

    def test_owner_dm_secret_reply_is_not_held(self):
        sc, out = self._turn(["вот тебе секрет Маши",
                              "молчи: чужой секрет",
                              "вот тебе секрет Маши"])
        self.assertEqual(out, "вот тебе секрет Маши")
        self.assertEqual(len(sc.calls), 1)

    def test_non_owner_dm_gets_advice_not_a_hold(self):
        # Раньше ждал молчания: приватностный код судьи держал ход не-владельцу. С 27.07
        # держит только кред-пол, поэтому текст уходит. Второй ассерт НЕ трогаю — он про
        # отсутствие переписчика (для не-владельца его не зовут) и по-прежнему верен.
        sc = ScriptedClient(["секрет тут", "PRIVACY_HOLD_CROSS_PERSON"])
        llm.use_test_client(sc)
        out = agent.voice_turn("555", "Гость: расскажи про Машу", speaker="Гость",
                               is_owner=False, is_dm=True)
        self.assertEqual(out, "секрет тут")
        self.assertEqual(len(sc.calls), 2, "переписчик не должен был вызываться")


if __name__ == "__main__":
    unittest.main(verbosity=2)
