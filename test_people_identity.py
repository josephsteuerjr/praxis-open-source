"""Один человек — одно досье: ключ имени против расщепления памяти о людях.

01.08.2026 на живом дереве лежало ПЯТЬ файлов на одного Егора («егор», «егор-косырев»,
«егор-косырев-tatarskiy_e4pochmak», «yegor-kosyrev», «yegor-kosyrev-tatarskiy_e4pochmak»)
и три на Арета. Плодил их не сбой, а механизм: имя файла делалось из того ярлыка, под
которым человек пришёл в конкретный ход, — её слова, display-имени Telegram или подписи
квитанции с хэндлом.

Здесь охраняется граница нормализации: складываются написания ОДНОГО имени, но не
сближаются РАЗНЫЕ буквы — у неё стоит стойка «похожие имена молча не сливаю», и эти
тесты обязаны падать, если кто-то её отменит.
"""
from __future__ import annotations

import unittest

import agent
import graph
import people
import telegram_contacts


def _mk(slug: str, name: str) -> None:
    people.write(slug, name, {})


def _clean() -> None:
    for p in people.PEOPLE_DIR.glob("*.md"):
        if not p.stem.startswith("_"):
            p.unlink()
    if graph.GRAPH_MD.exists():
        graph.GRAPH_MD.unlink()


class IdentityKey(unittest.TestCase):
    def test_folds_one_name_across_scripts(self):
        self.assertEqual(people.identity_key("Егор"), people.identity_key("Yegor"))
        self.assertEqual(people.identity_key("Егор Косырев"),
                         people.identity_key("Yegor Kosyrev"))

    def test_handle_tail_is_a_caption_not_a_name(self):
        self.assertEqual(people.identity_key("Yegor Kosyrev (@tatarskiy_e4pochmak)"),
                         people.identity_key("Yegor Kosyrev"))
        self.assertEqual(people.identity_key("Егор Косырев (@tatarskiy_e4pochmak)"),
                         people.identity_key("Yegor Kosyrev"))

    def test_diacritics_fold(self):
        self.assertEqual(people.identity_key("Arête"), people.identity_key("Арете"))

    def test_transliteration_variants_fold(self):
        self.assertEqual(people.identity_key("Хабиб"), people.identity_key("Khabib"))
        self.assertEqual(people.identity_key("Цветков"), people.identity_key("Tsvetkov"))
        self.assertEqual(people.identity_key("Alex"), people.identity_key("Алекс"))
        self.assertEqual(people.identity_key("Maxim"), people.identity_key("Максим"))

    def test_different_letters_stay_different(self):
        """Стойка «молча не сливаю»: Kosyrev и Kosyrew — не одно написание."""
        self.assertNotEqual(people.identity_key("Yegor Kosyrev"),
                            people.identity_key("Yegor Kosyrew"))
        self.assertNotEqual(people.identity_key("Арет"), people.identity_key("Арете"))
        self.assertNotEqual(people.identity_key("Анна"), people.identity_key("Ана"))

    def test_empty_is_empty(self):
        self.assertEqual(people.identity_key(""), "")
        self.assertEqual(people.identity_key("   "), "")


class SlugByIdentity(unittest.TestCase):
    def setUp(self):
        _clean()

    def test_finds_single_card(self):
        _mk("yegor-kosyrev", "Yegor Kosyrev")
        self.assertEqual(people.slug_by_identity("Егор Косырев"), "yegor-kosyrev")

    def test_ambiguity_returns_nothing(self):
        """Два досье с одним ключом — уже расщепление; угадывать здесь нельзя."""
        _mk("yegor-kosyrev", "Yegor Kosyrev")
        _mk("егор-косырев", "Егор Косырев")
        self.assertEqual(people.slug_by_identity("Егор Косырев"), "")

    def test_matches_alias_line(self):
        _mk("yegor-kosyrev", "Yegor Kosyrev")
        people.add_alias("yegor-kosyrev", "Егор")
        self.assertEqual(people.slug_by_identity("Yegor"), "yegor-kosyrev")


class ResolveDoesNotSplit(unittest.TestCase):
    def setUp(self):
        _clean()
        _mk("yegor-kosyrev", "Yegor Kosyrev")

    def test_russian_name_without_any_alias(self):
        """До 01.08 «Егор» резолвился в себя и заводил второе досье."""
        self.assertEqual(graph.resolve("Егор Косырев"), "yegor-kosyrev")

    def test_receipt_label_with_handle(self):
        self.assertEqual(graph.resolve("Yegor Kosyrev (@tatarskiy_e4pochmak)"), "yegor-kosyrev")
        self.assertEqual(graph.resolve("Егор Косырев (@tatarskiy_e4pochmak)"), "yegor-kosyrev")

    def test_different_spelling_still_gets_its_own_node(self):
        self.assertEqual(graph.resolve("Yegor Kosyrew"), "yegor-kosyrew")

    def test_topic_nodes_untouched(self):
        self.assertEqual(graph.resolve("переезд из самары"), "переезд-из-самары")

    def test_unknown_person_keeps_own_slug(self):
        self.assertEqual(graph.resolve("Незнакомец"), "незнакомец")


