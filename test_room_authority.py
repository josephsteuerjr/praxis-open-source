"""
Власть над комнатой: чья она и правда ли записана.

Два долга, которые эти тесты держат закрытыми.

1. АВТОРСТВО. 27.07.2026 во всех четырёх живых комнатах стояло `mode_set_by: owner`,
   и ни один из этих режимов Егор не выбирал: метку давали коэрсия («автор не из
   списка → owner») и протокол входа, где «Егор ДОБАВИЛ её в группу» записывалось как
   «Егор выбрал ей режим». Она читала это как чужое распоряжение о себе.
2. РЫЧАГИ. Режим брался только текстовой директивой (тула нет), а `disclosure` был
   рычагом одного Егора — при том, что меняет ЕЁ голос, и слова `disclosure` она не
   видела нигде. Решение Егора 28.07: и то, и другое — в её руки.

Отдельно тут заперта ОБРАТИМОСТЬ: любой режим, поставленный ею или им, она снимает
сама. Если однажды кто-то захочет «на всякий случай» поставить забор — тесты
`test_she_may_lift_a_mode_the_owner_set` и `test_a_term_never_outlives_its_own_clock`
покраснеют раньше прода.

Запуск:  python praxis_test.py test_room_authority -v
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import rooms

# Кратно 60: `_iso` пишет профиль с точностью до минуты, и проверка границы срока
# на секундах иначе меряла бы округление, а не механику.
T0 = 1_800_000_000.0
HOUR = 3600.0

# Дословные шапки живых комнат на 27.07.2026 (прочитаны на проде). Совместимость
# проверяется на них, а не на выдуманном образце.
LIVE_NORMAL = (
    "# Комната -1003843005958\n\n"
    "mode: normal\nmode_reason: вернулась\nmode_set_by: owner\n"
    "disclosure: standard\nengagement: addressed\n\n"
    "_(контекст этой группы — заполняется по ходу)_\n"
)
LIVE_OPEN = (
    "# Комната -1001240718803\n\n"
    "mode: normal\nmode_reason: вернулась\nmode_set_by: owner\n"
    "disclosure: open\nengagement: addressed\n"
)
LIVE_DEAD = (
    "# Комната -1001152779373\n\n"
    "mode: dead\nmode_reason: вышла из Telegram\nmode_set_by: praxis\n"
    "disclosure: standard\nmembership: left\nleft_at: 2026-07-27T17:58+00:00\n"
)


class RoomsHarness(unittest.TestCase):
    """Пути rooms — в песочницу: живое memory/ тесты не касаются никогда."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_room_auth_"))
        mem = self.tmp / "memory"
        self._orig = []
        for key, value in dict(BASE=self.tmp, MEM_DIR=mem, ROOMS_DIR=mem / "rooms",
                               ALLOWLIST=mem / "rooms_allowlist.json",
                               FROZEN=mem / "frozen_chats.json",
                               CARDS_PATH=mem / ".state" / "room_cards.json").items():
            self._orig.append((key, getattr(rooms, key)))
            setattr(rooms, key, value)

    def tearDown(self):
        for key, value in self._orig:
            setattr(rooms, key, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, cid: str, raw: str) -> None:
        rooms.ROOMS_DIR.mkdir(parents=True, exist_ok=True)
        rooms.profile_path(cid).write_text(raw, encoding="utf-8")

    def _raw(self, cid: str) -> str:
        return rooms.profile_path(cid).read_text(encoding="utf-8")


