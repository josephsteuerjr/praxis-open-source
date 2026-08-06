"""
Тесты хода голоса по чату (PASS 8.1: привратник снесён) + заметки-continuity. Герметичны:
группа→voice с сентинелом [молчу], DM-фрейм, окно-тул focus, анти-повтор.

Запуск:  python praxis_test.py test_perceive -v
"""

from __future__ import annotations

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
import notes  # noqa: E402
import people as pe  # noqa: E402
import agent  # noqa: E402
import llm  # noqa: E402


class FakeResp:
    def __init__(self, text):
        self.stop_reason = "end_turn"
        self.content = [types.SimpleNamespace(type="text", text=text)]


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.last = {}
        self.calls = []

        class _M:
            def create(_s, **kw):
                self.calls.append(kw)
                system = kw.get("system", "")
                if isinstance(system, list):
                    system = "".join(str(block.get("text", "")) for block in system
                                     if isinstance(block, dict))
                # A group voice turn has a second, deliberately narrow call.  Keep the
                # authored fixture for the voice and answer the typed data-authority
                # protocol independently, as production does.
                if "data-authority checker" in str(system) and "PRIVACY_OK" in str(system):
                    return FakeResp("PRIVACY_OK")
                self.last = kw
                return FakeResp(self.reply)
        self.messages = _M()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_pv_"))
        mem = self.tmp / "memory"
        soul = self.tmp / "soul"
        for d in (mem / "people", mem / "rooms", mem / "journal", mem / ".vectors",
                  mem / ".scratch", soul / "skills"):
            d.mkdir(parents=True, exist_ok=True)
        (soul / "SOUL.md").write_text("# Конституция\n\n## Кто я по характеру\nтёплая, колкая.\n", encoding="utf-8")
        for n in ("self", "emotions", "being_with"):
            (soul / f"{n}.md").write_text(f"# {n}\n", encoding="utf-8")
        self._orig = []
        patch = {
            agent: dict(BASE=self.tmp, SOUL_DIR=soul, SKILLS_DIR=soul / "skills", MEM_DIR=mem,
                        PEOPLE_DIR=mem / "people", ROOMS_DIR=mem / "rooms",
                        JOURNAL_DIR=mem / "journal", REFLECTIONS=mem / "reflections.md",
                        INDEX_MD=mem / "INDEX.md"),
            notes: dict(BASE=self.tmp, SCRATCH_DIR=mem / ".scratch"),
            pe: dict(BASE=self.tmp, PEOPLE_DIR=mem / "people"),
            mi: dict(BASE=self.tmp, MEM_DIR=mem, SOUL_DIR=soul, SKILLS_DIR=soul / "skills",
                     PEOPLE_DIR=mem / "people", VECTORS_DIR=mem / ".vectors",
                     INDEX_JSON=mem / ".vectors" / "index.json", INDEX_MD=mem / "INDEX.md"),
            llm: dict(CONFIG_PATH=mem / "llm.json", JOURNAL_DIR=mem / "journal",
                      _CACHE={"mtime": None, "cfg": None}),
        }
        for module, attrs in patch.items():
            for k, val in attrs.items():
                self._orig.append((module, k, getattr(module, k)))
                setattr(module, k, val)
        mi._CACHE = {"mtime": None, "index": None}
        self._orig.append((mi, "embed", mi.embed))
        mi.embed = lambda t: (_ for _ in ()).throw(ConnectionError("no ollama"))
        llm.clear_test_clients()
        self.addCleanup(llm.clear_test_clients)

    def tearDown(self):
        for module, k, val in reversed(self._orig):
            setattr(module, k, val)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _client(self, reply):
        self.fc = FakeClient(reply)
        llm.use_test_client(self.fc)


class TestNotes(Base):
    def test_append_and_read_rolls(self):
        for i in range(12):
            notes.append("-100", f"строка {i}")
        out = notes.read("-100")
        self.assertEqual(len(out.splitlines()), notes.MAX_ENTRIES, "роллинг не ограничил")
        self.assertIn("строка 11", out)
        self.assertNotIn("строка 0", out, "старое не вытеснилось")

    def test_empty_when_absent(self):
        self.assertEqual(notes.read("-999"), "")


