"""
Пункт 6: у всплывшего куска памяти есть происхождение.

Восприятие остаётся полным — это её память, и урезать её не лечение. Но текст чужой
комнаты не имеет права входить в промпт неотличимо от текста этой.

Запуск:  python praxis_test.py test_recall_provenance -v
"""

from __future__ import annotations

from test_perceive import Base  # noqa: F401  (герметичный харнесс + фейки anthropic/dotenv)

import agent  # noqa: E402


class TestRecallOrigin(Base):
    def _row(self, path, source="Николай", text="про запуск кошелька"):
        return {"path": path, "source": source, "text": text}

    def test_foreign_room_is_named(self):
        ctx = agent.ChannelContext(chat_id="777", is_dm=True, owner=True)
        token = agent._TURN_CHANNEL.set(ctx)
        try:
            label = agent._recall_origin(
                self._row("memory/groups/-1001240718803/archive.jsonl"),
                "memory/groups/-1001240718803/archive.jsonl")
        finally:
            agent._TURN_CHANNEL.reset(token)
        self.assertIn("-1001240718803", label,
                      "чужая комната обязана быть названа — иначе реплика оттуда "
                      "неотличима от реплики отсюда")
        self.assertIn("Николай", label, "автор при этом не теряется")

    def test_her_own_room_turn_is_named_too(self):
        ctx = agent.ChannelContext(chat_id="777", is_dm=True, owner=True)
        token = agent._TURN_CHANNEL.set(ctx)
        try:
            label = agent._recall_origin(
                self._row("memory/self/rooms/-1001240718803/turns-2026-07.jsonl"),
                "memory/self/rooms/-1001240718803/turns-2026-07.jsonl")
        finally:
            agent._TURN_CHANNEL.reset(token)
        self.assertIn("-1001240718803", label)

    def test_current_room_is_not_labelled(self):
        """Метка на каждой строке своего же чата обесценила бы саму метку."""
        ctx = agent.ChannelContext(chat_id="-1001240718803", room_id="-1001240718803",
                                   is_dm=False)
        token = agent._TURN_CHANNEL.set(ctx)
        try:
            label = agent._recall_origin(
                self._row("memory/groups/-1001240718803/archive.jsonl"),
                "memory/groups/-1001240718803/archive.jsonl")
        finally:
            agent._TURN_CHANNEL.reset(token)
        self.assertEqual(label, "Николай")

    def test_non_room_sources_are_unchanged(self):
        label = agent._recall_origin(
            self._row("memory/life/events/2026-07-25.jsonl", source="Егор"),
            "memory/life/events/2026-07-25.jsonl")
        self.assertEqual(label, "Егор")


class TestRoomTurnsAreFindableButNotSilent(Base):
    def test_indexed_as_its_own_kind(self):
        import memory_fts
        from pathlib import Path
        mem = self.tmp / "memory"
        p = mem / "self" / "rooms" / "-1001240718803" / "turns-2026-07.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}\n", encoding="utf-8")
        selected = memory_fts._selected_jsonl(Path(p), mem)
        self.assertIsNotNone(selected, "её ход в комнате обязан быть находимым")
        self.assertEqual(selected[0], "room_turn")

    def test_never_enters_the_prompt_silently(self):
        import memory_provenance
        self.assertFalse(memory_provenance.automatic_recall_allowed(
            source_type="room_turn",
            path="memory/self/rooms/-1001240718803/turns-2026-07.jsonl",
            text="что-то", memory_dir=self.tmp / "memory"))