class TestHonestAuthorship(RoomsHarness):
    def test_an_unnamed_author_is_never_yegor(self):
        """Корень лжи: неопознанный автор коэрсился в «owner»."""
        rooms.set_mode("-101", "quiet", set_by="какой-то новый вызывающий")
        rooms.set_mode("-102", "quiet")                     # автора не назвали вовсе
        for cid in ("-101", "-102"):
            self.assertEqual(rooms.profile_read(cid)["mode_set_by"], "unknown", cid)
            self.assertNotIn("owner", self._raw(cid),
                             f"{cid}: в файле не должно быть подставленного Егора")

    def test_a_named_author_is_kept_verbatim(self):
        for cid, who in (("-111", "owner"), ("-112", "praxis"), ("-113", "protocol")):
            rooms.set_mode(cid, "quiet", set_by=who)
            self.assertEqual(rooms.profile_read(cid)["mode_set_by"], who)

    def test_a_profile_without_the_field_is_not_attributed_to_her(self):
        """Пустое авторство читалось как «praxis» — то есть ничей режим числился её."""
        self._write("-120", "# Комната\n\nmode: quiet\ndisclosure: standard\n")
        self.assertEqual(rooms.profile_read("-120")["mode_set_by"], "unknown")

    def test_normal_has_no_author_because_it_is_no_mode(self):
        self._write("-1003843005958", LIVE_NORMAL)
        state = rooms.profile_read("-1003843005958")
        self.assertEqual(state["mode"], "normal")
        self.assertEqual(state["mode_set_by"], "",
                         "режима нет — значит, никто его и не ставил")

    def test_the_false_author_leaves_the_file_on_the_next_write(self):
        self._write("-1003843005958", LIVE_NORMAL)
        rooms.section_set("-1003843005958", "Наблюдения", "здесь тихо")
        raw = self._raw("-1003843005958")
        self.assertNotIn("mode_set_by", raw, "ложное авторство не должно пережить перезапись")
        self.assertIn("mode_reason: вернулась", raw, "факт «вернулась» — не ложь, он остаётся")
        self.assertIn("engagement: addressed", raw, "её решение 15.07 трогать нельзя")
        self.assertIn("здесь тихо", raw)

    def test_the_room_line_calls_nobody_by_name(self):
        """Режим от протокола входа не должен читаться как воля Егора."""
        line = rooms.context_from_text(
            "# Комната\n\nmode: quiet\nmode_set_by: protocol\ndisclosure: standard\n")
        self.assertIn("никто этого не выбирал", line)
        self.assertNotIn("Егор", line)

    def test_the_room_line_names_yegor_when_it_really_was_him(self):
        line = rooms.context_from_text(
            "# Комната\n\nmode: quiet\nmode_set_by: owner\ndisclosure: standard\n")
        self.assertIn("Егор так решил", line)


