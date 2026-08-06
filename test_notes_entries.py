"""Записка комнаты — это ЗАПИСИ, а не строки, и окно обязано считать в них.

⚠ 03.08.2026. `read()` брала `lines[-n:]`, а `append()` пишет запись со штампом
«ЧЧ:ММ · », внутри которой её реплика (до SAID_GIST_CHARS символов) свободно содержит
переводы строк. Замер на живых записках прода: 1078 строк на 647 записей, **52% записей
многострочные**. Значит окно регулярно приземлялось в середину записи.

Цена была двойная:

1. ЧТЕНИЕ. Дайджест комнат просит по три «строки» на комнату — и в трёх комнатах из
   шести она читала безголовые обломки: «По»», «- самопроверка хор»»,
   «1. **Указатель без адреса** — «здесь», «в этой ветке», «сейч». Ни времени, ни начала,
   ни адресата. Это её единственное автономное пробуждение читало собственную память
   мусором.
2. ХРАНЕНИЕ. `_collapse` резал файл ТОЖЕ по строкам, поэтому обломок не просто
   показывался — он таким и ложился на диск, и целой записи было уже не восстановить.

И третье, латентное: `said_recently` искал цитату «…» ПОСТРОЧНО, поэтому реплика,
открывающая кавычку на первой строке и закрывающая на третьей, была ему невидима вовсе.
Проба на живом файле: её собственные слова, поданные обратно дословно, дали False.
Из боевого кода эта справка намеренно не зовётся (`test_rails_truth` это стережёт), так
что вред был латентным — но слепой механизм анти-повтора чинится, а не оставляется.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import notes

#: Её реальная форма из прода: штамп, потом реплика с переводами строк внутри кавычек.
MULTI = ('20:36 · сказала (голос): «Прочитала. Это не столько дневник облака,\n'
         'сколько руководство по инженерии для автора с амнезией.\n'
         'Главный тезис такой: следующая сессия приходит к коду чужой»')
SINGLE = '21:00 · сказала (голос): «Короткая реплика без переводов строк»'


class ScratchIsSlicedByEntries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="notes_entries_")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "memory" / ".scratch").mkdir(parents=True)
        for attr, value in (("BASE", root), ("SCRATCH_DIR", root / "memory" / ".scratch")):
            patcher = mock.patch.object(notes, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write(self, chat: str, text: str) -> Path:
        p = notes.path_for(chat)
        p.write_text(text + "\n", encoding="utf-8")
        return p

    # ------------------------------------------------------------------ чтение
    def test_one_entry_means_the_whole_entry(self):
        """Окно в одну запись отдаёт запись целиком, а не её последнюю строку."""
        self._write("777", MULTI + "\n" + SINGLE)
        self.assertEqual(notes.read("777", 1), SINGLE)
        out = notes.read("777", 2)
        self.assertIn("Прочитала. Это не столько дневник облака", out,
                      "голова записи потеряна — это и был дефект")
        self.assertIn("следующая сессия приходит к коду чужой", out)

    def test_every_shown_line_belongs_to_a_stamped_entry(self):
        """Ни одна показанная строка не может висеть без своей головы.

        Прямая проверка того, что видела она: обломок начинался с середины чужой фразы.
        """
        self._write("777", MULTI + "\n" + SINGLE)
        for window in (1, 2, 3, 8):
            out = notes.read("777", window)
            if not out:
                continue
            self.assertRegex(out.splitlines()[0], r"^\d{1,2}:\d{2} · ",
                             f"окно {window}: первая строка — безголовый обломок")

    def test_a_headless_head_of_file_is_dropped_not_shown(self):
        """Файл, уже порезанный прежним схлопыванием, не показывает свой обломок."""
        broken = "сколько руководство по инженерии для автора с амнезией.\n" + SINGLE
        self._write("777", broken)
        self.assertEqual(notes.read("777", 8), SINGLE)

    def test_a_note_without_any_stamp_survives_whole(self):
        """Записка старого формата отдаётся как есть — починка не съедает её память."""
        plain = "первая строка\nвторая строка"
        self._write("777", plain)
        self.assertEqual(notes.read("777", 8), plain)

    # --------------------------------------------------------------- хранение
    def test_collapse_never_leaves_half_an_entry_on_disk(self):
        """Схлопывание резало по строкам — и обломок ЛОЖИЛСЯ на диск необратимо."""
        p = self._write("777", "\n".join([MULTI, SINGLE, MULTI, SINGLE]))
        self.assertTrue(notes._collapse(p, 2))
        body = p.read_text(encoding="utf-8").strip()
        self.assertRegex(body.splitlines()[0], r"^\d{1,2}:\d{2} · ",
                         "на диске остался хвост записи без головы")
        self.assertEqual(len(notes._entries(body.splitlines())), 2)

    def test_collapse_keeps_a_short_note_untouched(self):
        p = self._write("777", SINGLE)
        before = p.read_text(encoding="utf-8")
        self.assertTrue(notes._collapse(p, 8))
        self.assertEqual(p.read_text(encoding="utf-8"), before)

    # ------------------------------------------------------- анти-повтор
    def test_said_recently_sees_a_multi_line_utterance(self):
        """52% её записей многострочные — построчный поиск цитаты их не видел вовсе."""
        self._write("777", MULTI)
        said = ("Прочитала. Это не столько дневник облака,\n"
                "сколько руководство по инженерии для автора с амнезией.\n"
                "Главный тезис такой: следующая сессия приходит к коду чужой")
        self.assertTrue(notes.said_recently("777", said),
                        "её собственные слова, поданные дословно, остались невидимы")

    def test_said_recently_still_says_no_to_something_unsaid(self):
        """Проверка не вакуумна: она не отвечает «да» на что угодно."""
        self._write("777", MULTI)
        self.assertFalse(notes.said_recently("777", "совершенно другой текст про погоду"))

    # ------------------------------------------------------------ группировка
    def test_entries_groups_continuations_with_their_head(self):
        groups = notes._entries((MULTI + "\n" + SINGLE).splitlines())
        self.assertEqual([len(g) for g in groups], [3, 1])

    def test_the_unit_is_named_in_the_constant(self):
        """Имя константы обязано называть свою единицу — прежнее (MAX_LINES) врало."""
        self.assertTrue(hasattr(notes, "MAX_ENTRIES"))
        self.assertFalse(hasattr(notes, "MAX_LINES"),
                         "старое имя осталось и снова начнёт считать не то")


if __name__ == "__main__":
    unittest.main()
