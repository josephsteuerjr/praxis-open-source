"""
Тесты PASS 4, слой 1: часы (один тик), группа mention/reply-only, анти-повтор,
медиа-конверт, EN control-plane, снос легаси. Герметичны.

Запуск:  python praxis_test.py test_pass4 -v
"""

from __future__ import annotations

import asyncio
import os
import re
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

# Раннер строит TelegramClient на импорте: дать безвредные креды и сессию во временном
# каталоге, чтобы импорт не падал и не сорил session-файлами в репо.
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test")
os.environ.setdefault("TELEGRAM_SESSION",
                      str(Path(tempfile.gettempdir()) / "praxis_pass4_test_session"))

import agent  # noqa: E402
import notes  # noqa: E402
import reflex  # noqa: E402

try:
    import mtproto_runner as mr
    _RUNNER = True
except Exception:  # нет telethon / нет event loop — тесты раннера скипаются
    mr = None
    _RUNNER = False

from test_perceive import Base, FakeClient  # noqa: E402  (тот же герметичный харнесс)


def _msg(**kw):
    """Фейковое telethon-сообщение: только нужные атрибуты (обвязка на getattr-дефолтах)."""
    return types.SimpleNamespace(**kw)


def _person(name):
    return types.SimpleNamespace(first_name=name, last_name=None, username=None)


# --------------------------------------------------------------------------- #
#  Часы: один тик вместо четырёх циклов
# --------------------------------------------------------------------------- #

