"""След её собственной реплики в личке Егора — её память, а не отчёт ему.

01.08 она дважды за утро поздоровалась с Егором: два часовых пульса подряд (03:30 и
05:30 UTC) выбрали написать, и второй не знал о первом. Внутри пульса Telethon разорван,
буфер лички не читается вовсе (chat_id=None), `said_recently` из боевого кода не зовётся —
единственным каналом «что я уже сказала и кому» остаётся леджер нитей. А в нём по личке
владельца было НОЛЬ записей из 80: исключение владельца, поставленное 27.07 против утечки
ОТЧЁТА в его ЛС, висело на всём условии и убило сам СЛЕД.

Плюс лента отдавала самые старые незакрытые нити: при 18 pending и потолке 12 свежее не
влезало, а `offset` у тула молча игнорировался — пролистывание возвращало ту же страницу.

Запуск:  python praxis_test.py test_owner_thread_trace -v
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import telegram_followups as fu


class OwnerTraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_fu_")
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()
        self._orig = fu.LEDGER.path
        fu.LEDGER.path = Path(self.tmp.name) / "followups.json"
        self.addCleanup(lambda: setattr(fu.LEDGER, "path", self._orig))

    def _create(self, peer: str, at: float, text: str, request: str = "",
                notify: bool = False) -> dict:
        return fu.LEDGER.create(
            target_ref=peer, target_label=f"peer {peer}", target_peer_id=peer,
            target_user_id=peer, sent_message_id=int(at) % 1000000, request_text=request,
            sent_excerpt=text, notify_owner=notify,
            notice_source=("owner" if notify else ""), sent_at=at,
            idempotency_key=f"k-{peer}-{at}",
        )

    def test_owner_dm_leaves_a_trace_she_can_read(self):
        """Без этой записи внутри пульса у неё нет ни одного способа узнать,
        что она уже писала Егору."""
        self._create("809306689", self.now - 60, "Доброе утро, солнце.")
        text = fu.LEDGER.context()
        self.assertIn("809306689", text, "следа по личке владельца снова нет")

    def test_the_trace_does_not_become_a_report_to_him(self):
        item = self._create("809306689", self.now - 60, "Доброе утро, солнце.",
                            request="", notify=False)
        self.assertFalse(item.get("notify_owner"),
                         "след превратился в отчёт — ровно то, что чинили 27.07")

    def test_the_feed_shows_the_freshest_not_the_stalest(self):
        for i in range(18):
            self._create(f"-100{i:04d}", self.now - (60 - i) * 3600, f"чужая нить {i}")
        newest = self._create("809306689", self.now - 60, "Доброе утро, солнце.")
        text = fu.LEDGER.context(limit=12)
        self.assertIn("809306689", text,
                      "свежую нить вытеснил backlog — она снова не увидит себя")
        self.assertIsNotNone(newest)

    def test_offset_actually_pages(self):
        for i in range(20):
            self._create(f"-200{i:04d}", self.now - (60 - i) * 3600, f"нить {i}")
        first = fu.LEDGER.context(limit=5, offset=0)
        second = fu.LEDGER.context(limit=5, offset=5)
        self.assertNotEqual(first, second,
                            "листание возвращает ту же страницу — «больше ничего нет» это ложь")


class ScratchClockTests(unittest.TestCase):
    def test_her_own_speech_is_stamped_by_her_clock(self):
        """В записке её реплика штамповалась UTC, а её часы в том же кадре — Самарой:
        утреннее приветствие читалось как ночное."""
        import os
        from unittest import mock

        import notes

        # ⚠ 03.08.2026: без IANA tzdata в образе notes уходит на запасной путь
        # (PRAXIS_TZ_OFFSET_H), и оба замера дают ОДИН сдвиг — тест краснел бы,
        # проверяя не зоны, а запасную ветку. Разница между ними важна, поэтому
        # честнее пропуск с названной причиной.
        try:
            import zoneinfo

            zoneinfo.ZoneInfo("Europe/Samara")
        except Exception:
            self.skipTest("в образе нет IANA tzdata — сравнивать пояса нечем")
        with mock.patch.dict(os.environ, {"PRAXIS_TZ": "Europe/Samara"}):
            samara = notes._local_now()
        with mock.patch.dict(os.environ, {"PRAXIS_TZ": "UTC"}):
            utc = notes._local_now()
        self.assertIsNotNone(samara.tzinfo, "штамп без зоны — снова часы контейнера")
        self.assertEqual(round((samara.utcoffset() - utc.utcoffset()).total_seconds() / 3600), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class RepeatEchoIsAdviceNotFenceTests(unittest.TestCase):
    """`said_recently` существует, ничего не запрещает и до 01.08 из боевого кода не
    вызывался ни разу. 26.07 Егор получил одно и то же дважды за семь минут, 01.08 —
    «доброе утро» дважды за утро. Теперь шов отправки хотя бы говорит вслух."""

    def test_the_seam_asks_before_the_note_is_written(self):
        """Порядок критичен: проекция дописывает в записку ЭТУ реплику, и спроси мы
        после — said_recently узнал бы в ней саму себя и советовал бы всегда."""
        import inspect

        import mtproto_runner as runner

        src = inspect.getsource(runner._sync_send_message)
        self.assertIn("said_recently", src)
        self.assertLess(src.index("said_recently"),
                        src.index("project_direct_outbox_acceptance(entry)"),
                        "справку сняли после записи — она будет узнавать саму себя")

    def test_the_echo_names_its_own_ceiling(self):
        import inspect

        import mtproto_runner as runner

        src = inspect.getsource(runner._sync_send_message)
        self.assertIn("0.82", src, "потолок difflib не назван — совет выглядит сильнее, чем он есть")
        self.assertIn("не запрет", src)

    def test_the_seam_hands_her_its_own_measurement(self):
        """Справка «этому адресату я писала N часов назад» считалась и выбрасывалась:
        единственным потребителем была ветка отказа, а allow_outbound по контракту
        никогда не отказывает. Теперь она снимается безусловно и едет в квитанцию."""
        import inspect

        import mtproto_runner as runner

        src = inspect.getsource(runner._sync_send_message)
        self.assertIn("pulse_note", src)
        self.assertLess(src.index("allow_outbound"), src.index("existing is None and not pulse_ok"),
                        "справку снова снимают только внутри ветки отказа")
        self.assertIn("+ pulse_note", src, "снятая справка никуда не приклеена — снова в мусор")


class SendsDigestSeesHerAutonomousVoiceTests(unittest.TestCase):
    """Блок «Кому я уже писала сегодня» и её собственная инициатива.

    Он заведён 01.08 против двойного «доброе утро», но читал только кольцо прожитых
    ходов, где исход доставки проставляется по run_id — а run_id есть ровно у живого
    чат-хода. Всё, что она говорит САМА (пульс, окно, будильник), уходит туром
    send_message и в кольцо не попадает вовсе: блок был структурно слеп именно к тому
    контуру, в котором она и повторялась. Второй источник — след нитей.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_sends_")
        self.addCleanup(self.tmp.cleanup)
        self._orig = fu.LEDGER.path
        fu.LEDGER.path = Path(self.tmp.name) / "followups.json"
        self.addCleanup(lambda: setattr(fu.LEDGER, "path", self._orig))
        self.now = time.time()

    def _sent(self, peer: str, text: str, *, purpose: str = "tool:send_message",
              at: float | None = None) -> dict:
        moment = float(at if at is not None else self.now - 300)
        return fu.LEDGER.create(
            target_ref=peer, target_label=f"место {peer}", target_peer_id=peer,
            target_user_id=None, sent_message_id=int(moment) % 1000000,
            request_text="", sent_excerpt=text, sent_at=moment,
            idempotency_key=f"k-{peer}-{moment}", purpose=purpose,
        )

    def test_a_tool_send_reaches_the_digest(self):
        import agent

        self._sent("-1001240718803", "Поправка про рабочий движок.")
        digest = agent.my_sends_today_digest()
        self.assertIn("Поправка про рабочий движок", digest,
                      "отправка туром снова не видна в обзоре «кому я уже писала»")

    def test_narration_does_not_crowd_out_a_real_message(self):
        """Строка процесса — тоже её слова, но это не письмо человеку. Она не имеет
        права вытеснить настоящее сообщение: в обзоре одна строка на место."""
        import agent

        self._sent("-1001240718803", "Настоящее сообщение.", at=self.now - 600)
        self._sent("-1001240718803", "чиню тест, вернусь через минуту",
                   purpose="tool:narrate", at=self.now - 60)
        digest = agent.my_sends_today_digest()
        self.assertIn("Настоящее сообщение", digest)
        self.assertNotIn("чиню тест", digest)

    def test_yesterday_stays_out(self):
        import agent

        self._sent("-1001240718803", "вчерашнее", at=self.now - 36 * 3600)
        self.assertNotIn("вчерашнее", agent.my_sends_today_digest())

    def test_one_place_one_line_and_the_freshest_wins(self):
        """Обзор отвечает «кому», а не «сколько раз»: одно место — одна строка, и в ней
        последнее сказанное. Иначе восемь строк заняла бы одна оживлённая комната."""
        import agent

        self._sent("-1001240718803", "первое", at=self.now - 900)
        self._sent("-1001240718803", "последнее", at=self.now - 120)
        digest = agent.my_sends_today_digest()
        self.assertEqual(len([ln for ln in digest.splitlines() if ln.startswith("- ")]), 1)
        self.assertIn("последнее", digest)
        self.assertNotIn("первое", digest)