class RememberLandsOnOneCard(unittest.TestCase):
    def setUp(self):
        _clean()
        _mk("yegor-kosyrev", "Yegor Kosyrev")

    def test_russian_name_does_not_spawn_a_second_dossier(self):
        agent.tool_remember("Егор Косырев", "любит горы")
        self.assertFalse(people.path_for("егор-косырев").exists(),
                         "написание имени не должно заводить второе досье")
        self.assertIn("любит горы", people.read_text("yegor-kosyrev"))

    def test_receipt_label_does_not_spawn_a_third(self):
        agent.tool_remember("Yegor Kosyrev (@tatarskiy_e4pochmak)", "строит Praxis")
        self.assertFalse(people.path_for("yegor-kosyrev-tatarskiy_e4pochmak").exists())
        self.assertIn("строит Praxis", people.read_text("yegor-kosyrev"))

    def test_open_loop_lands_on_the_same_card(self):
        agent.tool_remember("Егор Косырев", "хочет уехать из Самары", open_loop=True)
        self.assertFalse(people.path_for("егор-косырев").exists())
        self.assertIn("Самары", people.read_text("yegor-kosyrev"))

    def test_genuinely_new_person_still_gets_a_card(self):
        agent.tool_remember("Марина Светлова", "пишет стихи")
        self.assertTrue(people.path_for("марина-светлова").exists())


class HerDetectorSeesTheClass(unittest.TestCase):
    """Детектор дублей в её сне давал по этой паре ratio 0.00 — она никогда не
    попадала даже в предложения, хотя на живом дереве это было пять досье."""

    def setUp(self):
        _clean()

    def test_cross_script_pair_is_now_scored(self):
        import sleep
        _mk("yegor-kosyrev", "Yegor Kosyrev")
        _mk("егор-косырев", "Егор Косырев")
        score, why = sleep._pair_score("yegor-kosyrev", "егор-косырев")
        self.assertEqual(score, 1.0)
        self.assertIn("одно имя в двух написаниях", why)

    def test_pair_reaches_the_candidate_list(self):
        import sleep
        _mk("yegor-kosyrev", "Yegor Kosyrev")
        _mk("егор-косырев", "Егор Косырев")
        pairs = {frozenset((c["keep"], c["absorb"])) for c in sleep.dossier_dup_candidates()}
        self.assertIn(frozenset(("yegor-kosyrev", "егор-косырев")), pairs)

    def test_nightly_pass_still_only_proposes(self):
        """Стойка «молча не сливаю»: обнаружение выросло, полномочия — нет."""
        import sleep
        _mk("yegor-kosyrev", "Yegor Kosyrev")
        _mk("егор-косырев", "Егор Косырев")
        merged, proposed = sleep.svs_dossier_pass(allow_merge=False)
        self.assertEqual(merged, 0)
        self.assertGreaterEqual(proposed, 1)
        self.assertTrue(people.path_for("егор-косырев").exists())
        self.assertTrue(people.path_for("yegor-kosyrev").exists())

    def test_different_spelling_still_below_merge_bar(self):
        import sleep
        _mk("yegor-kosyrev", "Yegor Kosyrev")
        _mk("yegor-kosyrew", "Yegor Kosyrew")
        score, _ = sleep._pair_score("yegor-kosyrev", "yegor-kosyrew")
        self.assertLess(score, sleep.MERGE_MIN_SCORE)


class AddressBookSpeaksHerLanguage(unittest.TestCase):
    """01.08 08:31: «„Егор“ пока нет в моей адресной книге» — через два часа после
    того, как она дважды ему написала."""

    def setUp(self):
        self._rows = telegram_contacts._load()
        self._backup = {k: dict(v) for k, v in self._rows.items()}
        self._rows.clear()
        self._rows["809306689"] = {
            "id": "809306689", "display_name": "Yegor Kosyrev",
            "username": "tatarskiy_e4pochmak",
            "aliases": ["Kosyrev", "Yegor", "Yegor Kosyrev", "tatarskiy_e4pochmak"],
            "contact": True, "dialog": True, "interactions": 1192, "last_seen": 0,
        }

    def tearDown(self):
        self._rows.clear()
        self._rows.update(self._backup)

    def test_russian_first_name_finds_the_latin_row(self):
        found = telegram_contacts.candidates("Егор")
        self.assertEqual([r["id"] for r in found], ["809306689"])

    def test_exact_latin_still_wins_its_own_tier(self):
        found = telegram_contacts.candidates("Yegor Kosyrev")
        self.assertEqual([r["id"] for r in found], ["809306689"])

    def test_stranger_is_still_not_found(self):
        self.assertEqual(telegram_contacts.candidates("Совершенно Посторонний"), [])


if __name__ == "__main__":
    unittest.main()