@unittest.skipUnless(_RUNNER, "нет telethon — тесты раннера в контейнере")
class TestClock(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_jobs_table_has_current_single_clock_cares(self):
        jobs = mr._clock_jobs()
        self.assertEqual(set(jobs),
                         {"buffers", "schedule", "sleep", "control", "immune",
                          "absence", "media", "formation", "followups", "social_pulse",
                          "membership", "direct_outbox", "text_outbox",
                          "owner_delivery",
                          "durable_resume", "computer_inventory",
                          "group_context_backfill", "reap_orphans",
                          "selfdev_reconcile", "forge_wake", "forge_events"})

    def test_startup_deadlines_only_run_persisted_catchup_checks(self):
        fired = []

        def care(name):
            async def run():
                fired.append(name)
            return run

        jobs = {
            "durable_resume": (45.0, care("durable_resume")),
            "social_pulse": (3600.0, care("social_pulse")),
            "computer_inventory": (3600.0, care("computer_inventory")),
            "sleep": (1800.0, care("sleep")),
            "heartbeat": (7200.0, care("heartbeat")),
            "schedule": (30.0, care("schedule")),
            "disabled": (0.0, care("disabled")),
        }
        original_boundary = mr.social_pulse.next_due_at
        mr.social_pulse.next_due_at = lambda *, now=None, path=None: float(now)
        try:
            next_at = mr._clock_initial_deadlines(100.0, jobs)
        finally:
            mr.social_pulse.next_due_at = original_boundary

        self.assertEqual(next_at["durable_resume"], 100.0)
        self.assertEqual(next_at["social_pulse"], 100.0)
        self.assertEqual(next_at["computer_inventory"], 100.0)
        self.assertEqual(next_at["sleep"], 1900.0)
        self.assertEqual(next_at["heartbeat"], 7300.0)
        self.assertEqual(next_at["schedule"], 130.0)
        self.assertNotIn("disabled", next_at)

        out = self._run(mr._clock_pass(100.0, next_at, jobs))
        self.assertEqual(
            out, ["durable_resume", "social_pulse", "computer_inventory"],
        )
        self.assertEqual(fired, out)

    def test_restart_before_hourly_boundary_keeps_social_pulse_window(self):
        """Промежуточный пасс, пункт C: рестарт за 5 минут до часового окна не должен
        уносить social_pulse на полный период — дедлайн остаётся исходной границей."""
        fired = []

        async def pulse():
            fired.append("social_pulse")

        jobs = {"social_pulse": (3600.0, pulse)}
        boundary = 10_300.0  # last_started + interval; рестарт в 10_000 — за 300с до неё
        original_boundary = mr.social_pulse.next_due_at
        mr.social_pulse.next_due_at = (
            lambda *, now=None, path=None: max(float(now), boundary)
        )
        try:
            next_at = mr._clock_initial_deadlines(10_000.0, jobs)
        finally:
            mr.social_pulse.next_due_at = original_boundary

        self.assertEqual(next_at["social_pulse"], boundary)
        # стартовый удар часов НЕ жжёт попытку (begin бы отказал и окно бы уехало)
        self._run(mr._clock_pass(10_000.0, next_at, jobs))
        self.assertEqual(fired, [])
        # на исходной границе окно срабатывает — не на рестарт+период (13_600)
        self._run(mr._clock_pass(boundary, next_at, jobs))
        self.assertEqual(fired, ["social_pulse"])
        self.assertEqual(next_at["social_pulse"], boundary + 3600.0)

    def test_durable_resume_care_uses_strict_agent_entrypoint_without_task_window(self):
        called = []
        original_resume = mr.agent.resume_durable_runs
        original_window = mr._task_window

        def resume(*, limit):
            called.append(limit)
            return [{
                "run_id": "run-1", "plan_kind": "owner_control",
                "status": "noop", "phase": "control",
            }]

        async def forbidden_window(*_args, **_kwargs):
            raise AssertionError("clock recovery must not create a fresh task window")

        mr.agent.resume_durable_runs = resume
        mr._task_window = forbidden_window
        try:
            self._run(mr._durable_resume_once())
        finally:
            mr.agent.resume_durable_runs = original_resume
            mr._task_window = original_window
        self.assertEqual(called, [20])

    def test_due_fires_and_reschedules(self):
        fired = []

        async def care():
            fired.append(1)

        jobs = {"j": (10.0, care)}
        next_at = {"j": 100.0}
        out = self._run(mr._clock_pass(100.0, next_at, jobs))
        self.assertEqual(out, ["j"])
        self.assertEqual(next_at["j"], 110.0, "срок должен перевзвестись на now+период")
        out2 = self._run(mr._clock_pass(105.0, next_at, jobs))
        self.assertEqual(out2, [], "до срока забота не дёргается")
        self.assertEqual(len(fired), 1)

    def test_disabled_job_never_fires(self):
        async def care():
            raise AssertionError("выключенная забота не должна дёргаться")

        jobs = {"off": (0.0, care)}
        out = self._run(mr._clock_pass(1e12, {}, jobs))
        self.assertEqual(out, [])

    def test_failing_job_does_not_block_others(self):
        ok = []

        async def bad():
            raise RuntimeError("boom")

        async def good():
            ok.append(1)

        jobs = {"a_bad": (5.0, bad), "b_good": (5.0, good)}
        next_at = {"a_bad": 0.0, "b_good": 0.0}
        out = self._run(mr._clock_pass(50.0, next_at, jobs))
        self.assertEqual(set(out), {"a_bad", "b_good"})
        self.assertEqual(ok, [1], "падение одной заботы не должно валить остальные")
        self.assertEqual(next_at["a_bad"], 55.0, "срок упавшей всё равно перевзводится")

    def test_legacy_loops_gone(self):
        for name in ("_buf_flusher", "_scheduler", "_consolidator", "_heartbeat"):
            self.assertFalse(hasattr(mr, name), f"легаси-цикл {name} должен быть удалён")


# --------------------------------------------------------------------------- #
#  Группа — только по адресу (@упоминание/реплай/имя)
# --------------------------------------------------------------------------- #

@unittest.skipUnless(_RUNNER, "нет telethon — тесты раннера в контейнере")
class TestGroupWake(unittest.TestCase):
    def test_group_background_never_wakes(self):
        for decision in ("maybe", "reply"):
            self.assertFalse(mr._should_wake(False, False, decision),
                             "фон в группе не должен будить проход")

    def test_group_addressed_wakes(self):
        self.assertTrue(mr._should_wake(False, True, "reply"))
        self.assertTrue(mr._should_wake(False, True, "maybe"))

    def test_direct_or_addressed_wakes_even_if_content_classifier_says_ignore(self):
        self.assertTrue(mr._should_wake(True, True, "ignore"))
        self.assertTrue(mr._should_wake(False, True, "ignore"))
        self.assertFalse(mr._should_wake(False, False, "ignore"))

    def test_dm_wakes_without_address(self):
        self.assertTrue(mr._should_wake(True, False, "reply"))


class TestReflex(unittest.TestCase):
    def test_named_short_text_is_reply(self):
        self.assertEqual(reflex.triage("Пракс?", is_private=False, named=True), "reply")

    def test_sticker_only_is_noise(self):
        self.assertEqual(reflex.triage("", is_private=True, media="[Стикер]"), "ignore")

    def test_media_only_dm_wakes(self):
        self.assertEqual(reflex.triage("", is_private=True, media="[Изображение]"), "reply")

    def test_media_only_group_is_maybe(self):
        self.assertEqual(reflex.triage("", is_private=False, media="[Голосовое]"), "maybe")

    def test_ack_is_visible_when_zero_threshold_disables_content_suppression(self):
        self.assertEqual(reflex.triage("ок", is_private=False), "maybe")


# --------------------------------------------------------------------------- #
#  Медиа-конверт
# --------------------------------------------------------------------------- #

@unittest.skipUnless(_RUNNER, "нет telethon — тесты раннера в контейнере")
class TestMediaEnvelope(unittest.TestCase):
    def test_media_tags(self):
        cases = [
            (_msg(photo=object()), "[Изображение]"),
            (_msg(voice=object()), "[Голосовое]"),
            (_msg(video_note=object()), "[Видеосообщение]"),
            (_msg(sticker=object(), video=object()), "[Стикер]"),  # стикер прежде видео
            (_msg(video=object()), "[Видео]"),
            (_msg(document=object(), file=types.SimpleNamespace(name="отчёт.pdf")),
             "[Документ: отчёт.pdf]"),
            (_msg(document=object()), "[Документ]"),
            (_msg(), ""),
        ]
        for m, want in cases:
            self.assertEqual(mr._media_tag(m), want)

    def test_format_messages_media_and_reply_marker(self):
        vasya = _person("Вася")
        msgs = [
            _msg(id=1, message="смотри что нашёл", sender=vasya, out=False),
            _msg(id=2, message="", photo=object(), sender=vasya, out=False),
            _msg(id=3, message="класс", sender=None, out=True, reply_to_msg_id=2),
            _msg(id=4, message="а это?", sender=vasya, out=False, reply_to_msg_id=99),
        ]
        lines = mr._format_messages(msgs)
        self.assertEqual(lines[0], "Вася: смотри что нашёл")
        self.assertEqual(lines[1], "Вася: [Изображение]")
        self.assertEqual(lines[2], "Praxis (в ответ Вася): класс")
        self.assertEqual(lines[3], "Вася (в ответ): а это?", "цель вне выборки — маркер без имени")

    def test_media_with_caption(self):
        m = _msg(id=1, message="это мы в горах", photo=object(), sender=_person("Аня"), out=False)
        self.assertEqual(mr._format_messages([m])[0], "Аня: [Изображение] это мы в горах")

    def test_text_only_skips_empty(self):
        self.assertEqual(mr._format_messages([_msg(message="", sender=None)]), [])


# --------------------------------------------------------------------------- #
#  Анти-повтор: записка хранит суть, said_recently сверяет по существу
# --------------------------------------------------------------------------- #

class TestSaidRecently(Base):
    def test_verbatim_repeat_detected(self):
        notes.append("-1", "трёп · реплика · сказала: «кэш греет вход, это нормально»")
        self.assertTrue(notes.said_recently("-1", "Кэш греет вход — это нормально!"))

    def test_truncated_gist_matches_long_text(self):
        long = "да я уже говорила: " + "кэш прогревается на первом проходе и потом отдаёт статику " * 3
        notes.append("-1", f"т · реплика · сказала: «{long[:notes.SAID_GIST_CHARS]}»")
        self.assertTrue(notes.said_recently("-1", long))

    def test_different_text_passes(self):
        notes.append("-1", "т · реплика · сказала: «кэш греет вход»")
        self.assertFalse(notes.said_recently("-1", "а погода сегодня отличная, пойдём гулять?"))

    def test_empty_and_absent(self):
        self.assertFalse(notes.said_recently("-1", ""))
        self.assertFalse(notes.said_recently("-nope", "что-нибудь"))


class TestAuthoredRepeat(Base):
    def test_group_exact_dup_is_not_silently_suppressed(self):
        self._client("норм ответ")
        out = agent.voice_turn("-100", "сосед: как дела?", speaker=None, is_dm=False)
        self.assertEqual(out, "норм ответ")

    def test_voice_turn_records_gist(self):
        self._client("ну смотри, кэш греет вход")
        out = agent.voice_turn("777", "егор: расскажи про кэш", speaker=None, is_owner=True)
        self.assertEqual(out, "ну смотри, кэш греет вход")
        # ⚠ Ход АВТОРСТВА больше не пишет «сказала»: между авторством и приёмкой лежат
        # отмена преемником, privacy hold, permanent failure и рестарт. Заметку пишет
        # проекция расписки — проверяем обе половины, чтобы не выяснилось, что мы просто
        # выкинули память вместо того, чтобы привязать её к факту.
        self.assertNotIn("сказала (голос)", notes.read("777") or "")

        # Вторая половина — на РЕАЛЬНОМ ходе, а не на выдуманном run_id: побочные
        # эффекты приёмки случаются ровно на переходе исхода, и без записи хода
        # переходить нечему. Раньше здесь стоял произвольный "run-gist", тест был
        # зелёным и не проверял связь расписки с ходом — та же болезнь, что мы лечим.
        import turns
        turn = turns.begin(kind="chat", chat_id="777", scope="owner", who="Егор")
        turn["out"] = "ну смотри, кэш греет вход"
        turn["run_id"] = "run-gist"
        turns.record(turn)

        agent.project_delivery_outcome("run-gist", "accepted",
                                       text="ну смотри, кэш греет вход", chat_id="777")
        self.assertIn("сказала (голос): «ну смотри, кэш греет вход»", notes.read("777"))
        self.assertEqual(turns.recent(1, scope="owner", chat_id="777")[-1]["delivery"],
                         "accepted")

        # И ровно один раз: повторная расписка не должна писать заметку заново.
        before = (notes.read("777") or "").count("сказала (голос)")
        agent.project_delivery_outcome("run-gist", "accepted",
                                       text="ну смотри, кэш греет вход", chat_id="777")
        self.assertEqual((notes.read("777") or "").count("сказала (голос)"), before,
                         "живой путь и путь восстановления не должны дублировать заметку")


# --------------------------------------------------------------------------- #
#  EN control-plane + снос легаси
# --------------------------------------------------------------------------- #

class TestControlPlaneEnglish(unittest.TestCase):
    def test_frames_have_no_cyrillic(self):
        for name in ("_HEARTBEAT_FRAME", "_TASK_WINDOW_FRAME", "_SLEEP_PROMPT"):
            frame = getattr(agent, name)
            self.assertIsNone(re.search(r"[а-яё]", frame, re.IGNORECASE),
                              f"{name} должен быть английским (control-plane)")

    def test_sleep_prompt_asks_russian_output(self):
        self.assertIn("in Russian", agent._SLEEP_PROMPT, "вывод остаётся русским")

    def test_heartbeat_frame_leaves_initiative_and_message_choice_to_praxis(self):
        self.assertIn("your own initiative window", agent._HEARTBEAT_FRAME)
        self.assertIn("check whether somebody replied", agent._HEARTBEAT_FRAME)
        self.assertIn("this frame does not rank", agent._HEARTBEAT_FRAME)
        for coercive in ("Don't send Yegor status reports", "Silence is presence",
                         "be frugal", "Nothing to say — stay silent"):
            self.assertNotIn(coercive, agent._HEARTBEAT_FRAME)


class TestLegacyGone(unittest.TestCase):
    def test_gate_removed(self):
        for name in ("gate", "GATE_SYSTEM", "GATEKEEPER_MODEL", "GATE_MAX_TOKENS"):
            self.assertFalse(hasattr(agent, name), f"легаси {name} должен быть удалён")

    def test_legacy_files_removed(self):
        repo = Path(agent.__file__).resolve().parent
        self.assertFalse((repo / "telegram_bot.py").exists())
        self.assertFalse((repo / "soul" / "skills" / "family_help_bridge.md").exists())

    def test_skills_index_honest(self):
        repo = Path(agent.__file__).resolve().parent
        idx = (repo / "soul" / "skills" / "INDEX.md").read_text(encoding="utf-8")
        self.assertNotIn("family_help_bridge", idx, "инвентарь навыков должен быть честным")


if __name__ == "__main__":
    unittest.main(verbosity=2)