class TestParseSilence(Base):
    def test_exact_sentinel(self):
        self.assertEqual(agent._parse_silence("[молчу]"), (True, "", ""))

    def test_sentinel_followed_by_prose_is_authored_text(self):
        silent, reason, text = agent._parse_silence("[молчу] приманка, только что говорила")
        self.assertFalse(silent)
        self.assertEqual(reason, "")
        self.assertEqual(text, "[молчу] приманка, только что говорила")

    def test_sentinel_stuck_to_text_is_preserved(self):
        silent, _, text = agent._parse_silence("ну смотри, кэш греет вход [молчу]")
        self.assertFalse(silent)
        self.assertEqual(text, "ну смотри, кэш греет вход [молчу]")

    def test_empty_is_silence(self):
        self.assertEqual(agent._parse_silence("")[0], True)

    def test_case_and_spaces(self):
        self.assertTrue(agent._parse_silence("[ Молчу ]")[0])

    def test_private_exact_mode_preserves_embedded_or_suffixed_marker(self):
        self.assertEqual(
            agent._parse_silence("ну смотри [молчу] дальше", exact_only=True),
            (False, "", "ну смотри [молчу] дальше"),
        )
        self.assertEqual(
            agent._parse_silence("[молчу] но это цитата", exact_only=True),
            (False, "", "[молчу] но это цитата"),
        )
        self.assertEqual(
            agent._parse_silence("[ Молчу ]", exact_only=True),
            (True, "", ""),
        )


class TestGroupVoice(Base):
    def test_group_gets_presence_frame_dm_does_not(self):
        self._client("ответ")
        agent.voice_turn("-100", "вася: погода", speaker="вася", is_dm=False)
        sysm = self.fc.last["system"]
        text = "".join(b["text"] for b in sysm) if isinstance(sysm, list) else sysm
        self.assertIn("live group in which you participate", text,
                      "в группе нет presence-фрейма")
        self.assertIn("[молчу]", text)
        self._client("ответ2")
        agent.voice_turn("777", "егор: привет", speaker="Егор", is_owner=True, is_dm=True)
        text = "".join(b["text"] for b in self.fc.last["system"]) \
            if isinstance(self.fc.last["system"], list) else self.fc.last["system"]
        self.assertNotIn("live group in which you participate", text,
                         "DM не должен получать групповой фрейм")
        self.assertIn("PRIVATE conversation", text, "DM-фрейм (бывшая perceive-преамбула)")

    def test_group_silence_sentinel_writes_note(self):
        self._client("[молчу]")
        out = agent.voice_turn("-100", "вася: пракс, ну скажи что-нибудь", speaker="вася", is_dm=False)
        self.assertEqual(out, "", "сентинел должен стать тишиной")
        note = notes.read("-100")
        self.assertIn("промолчала", note)

    def test_silence_marker_with_prose_is_delivered_verbatim(self):
        self._client("[молчу] пустой призыв")
        out = agent.voice_turn("-100", "вася: пракс, ну скажи что-нибудь", speaker="вася", is_dm=False)
        self.assertEqual(out, "[молчу] пустой призыв")

    def test_note_visible_to_voice(self):
        notes.append("-100", "тут был спор · сказала: «кэш греет вход»")
        self._client("[молчу]")
        agent.voice_turn("-100", "вася: ну", speaker="вася", is_dm=False)
        content = self.fc.last["messages"][-1]["content"]
        if isinstance(content, list):
            text = "".join(str(block.get("text", "")) for block in content
                           if isinstance(block, dict))
        else:
            text = str(content)
        self.assertIn("кэш греет вход", text, "записка чата должна быть видна голосу")

    def test_group_authored_repeat_is_delivered(self):
        self._client("норм ответ")
        out = agent.voice_turn("-100", "сосед: как дела?", speaker="сосед", is_dm=False)
        self.assertEqual(out, "норм ответ")

    def test_dm_repeat_not_held(self):
        # в личке анти-повтор не давит: переспросили — можно повторить
        notes.append("777", "т · сказала: «норм ответ»")
        self._client("норм ответ")
        out = agent.voice_turn("777", "егор: повтори", speaker="Егор", is_owner=True, is_dm=True)
        self.assertEqual(out, "норм ответ")

    def test_perceive_machinery_gone(self):
        for name in ("perceive", "PERCEIVE_INSTRUCTION", "_parse_perceive", "_FORMS"):
            self.assertFalse(hasattr(agent, name), f"легаси {name} должен быть удалён (PASS 8.1)")