class TestHerModeLever(RoomsHarness):
    def test_she_sets_and_lifts_her_own_mode(self):
        ok, note = rooms.set_own_mode("-201", "quiet", reason="шумно")
        self.assertTrue(ok)
        self.assertEqual(rooms.effective_mode("-201"), "quiet")
        self.assertEqual(rooms.profile_read("-201")["mode_set_by"], "praxis")
        self.assertIn("Снимаю сама", note)

        ok, _ = rooms.set_own_mode("-201", "normal")
        self.assertTrue(ok)
        self.assertEqual(rooms.effective_mode("-201"), "normal")
        self.assertEqual(rooms.profile_read("-201")["mode_set_by"], "")

    def test_she_may_lift_a_mode_the_owner_set(self):
        """Забор «его режим снимает только он» здесь запрещён — специально."""
        rooms.set_mode("-202", "frozen", set_by="owner", reason="Егор заморозил")
        ok, _ = rooms.set_own_mode("-202", "normal")
        self.assertTrue(ok, "любой обратимый режим она вправе снять сама")
        self.assertEqual(rooms.effective_mode("-202"), "normal")
        self.assertFalse(rooms.is_frozen("-202"), "легаси-флаг обязан сняться вместе с режимом")

    def test_a_term_never_outlives_its_own_clock(self):
        ok, _ = rooms.set_own_mode("-203", "quiet", ttl_h=2, now=T0)
        self.assertTrue(ok)
        self.assertEqual(rooms.effective_mode("-203", now=T0 + 2 * HOUR - 1), "quiet",
                         "за секунду до срока режим ещё стоит")
        self.assertEqual(rooms.effective_mode("-203", now=T0 + 2 * HOUR), "normal",
                         "в момент срока — сама возвращаюсь в обычно")
        self.assertEqual(rooms.profile_read("-203")["mode_set_by"], "",
                         "после возврата в normal авторства нет")

    def test_an_observer_term_is_not_silently_dropped(self):
        """«РЕЖИМ: наблюдай 3 ч» записывался наблюдателем НАВСЕГДА и молчал об этом."""
        ok, note = rooms.set_own_mode("-204", "observer", ttl_h=3, now=T0)
        self.assertTrue(ok)
        self.assertTrue(rooms.profile_read("-204")["mode_until"].strip(),
                        "срок обязан попасть в профиль")
        self.assertIn("3 ч", note)
        self.assertEqual(rooms.effective_mode("-204", now=T0 + 3 * HOUR - 1), "observer")
        self.assertEqual(rooms.effective_mode("-204", now=T0 + 3 * HOUR), "normal")

    def test_every_applied_limit_is_named_in_what_she_sees(self):
        ok, note = rooms.set_own_mode("-205", "quiet", reason="ц" * 300, ttl_h=5, now=T0)
        self.assertTrue(ok)
        self.assertIn("5 ч", note, "срок обязан быть назван")
        self.assertIn(str(rooms.MODE_REASON_MAX), note, "обрезка причины обязана быть названа")
        self.assertEqual(len(rooms.profile_read("-205")["mode_reason"]),
                         rooms.MODE_REASON_MAX)

        ok, note = rooms.set_own_mode("-206", "normal", ttl_h=5)
        self.assertTrue(ok)
        self.assertIn("не записала", note,
                      "отброшенный срок у «обычно» тоже обязан быть назван вслух")
        self.assertFalse(rooms.profile_read("-206")["mode_until"].strip())

    def test_a_short_reason_is_never_reported_as_clipped(self):
        _, note = rooms.set_own_mode("-207", "quiet", reason="шумно")
        self.assertNotIn("обрезала", note, "честность в обе стороны: не резала — не говорю")

    def test_dead_is_not_a_mode_she_chooses(self):
        rooms.set_mode("-208", "quiet", set_by="praxis")
        ok, why = rooms.set_own_mode("-208", "dead")
        self.assertFalse(ok)
        self.assertIn("Telegram", why, "почему отказ — сказано, а не просто «нельзя»")
        self.assertEqual(rooms.effective_mode("-208"), "quiet", "отказ ничего не тронул")

    def test_a_dead_room_needs_a_real_join(self):
        rooms.set_mode("-209", "dead", set_by="praxis", reason="вышла из Telegram")
        ok, why = rooms.set_own_mode("-209", "quiet")
        self.assertFalse(ok)
        self.assertIn("join", why)

    def test_self_demote_keeps_its_old_contract(self):
        """Легаси-имя зовут agent.py и старые тесты — его возврат менять нельзя."""
        self.assertEqual(rooms.self_demote("-210", "quiet"), (True, "quiet"))
        ok, why = rooms.self_demote("-210", "чепуха")
        self.assertFalse(ok)
        self.assertIn("чепуха", why)


