"""Она может посмотреть В КОНКРЕТНОЕ место — и не может подсмотреть чужое.

⚠ 03.08.2026. `tool_recent_turns` брала комнату ТОЛЬКО из `_active_chat()`, а в её часовом
пульсе он `None`: пульс рассуждает обо всех комнатах сразу и ни в одной не находится. То
есть в единственном автономном пробуждении — ровно там, где решается «хочу ли я кому-то
написать», — спросить «что я говорила вот здесь» она не могла ничем. Рука была, но молча
привязана к комнате, которой нет.

Гейт раскрытия здесь тот же, что у `other_rooms_digest`: содержимое одних комнат не
показывается в других. Это НЕ забор перед отправкой — заборы на том шве ловят повтор,
когда текст уже сочинён; это глаз до того, как говорить.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

import agent

#: Форма живого реестра прода (`memory/.state/buf_meta.json`), сокращённая.
META = {
    "809306689": {"name": "Yegor Kosyrev (@tatarskiy_e4pochmak)",
                  "last_ts": 300.0, "is_dm": True},
    "-1001240718803__topic__96256": {"name": "AbstractDL Chat · topic #96256",
                                     "last_ts": 200.0},
    "-1001240718803__topic__96051": {"name": "AbstractDL Chat · topic #96051",
                                     "last_ts": 100.0},
    "-1004301095307__topic__726": {"name": "mycelium · Курилка", "last_ts": 50.0},
}


class Base(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(agent, "_rooms_meta", lambda: dict(META))
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"PRAXIS_OWNER_ID": "809306689"}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def _in(self, ctx):
        token = agent._TURN_CHANNEL.set(ctx)
        self.addCleanup(agent._TURN_CHANNEL.reset, token)

    @staticmethod
    def _pulse_ctx():
        return agent.ChannelContext(
            chat_id=None, principal_id=agent.PRAXIS_SELF_PRINCIPAL, is_dm=True,
            owner=False, known=True, _scope_override="owner")

    @staticmethod
    def _group_ctx():
        return agent.ChannelContext.from_legacy(
            chat_id=-1001240718803, is_dm=False, owner=False, known=True)


class RoomsResolveWithoutGuessing(Base):
    def test_exact_name_wins(self):
        key, why = agent.resolve_room("mycelium · Курилка")
        self.assertEqual(key, "-1004301095307__topic__726")
        self.assertEqual(why, "")

    def test_the_address_itself_is_accepted(self):
        key, _ = agent.resolve_room("-1001240718803__topic__96051")
        self.assertEqual(key, "-1001240718803__topic__96051")

    def test_she_may_call_the_owner_by_his_name(self):
        """В реестре место подписано display-именем Telegram, а она ищет «Егора»."""
        for word in ("Егор", "егор", "  ЕГОР  "):
            key, _ = agent.resolve_room(word)
            self.assertEqual(key, "809306689", f"«{word}» не нашёл личку владельца")

    def test_several_matches_answer_with_addresses_not_with_a_guess(self):
        key, instead = agent.resolve_room("AbstractDL")
        self.assertIsNone(key, "выбрал одно из нескольких мест за неё")
        self.assertIn("#96256", instead)
        self.assertIn("#96051", instead)
        self.assertLess(instead.index("#96256"), instead.index("#96051"),
                        "кандидаты не по свежести")

    def test_an_unknown_place_is_not_invented(self):
        key, instead = agent.resolve_room("комната, которой нет")
        self.assertIsNone(key)
        for name in ("AbstractDL", "mycelium", "Yegor"):
            self.assertNotIn(name, instead, "подсунул чужое место вместо честного «нет»")

    def test_empty_query_asks_instead_of_picking(self):
        key, instead = agent.resolve_room("   ")
        self.assertIsNone(key)
        self.assertTrue(instead.strip())


class OnlyTheOwnerChannelMayNameAnotherRoom(Base):
    def test_her_own_window_may(self):
        self._in(self._pulse_ctx())
        self.assertTrue(agent._may_name_other_rooms(),
                        "её собственное окно проходит по praxis_self, не по правам Егора")

    def test_a_group_may_not(self):
        self._in(self._group_ctx())
        self.assertFalse(agent._may_name_other_rooms())

    def test_a_group_gets_a_refusal_and_no_data(self):
        self._in(self._group_ctx())
        with mock.patch.object(agent.turns, "describe") as described, \
                mock.patch.object(agent.notes, "read") as read:
            out = agent.tool_recent_turns(room="Егор")
        described.assert_not_called()
        read.assert_not_called()
        self.assertNotIn("809306689", out, "адрес чужой комнаты утёк в отказ")
        self.assertIn("не раскрываю", out)


class TheHandShowsBothSources(Base):
    def test_without_a_room_nothing_changes(self):
        self._in(self._pulse_ctx())
        with mock.patch.object(agent.turns, "describe", return_value="СТАРЫЙ ПУТЬ") as d:
            self.assertEqual(agent.tool_recent_turns(n=3), "СТАРЫЙ ПУТЬ")
        self.assertIsNone(d.call_args.kwargs["chat_id"],
                          "в пульсе комнаты нет — и это правда, а не дефект")

    def test_a_named_room_brings_turns_and_her_own_note(self):
        self._in(self._pulse_ctx())
        with mock.patch.object(agent.turns, "describe_room", return_value="ХОДЫ ТАМ") as d, \
                mock.patch.object(agent.notes, "read", return_value="20:36 · сказала: «а»"):
            out = agent.tool_recent_turns(room="mycelium · Курилка")
        self.assertEqual(d.call_args.kwargs["chat_id"], "-1004301095307__topic__726")
        self.assertIn("mycelium · Курилка", out)
        self.assertIn("ХОДЫ ТАМ", out)
        self.assertIn("20:36 · сказала: «а»", out)

    def test_the_hand_filters_by_place_not_by_the_privacy_tier(self):
        """⚠ Живая проба 03.08: «Егор» и «AbstractDL» вернули ОДИН ход, ничей из двух.

        `turns.describe(scope="owner", chat_id=X)` игнорирует X — там chat_id это «что
        мне позволено видеть», а не «что было здесь». Подпись «Место: X» над той лентой
        была ложью, и поймала её живая проба, а не тест. Пусть теперь ловит тест.
        """
        self._in(self._pulse_ctx())
        with mock.patch.object(agent.turns, "describe") as tier, \
                mock.patch.object(agent.turns, "describe_room", return_value="ТУТ") as place, \
                mock.patch.object(agent.notes, "read", return_value=""):
            agent.tool_recent_turns(room="Егор")
        tier.assert_not_called()
        place.assert_called_once()

    def test_an_empty_note_says_why_instead_of_looking_like_silence(self):
        """«Записки нет» и «я тут молчала» — разные утверждения, и путать их нельзя."""
        self._in(self._pulse_ctx())
        with mock.patch.object(agent.turns, "describe_room", return_value="ХОДЫ"), \
                mock.patch.object(agent.notes, "read", return_value="  "):
            out = agent.tool_recent_turns(room="Егор")
        self.assertIn("мимо того пути", out)

    def test_a_broken_note_does_not_take_the_turn_down(self):
        self._in(self._pulse_ctx())
        with mock.patch.object(agent.turns, "describe_room", return_value="ХОДЫ"), \
                mock.patch.object(agent.notes, "read", side_effect=OSError("том отвалился")):
            out = agent.tool_recent_turns(room="Егор")
        self.assertIn("ХОДЫ", out)


class PlaceIsNotPrivacy(unittest.TestCase):
    """Два разных вопроса, которые звались одним именем `chat_id`."""

    RING = [
        {"chat_id": "809306689", "ts": 1.0, "kind": "chat"},
        {"chat_id": "-1004301095307__topic__726", "ts": 2.0, "kind": "chat"},
        {"chat_id": None, "ts": 3.0, "kind": "task_window"},
    ]

    def _ring(self):
        return mock.patch.object(agent.turns, "_load_ring", lambda: list(self.RING))

    def test_in_room_keeps_only_that_place(self):
        with self._ring():
            rows = agent.turns.in_room(n=10, chat_id="809306689")
        self.assertEqual([r["ts"] for r in rows], [1.0])

    def test_the_privacy_tier_still_shows_everything_to_the_owner(self):
        """Не вакуумно: показывает, что старая функция фильтром НЕ является.

        Если этот тест однажды покраснеет — значит `recent` начал фильтровать по месту,
        и тогда надо пересматривать обе функции разом, а не радоваться.
        """
        with self._ring():
            rows = agent.turns.recent(n=10, scope="owner", chat_id="809306689")
        self.assertEqual(len(rows), 3)

    def test_a_window_without_a_place_belongs_to_no_room(self):
        """Её собственные окна не приписываются ничьей комнате."""
        self.assertFalse(agent.turns.same_place({"chat_id": None}, "809306689"))

    def test_describe_room_says_so_when_the_place_is_silent(self):
        with mock.patch.object(agent.turns, "_load_ring", lambda: []):
            self.assertIn("Ни одного записанного хода",
                          agent.turns.describe_room(n=5, chat_id="809306689"))


class TheToolDeclaresItsNewAddress(unittest.TestCase):
    def test_schema_offers_the_room(self):
        schema = agent.RECENT_TURNS_TOOL["input_schema"]["properties"]
        self.assertIn("room", schema)
        self.assertEqual(schema["room"]["type"], "string")

    def test_the_description_says_what_it_gives_back(self):
        text = agent.RECENT_TURNS_TOOL["description"]
        self.assertIn("room", text)
        self.assertIn("записку", text, "про второй источник не сказано — она о нём не узнает")

    def test_the_handler_accepts_the_argument_the_schema_promises(self):
        """Схема и рука обязаны сходиться: обещанный аргумент должен приниматься.

        Схема — это всё, что модель знает о руке. Обещать в ней `room` и не принимать его
        значит подарить ей вызов, который упадёт TypeError уже в бою.
        """
        import inspect
        params = inspect.signature(agent.tool_recent_turns).parameters
        for name in agent.RECENT_TURNS_TOOL["input_schema"]["properties"]:
            self.assertIn(name, params, f"схема обещает {name}, а рука его не берёт")


if __name__ == "__main__":
    unittest.main()
