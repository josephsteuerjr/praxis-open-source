"""
Реестр маршрутов (пункт 5, теневая половина).

Главное, что здесь проверяется: реестр не переписывает прошлое. Момент превращения
чата в форум из истории Telegram невосстановим — служебного сообщения об этом нет, —
поэтому всё до первого наблюдения обязано остаться `unknown`.

Запуск:  python praxis_test.py test_route_registry -v
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import telegram_routes as tr


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_routes_"))
        self._orig = tr.DIR
        tr.DIR = self.tmp / "memory" / ".state" / "group_context"

    def tearDown(self):
        tr.DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestEvidence(Base):
    def test_unknown_until_something_is_observed(self):
        self.assertEqual(tr.current("-100")["forum_status"], tr.UNKNOWN)
        self.assertEqual(tr.status_at("-100"), (tr.UNKNOWN, 0))

    def test_direct_telegram_answers_are_authoritative(self):
        tr.observe("-100", kind="channel_forum_missing", message_id=500)
        self.assertEqual(tr.current("-100")["forum_status"], tr.FALSE)
        tr.observe("-200", kind="get_forum_topics_ok", message_id=10)
        self.assertEqual(tr.current("-200")["forum_status"], tr.TRUE)

    def test_topic_opener_proves_a_real_forum(self):
        tr.observe("-300", kind="topic_opener_seen", message_id=7849,
                   detail="Открытые вопросы (швы)")
        self.assertEqual(tr.current("-300")["forum_status"], tr.TRUE)

    def test_weak_evidence_never_overrides_strong(self):
        """Объект с флагом min документирован как ненадёжный: он не имеет права
        отменять прямой ответ Telegram на GetForumTopics."""
        tr.observe("-100", kind="channel_forum_missing", message_id=1)
        tr.observe("-100", kind="update_min_entity", forum=True, message_id=2)
        self.assertEqual(tr.current("-100")["forum_status"], tr.FALSE)
        self.assertEqual(tr.current("-100")["epoch"], 1, "новой эпохи быть не должно")
        self.assertEqual(tr.current("-100")["contested"], 1, "но спор записан")

    def test_transient_failure_says_nothing(self):
        tr.observe("-100", kind="get_forum_topics_ok", message_id=1)
        tr.observe("-100", kind="rpc_unavailable", detail="FloodWaitError")
        self.assertEqual(tr.current("-100")["forum_status"], tr.TRUE,
                         "сеть упала — это не свидетельство о природе комнаты")

    def test_repeat_observation_does_not_open_an_epoch(self):
        for mid in (10, 20, 30):
            tr.observe("-100", kind="get_forum_topics_ok", message_id=mid)
        cur = tr.current("-100")
        self.assertEqual(cur["epoch"], 1)
        self.assertEqual(cur["observations"], 3)
        self.assertEqual(cur["until_message_id"], 30)


class TestGateIsDeterministic(Base):
    """⚠ Гейт зависел от ПОРЯДКА ЗАГРУЗКИ, и из-за этого слой B был мёртв в проде.

    Авторитетное свидетельство от Telegram писалось БЕЗ идентификатора сообщения,
    живое — с ним. Кто пришёл первым, тот и ставил границу эпохи; граница никогда не
    опускалась. Если первым было живое сообщение с высоким номером, все исторические
    ветки оказывались раньше границы и получали «не знаю».
    """

    HIST, LIVE = 93707, 93900

    def _live_then_backfill(self):
        tr.observe("-100", kind="entity_forum_flag", forum=False, message_id=self.LIVE)
        tr.observe("-100", kind="no_topic_openers_in_range",
                   since_message_id=89000, until_message_id=self.LIVE)

    def _backfill_then_live(self):
        tr.observe("-100", kind="no_topic_openers_in_range",
                   since_message_id=89000, until_message_id=self.LIVE)
        tr.observe("-100", kind="entity_forum_flag", forum=False, message_id=self.LIVE)

    def test_same_evidence_either_order_gives_the_same_answer(self):
        self._live_then_backfill()
        a = tr.status_at("-100", self.HIST)
        self.tearDown()
        self.setUp()
        self._backfill_then_live()
        b = tr.status_at("-100", self.HIST)
        self.assertEqual(a, b, "ответ гейта не имеет права зависеть от порядка загрузки")
        self.assertEqual(a[0], tr.FALSE, "историческая ветка обязана получить ответ")

    def test_evidence_at_an_earlier_id_widens_the_epoch_backwards(self):
        tr.observe("-100", kind="entity_forum_flag", forum=False, message_id=93900)
        self.assertEqual(tr.status_at("-100", 89500), (tr.UNKNOWN, 0))
        tr.observe("-100", kind="no_topic_openers_in_range",
                   since_message_id=89000, until_message_id=93900)
        self.assertEqual(tr.status_at("-100", 89500)[0], tr.FALSE,
                         "узнали, что режим действовал и раньше — эпоха расширяется назад")

    def test_observation_without_a_range_cannot_relabel_the_past(self):
        tr.observe("-100", kind="topic_opener_seen", message_id=500)   # тут был форум
        tr.observe("-100", kind="channel_forum_missing")               # а сейчас — нет
        self.assertEqual(tr.status_at("-100", 500)[0], tr.TRUE,
                         "наблюдение без диапазона не стирает настоящую эпоху форума")
        self.assertEqual(tr.status_at("-100", 400), (tr.UNKNOWN, 0))

    def test_gap_between_epochs_is_honest_unknown(self):
        tr.observe("-100", kind="no_topic_openers_in_range",
                   since_message_id=100, until_message_id=500)
        tr.observe("-100", kind="topic_opener_seen", message_id=900)
        self.assertEqual(tr.status_at("-100", 300)[0], tr.FALSE)
        self.assertEqual(tr.status_at("-100", 900)[0], tr.TRUE)
        self.assertEqual(tr.status_at("-100", 700), (tr.UNKNOWN, 0),
                         "смена случилась где-то в дыре, но где — мы не знаем")

    def test_newer_than_everything_observed_keeps_the_last_regime(self):
        tr.observe("-100", kind="no_topic_openers_in_range",
                   since_message_id=100, until_message_id=500)
        self.assertEqual(tr.status_at("-100", 9999)[0], tr.FALSE,
                         "режимы держатся, пока не сменятся, а смену мы бы увидели")


class TestEpochsDoNotRewriteThePast(Base):
    def test_conversion_opens_a_new_epoch_and_the_old_one_stands(self):
        # Свидетельство отвечает за ДИАПАЗОН, а не за точку: точное наблюдение на
        # одном сообщении и говорит только о нём. Историю покрывает бэкфилл, который
        # проходит диапазон целиком.
        tr.observe("-100", kind="no_topic_openers_in_range",
                   since_message_id=100, until_message_id=800)
        tr.observe("-100", kind="get_forum_topics_ok", message_id=900)
        self.assertEqual(tr.current("-100")["epoch"], 2)
        # ключ, выданный до превращения, обязан читаться в СВОЁМ режиме
        self.assertEqual(tr.status_at("-100", 150), (tr.FALSE, 1))
        self.assertEqual(tr.status_at("-100", 950), (tr.TRUE, 2))

    def test_history_before_the_first_observation_stays_unknown(self):
        """Служебного сообщения о превращении в форум в истории нет, значит момент
        невосстановим. Делать вид, что сегодняшнее состояние действовало всегда, —
        значит переписать прошлое."""
        tr.observe("-100", kind="get_forum_topics_ok", message_id=1000)
        self.assertEqual(tr.status_at("-100", 999), (tr.UNKNOWN, 0))
        self.assertEqual(tr.status_at("-100", 1000), (tr.TRUE, 1))

    def test_observation_without_a_range_answers_only_about_now(self):
        """⚠ Раньше наблюдение без границы отвечало ЗА ВСЁ — и именно поэтому ответ
        зависел от порядка загрузки. Теперь оно говорит только про «сейчас»."""
        tr.observe("-100", kind="channel_forum_missing")
        self.assertIsNone(tr.current("-100")["since_message_id"])
        self.assertEqual(tr.status_at("-100")[0], tr.FALSE, "про сейчас — отвечает")
        self.assertEqual(tr.status_at("-100", 5), (tr.UNKNOWN, 0),
                         "про конкретное сообщение без диапазона — не отвечает")


class TestItIsShadowOnly(Base):
    def test_registry_is_not_read_by_routing(self):
        """Реестр пока НИЧЕГО не решает: маршрутизация обязана вести себя так же."""
        import telegram_topics
        src = Path(telegram_topics.__file__).read_text(encoding="utf-8")
        self.assertNotIn("telegram_routes", src,
                         "маршрутизация не должна читать реестр до отдельного решения")

    def test_store_is_an_instrument_not_memory(self):
        import memory_fts
        tr.observe("-100", kind="channel_forum_missing", message_id=1)
        path = tr._path("-100")
        self.assertTrue(path.exists())
        self.assertIn(".state", path.as_posix())
        self.assertIsNone(
            memory_fts._selected_jsonl(path, self.tmp / "memory"),
            "реестр не должен попадать в её recall")

    def test_describe_is_human_readable(self):
        tr.observe("-1001240718803", kind="channel_forum_missing", message_id=1)
        self.assertIn("обычная супергруппа", tr.describe("-1001240718803"))
        tr.observe("-1004301095307", kind="topic_opener_seen", message_id=5)
        self.assertIn("форум", tr.describe("-1004301095307"))


if __name__ == "__main__":
    unittest.main()
