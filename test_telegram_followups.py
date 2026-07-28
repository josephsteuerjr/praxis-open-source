from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import telegram_followups as followups


ABSTRACT_DL = -1001240718803
OWNER_ID = 555000100


class _Clock:
    """Управляемое время.

    `list()`/`context()` берут время сами, а срок жизни следа проверяется НА
    ГРАНИЦЕ (ровно TTL, а не «когда-нибудь протухнет») — без подменённых часов
    такой тест написать нечем.
    """

    def __init__(self, value: float = 1000.0):
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


class FollowUpLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_followups_")
        self.ledger = followups.FollowUpLedger(Path(self.tmp.name) / "followups.json")
        self.clock = _Clock()
        patcher = mock.patch.object(followups.time, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _group_thread(self, sent_message_id: int, **extra) -> dict:
        """Нить в AbstractDL — та самая комната, где случился инцидент 27.07."""
        arguments = dict(
            target_ref=str(ABSTRACT_DL),
            target_label="AbstractDL Chat (@abstractdl_chat, id 1240718803)",
            target_peer_id=ABSTRACT_DL, target_user_id=None,
            sent_message_id=sent_message_id, request_text="", sent_at=0,
        )
        arguments.update(extra)
        return self.ledger.create(**arguments)

    def _row(self, text: str, followup_id: str) -> str:
        for line in text.splitlines():
            if followup_id in line:
                return line
        self.fail(f"нить {followup_id} не попала в context():\n{text}")

    # --- существующие контракты леджера ------------------------------------

    def test_explicit_request_detection_and_owner_buffer(self):
        self.assertTrue(followups.wants_followup(
            "Напиши Жене, а когда он ответит, сообщи мне"))
        self.assertTrue(followups.wants_followup("Tell me when she replies"))
        self.assertFalse(followups.wants_followup("Напиши Жене привет"))
        self.assertEqual(
            followups.request_from_owner_buffer([
                "Егор: старое", "Praxis: хорошо",
                "Егор: Напиши Жене и дай знать, что он ответит",
            ]),
            "Напиши Жене и дай знать, что он ответит",
        )
        self.assertEqual(followups.request_from_owner_buffer([
            "Егор: сообщи, когда ответит", "Praxis: уже отправила", "Егор: спасибо",
        ]), "", "authority comes from the latest owner line, not a stale request")
        self.assertEqual(followups.request_from_owner_buffer([
            "Егор: Напиши Жене привет",
        ], explicit_only=False), "Напиши Жене привет")
        self.assertEqual(followups.request_from_owner_buffer([
            "Егор: Напиши Жене привет, но не надо сообщать, если она ответит",
        ], explicit_only=False), "")

    def test_explicit_expiry_detection_is_narrow(self):
        self.assertEqual(
            followups.explicit_expiry_seconds("Код действует 30 минут"), 1800,
        )
        self.assertEqual(
            followups.explicit_expiry_seconds("This link is valid for 2 hours"), 7200,
        )
        self.assertIsNone(followups.explicit_expiry_seconds("Напишу через 30 минут"))

    def test_expired_followup_is_not_resolved_or_shown_as_pending(self):
        item = self.ledger.create(
            target_ref="@sonya", target_label="Соня", target_peer_id=42,
            target_user_id=42, sent_message_id=100,
            request_text="Код действует 30 минут", sent_at=10,
        )
        self.assertEqual(item["expires_at"], 1810,
                         "явно названный в тексте срок сильнее и следа, и заказа")
        self.assertIsNone(self.ledger.observe_incoming(
            peer_id=42, sender_id=42, message_id=101, text="позже",
            received_at=1811,
        ))
        expired = self.ledger.list(status="expired")
        self.assertEqual([entry["id"] for entry in expired], [item["id"]])
        self.assertNotIn(item["id"], self.ledger.context(limit=3))

    def test_dm_reply_resolves_latest_matching_message_and_notifies_once(self):
        item = self.ledger.create(
            target_ref="@zhenya", target_label="Женя (@zhenya, id 42)",
            target_peer_id=42, target_user_id=42, sent_message_id=100,
            request_text="сообщи, когда ответит", sent_at=10,
            notify_owner=True, notice_source="owner",
        )
        self.assertIsNone(self.ledger.observe_incoming(
            peer_id=42, sender_id=99, message_id=101, text="чужой", received_at=11))
        self.assertIsNone(self.ledger.observe_incoming(
            peer_id=42, sender_id=42, message_id=100, text="старое", received_at=11))
        matched = self.ledger.observe_incoming(
            peer_id=42, sender_id=42, message_id=101, text="Да, договорились", received_at=12)
        self.assertEqual(matched["id"], item["id"])
        self.assertEqual(matched["response"]["message_id"], 101)
        self.assertEqual([x["id"] for x in self.ledger.pending_notifications()], [item["id"]])
        self.assertTrue(self.ledger.mark_notified(item["id"], at=13))
        self.assertFalse(self.ledger.mark_notified(item["id"], at=14))
        self.assertEqual(self.ledger.pending_notifications(), [])

    def test_group_traffic_is_not_an_answer_without_exact_reply(self):
        item = self._group_thread(55)
        self.assertIsNone(self.ledger.observe_incoming(
            peer_id=ABSTRACT_DL, sender_id=7, message_id=56, text="фон", received_at=11))
        matched = self.ledger.observe_incoming(
            peer_id=ABSTRACT_DL, sender_id=7, message_id=57, text="ответ",
            reply_to_message_id=55, received_at=12)
        self.assertEqual(matched["id"], item["id"])

    def test_replayed_dm_reply_settles_only_its_exact_obligation(self):
        older = self.ledger.create(
            target_ref="42", target_label="Person", target_peer_id=42,
            target_user_id=42, sent_message_id=100, request_text="first", sent_at=10,
        )
        newer = self.ledger.create(
            target_ref="42", target_label="Person", target_peer_id=42,
            target_user_id=42, sent_message_id=200, request_text="second", sent_at=20,
        )

        matched = self.ledger.observe_incoming(
            peer_id=42, sender_id=42, message_id=201, text="answer to first",
            reply_to_message_id=100, received_at=30,
        )
        self.assertEqual(matched["id"], older["id"])
        self.assertIsNone(self.ledger.observe_incoming(
            peer_id=42, sender_id=42, message_id=201, text="answer to first",
            reply_to_message_id=100, received_at=30,
        ))
        self.assertEqual(self.ledger.list(status="pending")[0]["id"], newer["id"])

    def test_projection_is_idempotent_by_concrete_telegram_message(self):
        arguments = dict(
            target_ref="42", target_label="Person", target_peer_id=42,
            target_user_id=42, sent_message_id=100, request_text="track", sent_at=10,
        )
        first = self.ledger.create(**arguments, idempotency_key="outbox:first")
        replay = self.ledger.create(**arguments, idempotency_key="outbox:recovery")
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(len(self.ledger.list()), 1)
        self.assertFalse(self.ledger.mark_notified(first["id"], at=11))

    def test_cancel_is_durable_and_idempotent(self):
        item = self.ledger.create(
            target_ref="42", target_label="Женя", target_peer_id=42,
            target_user_id=42, sent_message_id=10, request_text="жду", sent_at=1,
        )
        self.assertTrue(self.ledger.cancel(item["id"]))
        self.assertFalse(self.ledger.cancel(item["id"]))
        self.assertEqual(self.ledger.list()[0]["status"], "cancelled")

    def test_compaction_never_evicts_pending(self):
        old_pending = {"id": "must-stay-pending", "status": "pending"}
        settled = [
            {"id": f"settled-{index}", "status": "cancelled"}
            for index in range(510)
        ]
        self.ledger.path.write_text(json.dumps({
            "version": 1, "items": [old_pending, *settled],
        }), encoding="utf-8")

        created = self.ledger.create(
            target_ref="42", target_label="New", target_peer_id=42,
            target_user_id=42, sent_message_id=11, request_text="track", sent_at=2,
        )

        items = self.ledger.list()
        ids = {item["id"] for item in items}
        self.assertIn(old_pending["id"], ids)
        self.assertIn(created["id"], ids)
        self.assertEqual(len(items), followups._SETTLED_KEEP)
        self.assertIn(old_pending["id"], self.ledger.context(limit=3))

    # --- 27.07: след нити ≠ почта Егору ------------------------------------

    def test_trace_is_not_owner_mail(self):
        """Нить без заказчика — её память. Ответ по ней Егору не уходит."""
        trace = self.ledger.create(
            target_ref="@vika", target_label="Вика (@vika_test, id 555000333)",
            target_peer_id=555000333, target_user_id=555000333,
            sent_message_id=100, request_text="", sent_at=0,
        )
        self.assertFalse(trace["notify_owner"], "отчёт по умолчанию НЕ заказан")
        self.assertEqual(trace["notice_source"], "")
        matched = self.ledger.observe_incoming(
            peer_id=555000333, sender_id=555000333, message_id=101,
            text="ага, нужна", received_at=60)
        self.assertEqual(matched["status"], "answered",
                         "ответ обязан закрыть нить: это её память об уже сказанном")
        self.assertEqual(self.ledger.pending_notifications(), [],
                         "след в почту Егору не превращается")

        ordered = self.ledger.create(
            target_ref="@vika", target_label="Вика (@vika_test, id 555000333)",
            target_peer_id=555000333, target_user_id=555000333,
            sent_message_id=200, request_text="сообщи, когда ответит", sent_at=0,
            notify_owner=True, notice_source="owner",
        )
        self.ledger.observe_incoming(
            peer_id=555000333, sender_id=555000333, message_id=201,
            text="и ещё вот", received_at=61)
        self.assertEqual([x["id"] for x in self.ledger.pending_notifications()],
                         [ordered["id"]], "заказанный отчёт обязан уйти")

    def test_owner_answer_is_recorded_but_never_mailed_back(self):
        """Живой инцидент 27.07 02:29→02:32, воспроизведённый построчно."""
        thread = self._group_thread(
            94243, request_text="Пракс, в абстракте ты написала «у меня на Opus 5»",
            notify_owner=True, notice_source="owner",
        )
        matched = self.ledger.observe_incoming(
            peer_id=ABSTRACT_DL, sender_id=OWNER_ID, message_id=94244,
            text="да, вот так и есть", reply_to_message_id=94243,
            sender_name="Yegor Kosyrev", sender_is_owner=True, received_at=120,
        )
        self.assertEqual(matched["id"], thread["id"], "матч обязан состояться")
        self.assertEqual(matched["status"], "answered",
                         "она должна знать, что ответ пришёл")
        self.assertEqual(matched["notice_skipped"], "ответил сам Егор")
        self.assertEqual(matched["notice_skipped_at"], 120)
        self.assertEqual(self.ledger.pending_notifications(), [],
                         "пересылать человеку его собственную реплику нечего")
        self.assertIn("письма не будет: ответил сам Егор", self.ledger.context())

        # Контрольная сторона границы: ответил НЕ Егор — письмо обязано уйти.
        other = self._group_thread(
            94250, request_text="сообщи, когда ответит",
            notify_owner=True, notice_source="owner")
        self.ledger.observe_incoming(
            peer_id=ABSTRACT_DL, sender_id=555000333, message_id=94251,
            text="ответ Ареты", reply_to_message_id=94250,
            sender_name="Арет", sender_is_owner=False, received_at=130,
        )
        self.assertEqual([x["id"] for x in self.ledger.pending_notifications()],
                         [other["id"]])

    def test_answering_name_is_carried_and_never_invented(self):
        named = self._group_thread(94243, notify_owner=True, notice_source="owner")
        matched = self.ledger.observe_incoming(
            peer_id=ABSTRACT_DL, sender_id=OWNER_ID, message_id=94244,
            text="ответ", reply_to_message_id=94243,
            sender_name="Yegor Kosyrev", received_at=120,
        )
        self.assertEqual(matched["id"], named["id"])
        self.assertEqual(matched["response"]["sender_name"], "Yegor Kosyrev")

        anonymous = self._group_thread(94250)
        matched = self.ledger.observe_incoming(
            peer_id=ABSTRACT_DL, sender_id=555, message_id=94251,
            text="ответ", reply_to_message_id=94250, received_at=130,
        )
        self.assertEqual(matched["id"], anonymous["id"])
        self.assertEqual(matched["response"]["sender_name"], "",
                         "не знаю кто — это пусто; выдумывать здесь нечего")
        self.assertNotIn("AbstractDL", matched["response"]["sender_name"],
                         "метка ЧАТА именем ответившего быть не может: чат не отвечает")

    def test_trace_expires_by_named_ttl_but_ordered_report_never_does(self):
        ttl = followups.TRACE_TTL_SEC
        trace = self._group_thread(1)
        ordered = self._group_thread(2, notify_owner=True, notice_source="owner")
        self.assertEqual(trace["expires_at"], ttl, "у следа срок есть и он назван")
        self.assertIsNone(ordered["expires_at"],
                          "заказ по возрасту не гаснет (правило 4)")

        self.clock.value = ttl - 1
        statuses = {x["id"]: x["status"] for x in self.ledger.list()}
        self.assertEqual(statuses[trace["id"]], "pending", "граница: за секунду до")

        self.clock.value = ttl
        statuses = {x["id"]: x["status"] for x in self.ledger.list()}
        self.assertEqual(statuses[trace["id"]], "expired", "граница: ровно в срок")
        self.assertEqual(statuses[ordered["id"]], "pending")

        self.clock.value = ttl * 100
        statuses = {x["id"]: x["status"] for x in self.ledger.list()}
        self.assertEqual(statuses[ordered["id"]], "pending",
                         "сто сроков спустя обязательство всё ещё живо")

    def test_praxis_can_arm_and_disarm_the_report_herself(self):
        thread = self._group_thread(94243)
        self.assertIsNotNone(thread["expires_at"])

        armed = self.ledger.set_notice(thread["id"], True)
        self.assertTrue(armed["notify_owner"])
        self.assertEqual(armed["notice_source"], "praxis")
        self.assertIsNone(armed["expires_at"], "заказ снимает возрастной срок")

        self.ledger.observe_incoming(
            peer_id=ABSTRACT_DL, sender_id=555, message_id=94244, text="ответ",
            reply_to_message_id=94243, sender_name="Арет", received_at=120)
        self.assertEqual([x["id"] for x in self.ledger.pending_notifications()],
                         [thread["id"]])

        disarmed = self.ledger.set_notice(thread["id"], False)
        self.assertFalse(disarmed["notify_owner"])
        self.assertEqual(disarmed["notice_source"], "")
        self.assertEqual(self.ledger.pending_notifications(), [],
                         "выключила — нить остаётся её следом")

        # Егор ответил сам → отчёта нет; но если она решит иначе — она автор.
        owner_thread = self._group_thread(
            94250, notify_owner=True, notice_source="owner")
        self.ledger.observe_incoming(
            peer_id=ABSTRACT_DL, sender_id=OWNER_ID, message_id=94251, text="его же слова",
            reply_to_message_id=94250, sender_is_owner=True, received_at=130)
        self.assertEqual(self.ledger.pending_notifications(), [])
        revived = self.ledger.set_notice(owner_thread["id"], True)
        self.assertEqual(revived["notice_skipped"], "")
        self.assertEqual([x["id"] for x in self.ledger.pending_notifications()],
                         [owner_thread["id"]])

        self.assertTrue(self.ledger.cancel(owner_thread["id"]))
        self.assertIsNone(self.ledger.set_notice(owner_thread["id"], True),
                          "на закрытой нити рычаг честно отвечает «нет такой»")
        self.assertIsNone(self.ledger.set_notice("tgfu_нет_такой", True))

    def test_set_notice_never_overrides_an_explicitly_stated_validity(self):
        thread = self.ledger.create(
            target_ref="@sonya", target_label="Соня", target_peer_id=42,
            target_user_id=42, sent_message_id=100,
            request_text="Код действует 30 минут", sent_at=0,
        )
        armed = self.ledger.set_notice(thread["id"], True)
        self.assertEqual(armed["expires_at"], 1800,
                         "срок, названный словами в самом сообщении, сильнее заказа")

    def test_compaction_does_not_protect_unnotifiable_answers_forever(self):
        orphan = {"id": "answered-no-order", "status": "answered", "notified_at": None}
        owner_answered = {
            "id": "answered-by-owner", "status": "answered", "notified_at": None,
            "notify_owner": True, "notice_skipped": "ответил сам Егор",
        }
        ordered = {
            "id": "answered-ordered", "status": "answered", "notified_at": None,
            "notify_owner": True,
        }
        settled = [
            {"id": f"settled-{index}", "status": "cancelled"} for index in range(510)
        ]
        self.ledger.path.write_text(json.dumps({
            "version": 1, "items": [orphan, owner_answered, ordered, *settled],
        }), encoding="utf-8")

        self.ledger.create(
            target_ref="42", target_label="New", target_peer_id=42,
            target_user_id=42, sent_message_id=11, request_text="track", sent_at=2,
        )

        ids = {item["id"] for item in self.ledger.list()}
        self.assertNotIn(orphan["id"], ids,
                         "«отвечено, отчёта не будет» не имеет права жить вечно")
        self.assertNotIn(owner_answered["id"], ids)
        self.assertIn(ordered["id"], ids, "невыполненное обязательство неприкосновенно")
        self.assertEqual(len(self.ledger.list()), followups._SETTLED_KEEP)

    def test_context_names_its_caps_and_who_ordered_the_report(self):
        trace = self._group_thread(
            1, sent_excerpt="Поправка: мой рабочий движок gpt-5.6-sol",
            request_text="слова Егора, а не мои")
        ordered = self._group_thread(2, notify_owner=True, notice_source="owner")
        mine = self._group_thread(3, notify_owner=True, notice_source="praxis")

        text = self.ledger.context()
        self.assertIn(f"не больше {followups.CONTEXT_LIMIT}", text)
        self.assertIn("показываю 3 нитей из 3", text)
        self.assertIn(f"гаснет через {followups.TRACE_TTL_SEC / 3600:.0f}ч", text)
        self.assertIn("заказанный отчёт не гаснет никогда", text)

        self.assertIn("отчёт Егору: нет — это мой след", self._row(text, trace["id"]))
        self.assertIn("повод отправки: Поправка: мой рабочий движок",
                      self._row(text, trace["id"]))
        self.assertIn("отчёт Егору: заказан им словами", self._row(text, ordered["id"]))
        self.assertIn("отчёт Егору: мой — я включила сама", self._row(text, mine["id"]))

    def test_context_names_the_truncation_it_actually_applies(self):
        """Раннер отдаёт текст целиком, доверяя леджеру НАЗВАТЬ оба реза.

        Граница: числа в шапке обязаны совпасть с фактическими срезами, иначе
        сдвиг `[:240]`/`[:500]` станет новым молчаливым пределом (правило 2).
        """
        long_text = "я" * 900
        item = self._group_thread(1, sent_excerpt=long_text)
        self.assertEqual(len(item["sent_excerpt"]), 500, "рез записи = 500")
        row = self._row(self.ledger.context(), item["id"])
        gist = row.split("повод отправки: ", 1)[1].split(";", 1)[0]
        self.assertEqual(len(gist), 240, "рез строки = 240")
        header = self.ledger.context().splitlines()[0]
        self.assertIn("до 240 симв. в строке", header)
        self.assertIn("до 500 в самой записи", header)

    def test_broken_trace_ttl_env_falls_back_instead_of_killing_the_runner(self):
        """Урок forge.py:128: битое значение в .deploy.env роняло весь раннер."""
        for raw in ("", "   ", "три часа", "-5", "0", "nan-ish"):
            with self.subTest(value=raw):
                with mock.patch.dict(
                        os.environ, {"PRAXIS_FOLLOWUP_TRACE_TTL_SEC": raw}):
                    self.assertEqual(followups._trace_ttl(), 259200.0)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRAXIS_FOLLOWUP_TRACE_TTL_SEC", None)
            self.assertEqual(followups._trace_ttl(), 259200.0)
        with mock.patch.dict(os.environ, {"PRAXIS_FOLLOWUP_TRACE_TTL_SEC": "3600"}):
            self.assertEqual(followups._trace_ttl(), 3600.0)

    def test_explicit_only_is_the_default_for_the_owner_buffer(self):
        """Все 11 owner-записей прода — вот такие; отчёта он не заказывал ни разу."""
        for line in ("Егор: им отправить", "Егор: [Голосовое]",
                     "Егор: предложи Вике помощь с эксельками, если ей нужно"):
            with self.subTest(line=line):
                self.assertEqual(followups.request_from_owner_buffer([line]), "")
        self.assertEqual(
            followups.request_from_owner_buffer(
                ["Егор: Напиши Жене и дай знать, что он ответит"]),
            "Напиши Жене и дай знать, что он ответит")


if __name__ == "__main__":
    unittest.main()