class TestHerDisclosureLever(RoomsHarness):
    def test_disclosure_is_her_lever_too(self):
        ok, note = rooms.set_own_disclosure("-301", "open")
        self.assertTrue(ok)
        self.assertEqual(rooms.disclosure_of("-301"), "open")
        self.assertEqual(rooms.profile_read("-301")["disclosure_set_by"], "praxis")
        self.assertIn("визитк", note, "сказано, что именно меняется в её голосе")

        ok, _ = rooms.set_own_disclosure("-301", "standard")
        self.assertTrue(ok)
        self.assertEqual(rooms.disclosure_of("-301"), "standard")
        self.assertEqual(rooms.profile_read("-301")["disclosure_set_by"], "",
                         "standard — умолчание, а не чей-то выбор")

    def test_disclosure_authorship_is_honest(self):
        rooms.set_disclosure("-302", "open", set_by="owner")
        self.assertEqual(rooms.profile_read("-302")["disclosure_set_by"], "owner")
        rooms.set_disclosure("-303", "open", set_by="кто-то")
        self.assertEqual(rooms.profile_read("-303")["disclosure_set_by"], "unknown")

    def test_an_unknown_level_changes_nothing_and_says_so(self):
        rooms.set_disclosure("-304", "open", set_by="praxis")
        ok, why = rooms.set_disclosure("-304", "публично", set_by="praxis")
        self.assertFalse(ok)
        self.assertIn("standard", why)
        self.assertIn("open", why)
        self.assertEqual(rooms.disclosure_of("-304"), "open", "неверный уровень ничего не тронул")

    def test_she_reads_the_lever_that_changes_her_voice(self):
        opened = rooms.context_from_text(
            "# Комната\n\nmode: normal\ndisclosure: open\ndisclosure_set_by: owner\n")
        self.assertIn("раскрытие open", opened)
        self.assertIn("Егор так решил", opened, "чей это выбор — тоже её знание")
        self.assertIn("мой рычаг", opened, "она обязана знать, что вправе вернуть standard")

        plain = rooms.context_from_text("# Комната\n\nmode: normal\ndisclosure: standard\n")
        self.assertNotIn("раскрытие", plain, "умолчание в промпт не лезет")


class TestLiveProfilesStillWork(RoomsHarness):
    def test_the_live_normal_rooms_keep_everything_but_the_false_author(self):
        for cid, raw in (("-1003843005958", LIVE_NORMAL), ("-1001240718803", LIVE_OPEN)):
            self._write(cid, raw)
            state = rooms.profile_read(cid)
            self.assertEqual(state["mode"], "normal", cid)
            self.assertEqual(state["engagement"], "addressed", cid)
            self.assertEqual(state["mode_set_by"], "", cid)
            self.assertEqual(rooms.membership_state(cid)["state"], "active", cid)
        self.assertEqual(rooms.disclosure_of("-1001240718803"), "open",
                         "открытая комната обязана остаться открытой")

    def test_a_dead_room_keeps_its_true_author_and_its_answer(self):
        self._write("-1001152779373", LIVE_DEAD)
        state = rooms.profile_read("-1001152779373")
        self.assertEqual(state["mode"], "dead")
        self.assertEqual(state["mode_set_by"], "praxis",
                         "здесь автор настоящий — стирать его нельзя")
        self.assertEqual(rooms.membership_state("-1001152779373")["state"], "unreachable")

    def test_room_state_is_one_card_for_tool_and_panel(self):
        rooms.set_own_mode("-401", "quiet", reason="шумно", ttl_h=4, now=T0)
        rooms.set_own_disclosure("-401", "open")
        card = rooms.room_state("-401", now=T0)
        self.assertEqual(card["mode"], "quiet")
        self.assertEqual(card["mode_word"], "тише")
        self.assertEqual(card["mode_author"], "я сама так решила")
        self.assertEqual(card["disclosure_author"], "я сама так решила")
        self.assertFalse(card["mode_expired"])
        self.assertEqual(card["self_modes"], list(rooms.SELF_MODES))
        self.assertNotIn("dead", card["self_modes"],
                         "мёртвой комнату объявляет Telegram, а не её воля")
        self.assertEqual(card["membership"]["state"], "active")
        self.assertEqual(rooms.room_state("-401", now=T0 + 5 * HOUR)["mode"], "normal",
                         "карточка показывает живой режим, а не просроченную шапку")


if __name__ == "__main__":
    unittest.main()