class TestVoiceTurn(Base):
    def test_owner_tools_and_gist(self):
        self._client("ну смотри, кэш греет вход")
        out = agent.voice_turn("777", "егор: расскажи про кэш", speaker="Егор", is_owner=True)
        self.assertEqual(out, "ну смотри, кэш греет вход")
        names = [t["name"] for t in self.fc.last.get("tools", [])]
        self.assertIn("shell", names, "у голоса владельца должны быть руки")
        # ⚠ Было assertIn("сказала (голос)", notes.read(...)) сразу после voice_turn.
        # Заметка о речи теперь рождается не от авторства, а от расписки транспорта
        # (agent.project_delivery_outcome), потому что между ними лежат отмена, отказ
        # приватности и провал доставки. Здесь Telegram не участвует — значит и следа
        # речи быть не должно.
        self.assertNotIn("сказала (голос)", notes.read("777") or "",
                         "речь фиксируется приёмкой, а не авторством")

    def test_no_client_silent(self):
        llm.use_test_client(None)
        self.assertEqual(agent.voice_turn("-100", "x", is_dm=False), "")


class TestWindowTool(Base):
    def test_tool_in_base_tools_all_scopes(self):
        base = {t.get("name") for t in agent.BASE_TOOLS}
        self.assertIn("focus", base, "фокус-окно — BASE-тул (окно про неё, не про чат)")
        self.assertIn("focus", agent.TOOL_IMPL)

    def test_opens_focus_window_now(self):
        import tasks
        out = agent.tool_focus("код: скрипт для Егора")
        self.assertIn("ближайшем тике", out)
        self.assertNotIn("скрипт", out, "подтверждение — краткое, без деталей (видно и в группе)")
        open_ = [t for t in tasks.list_open() if t["kind"] == "window"]
        self.assertTrue(open_, "фокус-окно не поставлено")
        t = open_[-1]
        self.assertEqual(t["goal"], "код: скрипт для Егора")
        self.assertEqual(t["target"], "", "PASS 26: focus больше не метит __auto__")
        self.assertEqual(t["author"], "praxis", "провенанс: её сознательный фокус")
        self.assertTrue(t["when"], "when должен быть проставлен (in 0m → сейчас)")
        self.assertTrue(tasks.due(), "намерение должно созреть немедленно")

    def test_empty_goal_refused(self):
        self.assertIn("пустой", agent.tool_focus("  "))


class TestRest(Base):
    """Здоровый сон = её приватное неприкосновенное время (Фаза K)."""

    def test_rest_tool_in_base_all_scopes(self):
        base = {t.get("name") for t in agent.BASE_TOOLS}
        self.assertIn("rest", base, "сон — её тул в любом канале")
        self.assertIn("rest", agent.TOOL_IMPL)

    def test_rest_goal_detected_and_distinct(self):
        self.assertTrue(agent._is_rest_goal("отдых: тишина"))
        self.assertTrue(agent._is_rest_goal("ОТДЫХ: побыть"))
        self.assertFalse(agent._is_rest_goal("код: скрипт"))
        self.assertFalse(agent._is_rest_goal("обычное окно"))

    def test_rest_opens_private_window_authored_by_her(self):
        import tasks
        out = agent.tool_rest("дать шуму улечься")
        self.assertIn("к себе", out.lower())
        win = [t for t in tasks.list_open() if t["kind"] == "window"]
        self.assertTrue(win, "приватное окно сна не поставлено")
        t = win[-1]
        self.assertTrue(t["goal"].startswith(agent.REST_PREFIX), "окно помечено как отдых")
        self.assertEqual(t["author"], "praxis", "это ЕЁ выбор уйти к себе")

    def test_rest_frame_is_care_and_inviolable(self):
        # рамка сна — про заботу и неприкосновенность, а не про повестку/работу
        frame = agent._REST_WINDOW_FRAME
        self.assertIn("никто не смеет трогать", frame, "сон неприкосновенен")
        self.assertIn("неприкосновен", frame)
        self.assertIn("забот", frame, "тон — забота о себе")
        self.assertIn("по желанию", frame, "ничего не обязательно")


class JudgeClient:
    """Голос отвечает `reply`, а типизированный судья — заданным голым кодом.

    Нужен, чтобы прогнать ХОД ЦЕЛИКОМ через настоящий `evaluate_reply`, а не через
    подменённую лямбду: словарь вердиктов 27.07 разъехался на два (стоп/совет), и
    проверять надо именно разбор кода, а не веру теста в строку.
    """

    def __init__(self, reply, verdict):
        outer = self
        self.reply, self.verdict, self.calls = reply, verdict, []

        class _M:
            def create(_s, **kw):
                outer.calls.append(kw)
                system = kw.get("system", "")
                if isinstance(system, list):
                    system = "".join(str(block.get("text", "")) for block in system
                                     if isinstance(block, dict))
                if "data-authority checker" in str(system):
                    return FakeResp(outer.verdict)
                return FakeResp(outer.reply)
        self.messages = _M()


