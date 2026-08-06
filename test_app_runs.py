# -*- coding: utf-8 -*-
"""Раздел «Прогоны» мини-аппа: срез снимка не должен выдавать себя за всю историю.

Дефект, который ловят эти тесты (03.08.2026, живой снимок HEAD 4c77776c):

1. Снимок отдаёт аппу ВЫБОРКУ: runs.items — 74 карточки при runs.total = 2120,
   и по построению эта выборка почти равна множеству «требует внимания»
   (active 1 + failed 73). Успешных карточек в ней нет ВООБЩЕ.
   Апп за 134 КБ кода ни разу не читал runs.counts и runs.total — числа
   приезжали в браузер и молча выбрасывались. Экран получался «весь красный»
   не потому, что у Праксис всё плохо, а потому, что зелёное не доехало.

2. Вкладка «Готовые» спрашивала TERMINAL_STATUSES = done+cancelled+failed.
   Раз done-карточек в срезе нет, она показывала ровно 73 УПАВШИХ прогона
   под словом «готовые» — то есть переименовывала поражения в успехи.
   Это худшая из здешних неправд: она не скрывает факт, она его переворачивает.

3. Плитка на главной считала историю длиной среза («74 в истории» при 2120 —
   занижение в 28 раз) и звала ЗАБЛОКИРОВАННЫЙ прогон «в движении».

Тесты структурные: они читают ассеты, потому что логика живёт в браузере и на
проде её нечем исполнить. Каждый проверяет ИМЕННО механизм — таблицу фильтров,
источник числа, наличие сводки, — а не случайное присутствие слова.
"""

import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
APP_JS = (BASE / "praxis_static" / "app.js").read_text(encoding="utf-8")
APP_CSS = (BASE / "praxis_static" / "app.css").read_text(encoding="utf-8")
APP_HTML = (BASE / "praxisapp.html").read_text(encoding="utf-8")


def js_function(name: str) -> str:
    """Тело функции верхнего уровня: от `function name(` до закрывающей скобки в 0 колонке."""
    start = APP_JS.find("function %s(" % name)
    assert start >= 0, "в app.js нет функции %s" % name
    end = APP_JS.find("\n}\n", start)
    assert end > start, "не нашёл конец функции %s" % name
    return APP_JS[start:end + 3]


def js_set(name: str) -> set:
    """Члены `const NAME = new Set([...]);` — читаем то же, что читает браузер."""
    match = re.search(r"const %s = new Set\(\[(.*?)\]\)" % re.escape(name), APP_JS, re.S)
    assert match, "в app.js нет набора %s" % name
    return set(re.findall(r'"([a-z_]+)"', match.group(1)))


def filter_table() -> dict:
    """Таблица фильтров: ключ кнопки -> имя набора статусов (или null для «Все»)."""
    match = re.search(r"const RUN_FILTER_STATUSES = new Map\(\[(.*?)\]\);", APP_JS, re.S)
    assert match, "в app.js нет таблицы RUN_FILTER_STATUSES"
    return {
        key: value
        for key, value in re.findall(r'\["([a-z_]+)",\s*([A-Za-z_]+)\]', match.group(1))
    }


class RunsFilterTruth(unittest.TestCase):
    """Кнопка фильтра и предикат под ней обязаны означать одно и то же."""

    def test_done_filter_beryot_tolko_uspeshnye(self):
        """«Успешные» = done. Пока это был TERMINAL_STATUSES, вкладка звала 73 падения готовыми."""
        table = filter_table()
        self.assertIn("done", table)
        self.assertEqual(js_set(table["done"]), {"done"})

    def test_terminal_statuses_bolshe_ne_predikat_vkladki(self):
        """Прямая строка старого дефекта не должна вернуться копипастой."""
        self.assertNotIn(
            'if (model.runFilter === "done") return TERMINAL_STATUSES.has(status);',
            APP_JS,
        )

    def test_kazhdaya_knopka_izvestna_tablitse(self):
        """Кнопка без записи в таблице тихо показывает ВСЁ под чужой подписью — это и был класс дефекта."""
        buttons = set(re.findall(r'data-run-filter="([a-z_]+)"', APP_HTML))
        self.assertTrue(buttons, "в praxisapp.html не нашлось ни одной кнопки фильтра")
        self.assertEqual(buttons, set(filter_table()))

    def test_predikat_hodit_cherez_tablicu(self):
        """Предикат обязан читать таблицу, иначе она — украшение, а не источник истины."""
        body = js_function("runMatchesFilter")
        self.assertIn("RUN_FILTER_STATUSES.get(model.runFilter)", body)

    def test_knopka_ne_nazyvaetsya_gotovye(self):
        """Слово «готовые» покрывало и отменённые, и упавшие; подпись должна совпасть с предикатом."""
        label = re.search(r'data-run-filter="done">([^<]+)<', APP_HTML)
        self.assertIsNotNone(label)
        self.assertEqual(label.group(1), "Успешные")


