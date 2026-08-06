# -*- coding: utf-8 -*-
"""Чтение её markdown-карт в мини-аппе: где проходит обрыв и чем рисуется карта.

Здесь два независимых механизма, и оба ловятся адресно, а не по совпадению.

1. Сервер. map.read резал файл по БАЙТАМ до декодирования. У её карт кириллица —
   два байта на букву, — поэтому край почти всегда приходился на середину
   последовательности, и errors="replace" дописывал U+FFFD. Обрыв выглядел не как
   обрыв, а как испорченное слово: владелец видел мусор и имел все основания думать,
   что мусор лежит в самой памяти.

2. Клиент. Карта показывалась сырым исходником в общем приёмнике серверных расписок.
   Разбор markdown, который её заменил, безопасен КОНСТРУКЦИЕЙ: текст из файла попадает
   в документ только как textContent или createTextNode. Это свойство надо охранять
   именно как свойство — сегодняшнее отсутствие дыры ничего не гарантирует завтра, а
   единственный способ её завести — переписать рендерер на склейку HTML-строк.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import praxis_app

BASE = Path(praxis_app.__file__).resolve().parent


class TestMapReadCutsOnCharacterBoundary(unittest.TestCase):
    """Обрыв длинной карты не должен портить последнюю букву."""

    def test_tail_is_not_replaced_with_u_fffd(self):
        """Граница потолка внутри двухбайтовой буквы: буква выпадает целиком, не ломается.

        Дефект до 03.08.2026: raw[:limit].decode("utf-8", "replace") на нечётной
        границе давал U+FFFD в самом конце текста.
        """
        raw = ("я" * 100).encode("utf-8")          # ровно 200 байт, по 2 на букву
        self.assertEqual(len(raw), 200)
        # Прежнее поведение — для протокола: срез по байтам действительно портил хвост.
        self.assertTrue(raw[:51].decode("utf-8", "replace").endswith("�"))

        content, truncated = praxis_app._bounded_map_text(raw, limit=51)
        self.assertTrue(truncated)
        self.assertNotIn("�", content)
        self.assertEqual(content, "я" * 25)

    def test_cut_exactly_on_boundary_keeps_the_last_whole_letter(self):
        """Если потолок совпал с границей символа, последняя буква остаётся на месте."""
        raw = ("я" * 100).encode("utf-8")
        content, truncated = praxis_app._bounded_map_text(raw, limit=50)
        self.assertTrue(truncated)
        self.assertEqual(content, "я" * 25)

    def test_short_map_is_whole_and_not_marked_truncated(self):
        """Карта короче потолка отдаётся целиком и без флага обрезки."""
        raw = "# PEOPLE\n\n- [егор](../people/егор.md) Егор\n".encode("utf-8")
        content, truncated = praxis_app._bounded_map_text(raw, limit=4096)
        self.assertFalse(truncated)
        self.assertEqual(content, raw.decode("utf-8"))

    def test_map_read_branch_delegates_to_the_boundary_helper(self):
        """Сама ветка map.read обязана звать помощник, а не резать байты у себя."""
        source = (BASE / "praxis_app.py").read_text(encoding="utf-8")
        self.assertNotIn('content = raw[:limit].decode("utf-8", "replace")', source)
        self.assertIn("content, truncated = _bounded_map_text(raw)", source)
        # Гейт и жёсткий выбор имени карты трогать было нельзя — проверяем, что стоят.
        self.assertIn('if not viewer.may("praxis.snapshot"):', source)
        self.assertIn("if name not in MAP_NAMES:", source)


class TestMemoryMapRendererIsBuiltNotConcatenated(unittest.TestCase):
    """Рендерер карт безопасен конструкцией; переписать его склейкой HTML нельзя."""

    def setUp(self):
        self.js = (BASE / "praxis_static" / "app.js").read_text(encoding="utf-8")
        start = self.js.index("function mdLink(")
        end = self.js.index("function showCommandResult(")
        self.assertLess(start, end, "блок разбора markdown должен стоять до showCommandResult")
        self.block = self.js[start:end]

    def test_no_html_string_assembly_anywhere_in_app_js(self):
        """Ни один узел в аппе не собирается из строки HTML — ни старый, ни новый.

        Проверяем УПОТРЕБЛЕНИЕ, а не упоминание: комментарий рядом с рендерером
        объясняет, почему эти приёмы запрещены, и не должен ронять собственный тест.
        """
        for forbidden in (".innerHTML", ".outerHTML", "insertAdjacentHTML(",
                          "document.write("):
            self.assertNotIn(forbidden, self.js, f"{forbidden} в app.js")

    def test_renderer_puts_file_text_through_text_nodes_only(self):
        """Текст карты попадает в документ только текстовым узлом."""
        self.assertIn("document.createTextNode(", self.block)
        for forbidden in ("eval(", "new Function", "Range.createContextualFragment",
                          "DOMParser", "srcdoc"):
            self.assertNotIn(forbidden, self.block, f"{forbidden} в рендерере карты")

    def test_only_http_targets_ever_become_real_links(self):
        """href ставится ровно в одной ветке — под проверкой схемы http(s).

        Цели внутри карт относительные («../people/егор.md»); такой файл аппу не отдаёт
        ни один маршрут, поэтому живой ссылкой он быть не должен, а чужая схема
        (javascript:, data:) не должна попасть в href вовсе.
        """
        self.assertIn("const MD_HTTP = /^https?:\\/\\//i;", self.js)
        self.assertIn("if (MD_HTTP.test(target)) {", self.block)
        self.assertEqual(self.block.count("link.href = target;"), 1)
        self.assertEqual(self.block.count(".href ="), 1)
        self.assertIn('link.rel = "noopener noreferrer";', self.block)

    def test_map_click_opens_its_own_sheet_not_a_server_receipt(self):
        """Нажатие на карту ведёт в лист карты, а не в общий приёмник расписок."""
        self.assertIn("openMemoryMap(map.dataset.memoryMap, map)", self.js)
        self.assertNotIn("showCommandResult(map.dataset.memoryMap", self.js)
        self.assertIn('eyebrow: "Карта памяти"', self.js)

    def test_plain_text_is_emitted_before_any_token_decision(self):
        """Текст между находками отдаётся ДО разбора самой находки — иначе он теряется.

        Поймано живым прогоном рендерера 03.08.2026. Если «_case_» в snake_case_name
        признаётся не курсивом, а обычным текстом, а слайс «вложенная строка про snake»
        отдаётся только в ветках-разметке, то начало строки её карты пропадает молча —
        то есть апп показывает карту короче, чем она есть, и не говорит об этом.
        """
        emit = self.block.index("if (match.index > last) parent.append(document.createTextNode(")
        decide = self.block.index("MD_WORD.test(before)")
        self.assertLess(emit, decide, "простой текст обязан уходить в узел раньше решения о токене")

    def test_truncation_is_declared_to_the_owner(self):
        """result.truncated обязан превращаться в видимую строку, а не молчать."""
        self.assertIn("if (result.truncated) {", self.block)
        self.assertIn("md-notice", self.block)


class TestMemoryMapCardTellsTheTruth(unittest.TestCase):
    """Карточка карты и сетка карт не должны обещать несуществующее."""

    def setUp(self):
        self.js = (BASE / "praxis_static" / "app.js").read_text(encoding="utf-8")
        self.css = (BASE / "praxis_static" / "app.css").read_text(encoding="utf-8")

    def test_dead_count_field_is_gone_from_code_and_styles(self):
        """Поля count в снимке нет: praxis_app отдаёт available/updated_at/bytes."""
        self.assertNotIn("map.count", self.js)
        self.assertNotIn(".map-card .map-count", self.css)
        self.assertIn("const available = map.available !== false;", self.js)
        self.assertIn("card.disabled = !available;", self.js)

    def test_map_grid_is_gated_by_the_same_scope_as_search(self):
        """Гейт у чтения карты и у поиска один — praxis.snapshot; сетка не зовёт в 403."""
        self.assertIn('const mayReadMemory = hasScope("praxis.snapshot", snapshot);', self.js)
        self.assertIn('document.querySelector("#memorySearch").hidden = !mayReadMemory;', self.js)
        self.assertIn("replace(target, ...(mayReadMemory", self.js)

    def test_reading_a_map_does_not_order_a_new_snapshot(self):
        """Чтение не двигает снимок: иначе каждая карта стоит пересборки всего экрана."""
        self.assertIn("const READ_ONLY_COMMANDS = new Set([", self.js)
        self.assertIn('"memory.map.read", "memory.search",', self.js)
        self.assertIn(
            "const readOnly = READ_ONLY_COMMANDS.has(`${String(domain)}.${String(action)}`);",
            self.js,
        )
        self.assertIn("} else if (!readOnly) {", self.js)
        self.assertIn('if (!readOnly) telegram.haptic("medium");', self.js)


class TestMarkdownDocScrollsWithTheSheet(unittest.TestCase):
    """У документа карты не должно быть собственной высоты — иначе скроллер в скроллере."""

    def test_md_doc_has_no_own_max_height_while_recap_keeps_its_own(self):
        css = (BASE / "praxis_static" / "app.css").read_text(encoding="utf-8")
        start = css.index(".md-doc {")
        block = css[start:css.index("}", start)]
        self.assertNotIn("max-height", block)
        self.assertNotIn("overflow-y", block)
        # .recap-copy — приёмник логов и вывода shell; его правила менять было не нужно.
        recap = css[css.index(".recap-copy {"):]
        self.assertIn("max-height: min(48vh, 520px);", recap[:recap.index("}")])


if __name__ == "__main__":
    unittest.main()