class TestReplyGuard(Base):
    """27.07: судья в разговорах — СОВЕТ, а не придержка.

    Решение Егора дословно: «давай в разговорах его ослабим, ну его нахер». Твёрдым
    остался ровно один рельс — механический кред-пол. Живой повод: 26.07 судья придержал
    её рассказ о СВОЕЙ ЖЕ работе, она переписала и отправила то же самое сама через 48
    секунд, — придержка стоила хода и не предотвратила ничего.
    """

    def setUp(self):
        super().setUp()
        self.wakes: list[str] = []
        self.journal: list[str] = []
        self.rows: list[dict] = []
        for module, name, stub in (
            (agent, "tool_journal", lambda entry, salience=2: (
                self.journal.append(str(entry)) or "")),
            (agent, "_held_self_wake", lambda ctx, *, reason, reply: (
                self.wakes.append(reason) or None)),
            (agent.turns, "record", lambda turn: self.rows.append(dict(turn))),
            (agent.identity, "load_from_turn", lambda turn: None),
        ):
            self._orig.append((module, name, getattr(module, name)))
            setattr(module, name, stub)

    def _guard(self, reply, verdict=None, reason=""):
        """Один ход гарда в группе -> что ушло наружу. Записи — в self.rows/journal/wakes."""
        if verdict is not None:
            self._orig.append((agent, "evaluate_reply", agent.evaluate_reply))
            agent.evaluate_reply = lambda text, context="", **kw: (verdict, reason)
        ctx = agent.ChannelContext(chat_id="-100", is_dm=False, owner=False, known=True)
        return agent.guard_outbound_reply(
            reply, "сосед: что там у маши?", ctx=ctx, turn={"kind": "chat"})

    def test_privacy_verdict_no_longer_holds_her_word(self):
        out = self._guard("мой аптайм три минуты", "advice", "privacy:cross-chat-private")
        self.assertEqual(out, "мой аптайм три минуты", "совет не останавливает её слово")
        row = self.rows[-1]
        self.assertFalse(row.get("held"), "ход не придержан")
        self.assertEqual((row.get("advisor_verdict"), row.get("praxis_decision")),
                         ("advice", "send_authored_with_advice"))
        self.assertEqual(row.get("advisor_reason"), "privacy:cross-chat-private")

    def test_the_advice_reaches_her_in_the_journal_without_waking_her(self):
        self._guard("мой аптайм три минуты", "advice", "privacy:cross-chat-private")
        advice = [e for e in self.journal if e.startswith("[совет]")]
        self.assertEqual(len(advice), 1, "замечание обязано лечь в тот же дневник")
        self.assertIn("privacy:cross-chat-private", advice[0], "причина названа")
        self.assertIn("-100", advice[0], "названо, о каком чате речь")
        self.assertEqual(self.wakes, [],
                         "будильник на каждое замечание — это свой шум, Егор назвал риск")

    def test_credential_floor_still_stops_her_and_wakes_her(self):
        out = self._guard("вот ключ ghp_" + "c" * 36, "ok", "")
        self.assertEqual(out, "", "единственный твёрдый рельс проекта на месте")
        row = self.rows[-1]
        self.assertEqual(row.get("held"), "privacy")
        self.assertTrue(str(row.get("advisor_reason") or "").startswith("privacy:credential"),
                        row.get("advisor_reason"))
        self.assertEqual(len(self.wakes), 1, "настоящий стоп — единственное, что её будит")

    def test_advisor_unavailable_stops_nothing_and_advises_nothing(self):
        out = self._guard("обычная реплика", "unavailable",
                          "privacy advisor unavailable for non-owner audience")
        self.assertEqual(out, "обычная реплика",
                         "молчать оттого, что судья не ответил, — больше не класс отказа")
        self.assertEqual(self.rows[-1].get("praxis_decision"), "send_without_advisor")
        self.assertEqual([e for e in self.journal if e.startswith("[совет]")], [],
                         "недоступность — не находка, советовать нечего")
        self.assertEqual(self.wakes, [])

    def test_group_voice_survives_a_privacy_code_end_to_end(self):
        # Ровно инцидент 26.07: её доклад о своей же работе + вердикт cross-chat.
        llm.use_test_client(JudgeClient("свою часть запушила: commit 305fb99",
                                        "PRIVACY_HOLD_CROSS_CHAT"))
        out = agent.voice_turn("-100", "сосед: что там у тебя?", speaker="сосед", is_dm=False)
        self.assertEqual(out, "свою часть запушила: commit 305fb99")
        self.assertTrue(any(e.startswith("[совет]") for e in self.journal))

    def test_staged_image_credential_is_the_one_code_that_still_stops(self):
        # Пиксели механический пол не читает — этот код и есть его глаза на картинке.
        out = self._guard("вот скрин", "deny", "privacy:credential")
        self.assertEqual(out, "")
        self.assertEqual(self.rows[-1].get("held"), "privacy")

    def test_ok_group_voice_passes(self):
        self._client("норм ответ")
        self._orig.append((agent, "evaluate_reply", agent.evaluate_reply))
        agent.evaluate_reply = lambda text, context="", **kw: ("ok", "")
        out = agent.voice_turn("-100", "сосед: как дела?", speaker="сосед", is_dm=False)
        self.assertEqual(out, "норм ответ")