class RunsCountsReachTheScreen(unittest.TestCase):
    """runs.counts и runs.total приезжают в снимке — их нельзя выбрасывать."""

    def test_app_chitaet_counts(self):
        """До 03.08.2026 слова counts во всём app.js не было ни разу."""
        self.assertIn("runsMeta(snapshot).counts", js_function("runsCounts"))

    def test_app_chitaet_total(self):
        """Число прожитых прогонов приходит полем total, а не длиной среза."""
        self.assertIn("runsMeta(snapshot).total", js_function("runsTotal"))

    def test_plitka_beryot_istoriyu_iz_total(self):
        """«74 в истории» при total 2120 — занижение работы Праксис в 28 раз."""
        body = js_function("renderNow")
        self.assertIn("runsTotal(snapshot)", body)
        self.assertNotIn("${runsOf(snapshot).length} в истории", body)

    def test_plitka_ne_zovyot_zablokirovannyj_progon_dvizheniem(self):
        """ACTIVE_STATUSES — «не терминальные». Движение — только pending и running."""
        self.assertEqual(js_set("MOVING_STATUSES"), {"pending", "running"})
        self.assertIn("MOVING_STATUSES.has", js_function("renderNow"))


class RunsSliceIsLabelled(unittest.TestCase):
    """Раздел обязан вслух сказать, что это выборка, и чего в ней нет."""

    def test_renderRuns_zovyot_svodku(self):
        body = js_function("renderRuns")
        self.assertIn("renderRunsSummary(snapshot)", body)

    def test_svodka_govorit_skolko_iz_skolkih(self):
        """Без пары «показано / всего» экран остаётся утверждением, что это вся история."""
        body = js_function("renderRunsSummary")
        self.assertIn("${shown} из ${total}", body)

    def test_svodka_perechislyaet_chego_v_sreze_net(self):
        """1990 успешных прогонов существуют, но в срезе их ноль — это надо назвать, а не скрыть."""
        self.assertIn("runsMissingStatuses(snapshot)", js_function("renderRunsSummary"))

    def test_svodka_molchit_kogda_counts_ne_prishli(self):
        """Старый сервер без counts: лучше спрятать блок, чем нарисовать выдуманные нули."""
        body = js_function("renderRunsSummary")
        self.assertIn("box.hidden = true", body)

    def test_pustoe_mesto_schitaet_po_counts(self):
        """«История появится после durable execution» под вкладкой успешных — прямое отрицание 1990 прогонов."""
        body = js_function("runsEmptyState")
        self.assertIn("runsCountOf(snapshot", body)
        self.assertIn("но их нет в срезе", body)

    def test_razmetka_i_stil_svodki_na_meste(self):
        """Сводке нужен контейнер в разметке и своя раскладка, иначе она не отрисуется."""
        self.assertIn('id="runsSummary"', APP_HTML)
        self.assertLess(APP_HTML.index('id="runsSummary"'), APP_HTML.index('id="runsList"'))
        self.assertIn(".runs-summary {", APP_CSS)
        self.assertIn(".runs-summary__metrics {", APP_CSS)


class RunsSummaryIsScrubbedOnRevoke(unittest.TestCase):
    """Отзыв доступа обязан уносить сводку вместе со всем остальным состоянием."""

    def test_svodka_est_v_spiske_uborki(self):
        """scrubProtectedSurface поимённо чистит #runsList и #memoryHealth — все они в НЕактивных
        .view с display:none. Значит смысл рутины именно в снятии остатка ИЗ DOM. Агрегат
        #runsSummary — брат #memoryHealth: без записи в списке после 403 в документе остаются
        числа 2120/1990/73/56/1 и фраза про срез в сессии, у которой прав на них уже нет."""
        self.assertIn('"#runsSummary"', js_function("scrubProtectedSurface"))

    def test_svodka_snova_pryachetsya(self):
        """replaceChildren() снимает содержимое, но не блок: .runs-summary — коробка с рамкой,
        и после первого renderRuns она живёт с hidden=false. Иначе после реавторизации до первого
        снимка на экране висит пустая рамка ни о чём."""
        body = js_function("scrubProtectedSurface")
        self.assertIn('document.querySelector("#runsSummary")?.setAttribute("hidden", "")', body)


if __name__ == "__main__":
    unittest.main()
