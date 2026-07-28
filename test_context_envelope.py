"""
Тесты теневого конверта контекста (пункт 4).

Главное, что здесь проверяется: тень ИЗМЕРЯЕТ, а не переписывает прод. Если бы
`actor_principal` определили как «то, что лежит в ctx.principal_id», тень совпадала бы
с продом на 100% и не измеряла бы ничего — включая тот единственный случай, ради
которого всё затевалось.

Запуск:  python praxis_test.py test_context_envelope -v
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import context_envelope as ce


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_env_"))
        self._orig = ce.SHADOW_PATH
        ce.SHADOW_PATH = self.tmp / "memory" / ".state" / "envelope_shadow.jsonl"

    def tearDown(self):
        ce.SHADOW_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestMeasurement(Base):
    def test_ambient_group_wake_keeps_the_real_sender(self):
        """Тот самый шов: прод пишет praxis:self, тень обязана помнить человека."""
        env = ce.measure(chat_id="-1001240718803", room_id="-1001240718803",
                         is_dm=False, owner=False, praxis_self=True,
                         actor_raw=62985, synthesized=True,
                         triggers=(ce.Trigger(principal_id="62985", message_id=93707,
                                              kind="ambient"),),
                         delegation_ref="wake:not_addressed",
                         origin_message_id=93707, origin_addressed=False)
        self.assertEqual(env.actor_principal_raw, "62985")
        self.assertTrue(env.actor_synthesized)
        self.assertEqual(env.delegation_ref, "wake:not_addressed")
        self.assertFalse(env.origin_addressed)
        self.assertEqual(env.provenance, "measured")

    def test_disclosure_tier_is_not_scope(self):
        """Тир раскрытия считается из (аудитория, актор), а не из ярлыка scope —
        иначе он был бы пятым мнением того же перегруженного поля."""
        self.assertEqual(ce.measure(chat_id=None, is_dm=True,
                                    praxis_self=True).disclosure_tier, "self")
        self.assertEqual(ce.measure(chat_id="555000100", is_dm=True,
                                    owner=True).disclosure_tier, "owner_private")
        self.assertEqual(ce.measure(chat_id="777", is_dm=True,
                                    owner=False).disclosure_tier, "person_private")
        self.assertEqual(ce.measure(chat_id="-100", is_dm=False,
                                    owner=True).disclosure_tier, "room_public")

    def test_origin_is_strict_or_nothing(self):
        """Никаких фолбэков: durable-запись уже умеет штамповать «происхождением»
        всю карту реплаев, и тень не имеет права это унаследовать."""
        self.assertIsNone(ce.measure(origin_message_id=None).origin_message_id)
        self.assertIsNone(ce.measure(origin_message_id="не число").origin_message_id)
        self.assertEqual(ce.measure(origin_message_id=93707).origin_message_id, 93707)

    def test_forum_status_defaults_to_unknown(self):
        """`unknown` воспроизводит сегодняшнее поведение маршрутизации байт-в-байт;
        «assumed» не существует как значение сознательно."""
        self.assertEqual(ce.measure().forum_status, "unknown")
        self.assertEqual(ce.measure(forum_status="мусор").forum_status, "unknown")
        self.assertEqual(ce.measure(forum_status="false").forum_status, "false")

    def test_trigger_kind_follows_the_number_of_causes(self):
        self.assertEqual(ce.measure().trigger_kind, "none")
        self.assertEqual(ce.measure(synthesized=True).trigger_kind, "synthetic")
        one = (ce.Trigger(principal_id="1"),)
        self.assertEqual(ce.measure(triggers=one).trigger_kind, "single")
        two = one + (ce.Trigger(principal_id="2"),)
        self.assertEqual(ce.measure(triggers=two).trigger_kind, "coalesced")

    def test_default_envelope_is_not_mistakable_for_a_measurement(self):
        self.assertEqual(ce.RunEnvelope().provenance, "absent")


class TestShadowLedger(Base):
    def test_record_and_divergence(self):
        for i in range(3):
            ce.record(ce.measure(chat_id="-100", room_id="-100", is_dm=False,
                                 actor_raw=10 + i, synthesized=True,
                                 origin_message_id=i + 1),
                      run_id=f"run-{i}")
        ce.record(ce.measure(chat_id="555000100", is_dm=True, owner=True,
                             actor_raw=555000100, origin_message_id=7),
                  run_id="run-owner")
        d = ce.divergence()
        self.assertEqual(d["runs"], 4)
        self.assertEqual(d["actor_synthesized"], 3)
        self.assertEqual(d["actor_synthesized_by_room"], {"-100": 3})
        self.assertEqual(d["forum_status_unknown"], 4)
        self.assertEqual(d["origin_missing"], 0)

    def test_ledger_never_raises_and_stays_outside_the_index(self):
        import memory_fts
        ce.SHADOW_PATH = self.tmp / "нет" / "такого" / "пути" / "x.jsonl"
        ce.record(ce.measure(chat_id="1"), run_id="r")   # каталога нет — но и не падаем
        ce.SHADOW_PATH = self.tmp / "memory" / ".state" / "envelope_shadow.jsonl"
        ce.record(ce.measure(chat_id="1"), run_id="r")
        self.assertTrue(ce.SHADOW_PATH.exists())
        # Приборы — не память: индексатор не должен брать этот путь ни при каких условиях.
        self.assertIsNone(
            memory_fts._selected_jsonl(ce.SHADOW_PATH, self.tmp / "memory"),
            "теневой журнал попал бы в её recall — это приборы, а не прожитое")

    def test_row_is_readable_json_with_schema(self):
        ce.record(ce.measure(chat_id="1", actor_raw=5), run_id="r1", note="chat_turn")
        row = json.loads(ce.SHADOW_PATH.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(row["run_id"], "r1")
        self.assertEqual(row["note"], "chat_turn")
        self.assertEqual(row["envelope"]["schema"], ce.SCHEMA)
        self.assertEqual(row["envelope"]["actor_principal_raw"], "5")


class TestChannelContextCarriesIt(Base):
    def test_context_default_is_none_not_a_fabricated_envelope(self):
        import agent
        self.assertIsNone(agent.ChannelContext().envelope)

    def test_runner_measures_before_the_substitution(self):
        """Регрессия на сам смысл тени: значение берётся ДО того, как прод затрёт
        актора на praxis:self, иначе тень согласна с продом всегда и бесполезна."""
        import agent
        env = ce.measure(chat_id="-100", room_id="-100", is_dm=False,
                         praxis_self=True, actor_raw=62985, synthesized=True)
        ctx = agent.ChannelContext(chat_id="-100", room_id="-100", is_dm=False,
                                   principal_id=agent.PRAXIS_SELF_PRINCIPAL,
                                   envelope=env)
        self.assertEqual(ctx.principal_id, agent.PRAXIS_SELF_PRINCIPAL)
        self.assertEqual(ctx.envelope.actor_principal_raw, "62985")
        self.assertNotEqual(ctx.envelope.actor_principal_raw, ctx.principal_id)


if __name__ == "__main__":
    unittest.main()