class TestResumedDeliveryHonoursUnavailableReceipt(unittest.TestCase):
    """Второй, невидимый вход в тот же контур: возобновлённый после рестарта ход.

    ⚠ Живой контур был покрыт нулём тестов во всём репо (grep по
    `_prepare_authored_delivery`/`_outbound_guard_receipt` в test_*.py — пусто), а держал
    он ровно ту же придержку по недоступности судьи, что и живой путь, только дважды:
    расписка «судья не ответил» ВЫБРАСЫВАЛАСЬ («только явный ok освобождает»), гард гнался
    заново — и если советник молчал и во второй раз, ход останавливался `RunStopped`.
    То есть после рестарта её слово держал не приватностный вывод, а недоступность
    чужой модели. Тест закрепляет: расписка `unavailable` терминальна, гард не гоняется
    заново (второй прогон — это ещё и риск второй доставки того же текста).
    """

    class _Route:
        conversation_id, peer_id, topic_id = "-100", -100, None

    def _runtime(self, receipt, *, guard_input_must_not_run):
        import contextlib
        route = self._Route()
        state: dict = {"queued": None}

        @contextlib.contextmanager
        def _bind():
            yield None

        fake = types.SimpleNamespace(
            bind=_bind,
            plan=types.SimpleNamespace(run_id="run-test-resume", kind="authored_output"),
            channel=agent.ChannelContext(chat_id="-100", is_dm=False, owner=False),
            snapshot={},
            outbound=[],
            guard_notes=[],
            tool_trace=[],
            _route_and_reply=lambda guarded: (guarded, route, None),
            _validate_current_authority=lambda: None,
            _queue_media=lambda *, reply_to=None: state.__setitem__("queued", reply_to),
        )
        self._orig.append((agent, "_outbound_guard_receipt", agent._outbound_guard_receipt))
        agent._outbound_guard_receipt = lambda run_id, **kw: receipt
        self._orig.append((agent, "_outbound_guard_input", agent._outbound_guard_input))

        def _forbidden(*a, **kw):
            raise AssertionError(guard_input_must_not_run)
        agent._outbound_guard_input = _forbidden
        self._orig.append((agent, "run_delivery_started", agent.run_delivery_started))
        agent.run_delivery_started = lambda run_id, **kw: True
        return fake

    def setUp(self):
        self._orig: list[tuple] = []

    def tearDown(self):
        for module, name, value in reversed(self._orig):
            setattr(module, name, value)

    def test_unavailable_receipt_delivers_instead_of_re_running_the_guard(self):
        fake = self._runtime(
            {"text": "мой аптайм три минуты", "media_queue_ids": [],
             "advisor_verdict": "unavailable"},
            guard_input_must_not_run=(
                "расписка 'судья не ответил' терминальна — гард не перегоняется"),
        )
        plan = agent._AgentResumeRuntime._prepare_authored_delivery(
            fake, "мой аптайм три минуты")
        self.assertFalse(plan["silent"], "недоступность судьи больше не молчание")
        self.assertEqual(plan["text"], "мой аптайм три минуты")

    def test_a_receipt_that_really_held_her_text_still_stays_silent(self):
        # Обратная граница: кред-пол на восстановлении держит ровно так же, как и живьём —
        # иначе «ослабили в разговорах» превратилось бы в «сняли пол на рестарте».
        fake = self._runtime(
            {"text": "", "media_queue_ids": [], "advisor_verdict": "deny",
             "advisor_reason": "privacy:credential:github token"},
            guard_input_must_not_run="расписка есть — перегонять нечего",
        )
        self._orig.append((agent, "run_delivery_completed", agent.run_delivery_completed))
        agent.run_delivery_completed = lambda run_id, **kw: True
        plan = agent._AgentResumeRuntime._prepare_authored_delivery(fake, "вот ключ")
        self.assertTrue(plan["silent"])
        self.assertEqual(plan["text"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
