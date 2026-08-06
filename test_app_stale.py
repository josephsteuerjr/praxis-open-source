# -*- coding: utf-8 -*-
"""Мини-апп не должен отвечать на вопросы о её состоянии литералами.

⚠ 03.08.2026. Общий механизм всех дефектов ниже одинаков и потому проверяется одинаково:
отрисовка спрашивала снимок по ключу, которого он не отдаёт (snapshot.praxis,
system.services, computer.processes, telegram.social_pulse), промах молча падал в литерал
по умолчанию, а литерал этот всегда бодрый — «на связи», «Контур устойчив», «здоров».
Поэтому тесты пришпиливают ОБЕ стороны договора: какие ключи сервер действительно кладёт
и какие ключи читает экран. Проверять сами слова на экране бесполезно — они как раз и
были придуманы."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import praxis_app

HERE = Path(praxis_app.__file__).resolve().parent
APP_JS = (HERE / "praxis_static" / "app.js").read_text(encoding="utf-8")
APP_HTML = (HERE / "praxisapp.html").read_text(encoding="utf-8")
APP_PY = (HERE / "praxis_app.py").read_text(encoding="utf-8")
PANEL_PY = (HERE / "panel.py").read_text(encoding="utf-8")

# Словарь, которым сервер называет её состояние в snapshot.now (обе ветки snapshot()).
NOW_STATE_WORDS = ("active", "ready", "online", "offline")


def code_only(text: str) -> str:
    """Убрать строки-комментарии.

    ⚠ Половина тестов ниже проверяет ОТСУТСТВИЕ имени ключа в app.js. Если мерить по
    всему файлу, комментарий, объясняющий дефект, сам же и красит тест — и следующему
    читателю останется правка без причины. Меряем исполняемый текст, а комментарии
    обязаны называть фантомные ключи по имени."""
    return chr(10).join(
        line for line in text.splitlines() if not line.strip().startswith("//")
    )


APP_CODE = code_only(APP_JS)


def between(text: str, start: str, end: str) -> str:
    assert start in text, f"якорь пропал: {start}"
    tail = text.split(start, 1)[1]
    assert end in tail, f"якорь пропал: {end}"
    return tail.split(end, 1)[0]


class ScreenDoesNotAskForKeysThatDoNotExist(unittest.TestCase):
    """Каждый из этих ключей отсутствует в снимке, и каждый давал экрану бодрый литерал."""

    def test_snapshot_praxis_is_not_read(self):
        self.assertNotIn("snapshot.praxis", APP_CODE,
                         "шапка снова утверждает «на связи» из несуществующего ключа")

    def test_system_services_is_not_read(self):
        self.assertNotIn("system.services", APP_CODE,
                         "плитка снова делает зелёный вывод из пустого списка")
        self.assertNotIn("serviceCard", APP_CODE,
                         "карточка сервисов не вызывалась ни разу — источника у неё нет")

    def test_social_pulse_is_not_read(self):
        self.assertNotIn("social_pulse", APP_CODE,
                         "карточка снова утверждает здоровье механизма без фактов о нём")
        self.assertNotIn("pulse.status", APP_CODE)

    def test_process_source_is_not_guessed(self):
        self.assertNotIn("computer.operations", APP_CODE,
                         "догадка о втором имени источника процессов вернулась")

    def test_server_really_does_not_send_them(self):
        """Обратная сторона договора: если сервер однажды НАЧНЁТ их присылать, тест
        обязан покраснеть — иначе экран будет молчать о новом источнике."""
        computer = between(APP_PY, "        computer = {", "        payload: dict[str, Any]")
        self.assertNotIn("processes", computer)
        state = between(PANEL_PY, "def server_state()", "# группировка и описания")
        self.assertNotIn('"services"', state)
        telegram = between(APP_PY, "    def _telegram(self)", "    def _trust(self")
        self.assertNotIn("social_pulse", telegram)


class HeaderAndHeroReadNow(unittest.TestCase):
    """Дефект: snapshot.now сервер собирает в каждом снимке, а слова snapshot.now во всём
    app.js не встречалось ни разу. Подпись под её именем читалась «на связи» безусловно."""

    def test_header_reads_now_state(self):
        header = between(APP_CODE, "function renderHeader(snapshot) {", chr(10) + "}")
        self.assertIn("nowState(snapshot)", header)
        self.assertNotIn("praxis.", header)

    def test_hero_reads_now_state(self):
        hero = between(APP_CODE, "function renderNow(snapshot) {", 'setText("#heroRevision"')
        self.assertIn("nowOf(snapshot)", hero)
        self.assertIn("nowState(snapshot)", hero)
        self.assertNotIn("praxis.", hero)

    def test_client_translates_every_word_the_server_can_say(self):
        """Ловушка ровно та же, что с комнатами: непереведённое слово печатается
        латиницей поверх русского экрана. Проверяем не «есть ли ready», а покрытие
        всего словаря сервера."""
        now_block = between(APP_PY, 'payload["now"] = {', 'payload["revision"]')
        table = between(APP_JS, "function statusLabel(value) {", "})[String(")
        for word in NOW_STATE_WORDS:
            self.assertIn(f'"{word}"', now_block,
                          "сервер перестал говорить это слово — сверить словарь клиента")
            self.assertIn(f"{word}:", table,
                          f"состояние {word} напечатается на экране латиницей")


class SystemSurfaceShowsMeasuredNumbers(unittest.TestCase):
    """Дефект: и плитка, и раздел спрашивали system.services. server_state() отдаёт
    loadavg, mem, disk, uptime_sec и возраст логов — ничего из этого видно не было."""

    def test_server_state_keys_are_the_ones_the_screen_reads(self):
        state = between(PANEL_PY, "def server_state()", "# группировка и описания")
        for key in ('"loadavg"', '"cpus"', '"mem"', '"disk"', '"uptime_sec"', '"logs"'):
            self.assertIn(key, state, "сервер перестал отдавать поле, на которое опёрся экран")

    def test_reader_uses_those_keys(self):
        reader = between(APP_CODE, "function systemNumbers(system = {}) {", chr(10) + "}")
        for key in ("system.loadavg", "system.cpus", "system.mem", "system.disk",
                    "system.uptime_sec", "system.logs"):
            self.assertIn(key, reader)

    def test_tile_has_three_outcomes_not_two(self):
        """Отсутствие фактов — не «устойчив» и не «требует внимания», а третье слово."""
        tile = between(APP_CODE, 'setText("#tileSystemTitle"', 'showBadge("#navMoreBadge"')
        self.assertIn("sys.error", tile)
        self.assertIn("sysKnown", tile)
        self.assertNotIn("Контур устойчив", APP_CODE,
                         "вывод о здоровье контура снова сделан без данных")

    def test_section_renders_facts(self):
        block = between(APP_CODE, 'setText("#systemHead"', "#view-more [data-command]")
        self.assertIn("systemNumbers(system)", block)
        self.assertIn("systemFactCard", block)
        self.assertIn(">Сервер<", APP_HTML)
        self.assertNotIn(">Сервисы<", APP_HTML,
                         "заголовок снова обещает то, чего сервер не присылает")


class ComputerShowsTheInventoryItAlreadyHas(unittest.TestCase):
    """Дефект: раздел просил computer.processes (источника нет по построению) и молчал о
    computer.inventory, который приезжает в каждом снимке."""

    def test_inventory_keys_exist_on_the_server(self):
        block = between(APP_PY, "    def _inventory(self", "    def _computer_evidence(self")
        for key in ('"hostname"', '"volumes"', '"tools"', '"apps_count"', '"projects_count"'):
            self.assertIn(key, block)

    def test_inventory_reaches_the_screen(self):
        facts = between(APP_CODE, 'const facts = element("div", "device-facts");',
                        "replace(target, head, facts);")
        self.assertIn("computer.inventory", facts)
        for key in ("inventory.hostname", "inventory.volumes", "inventory.tools",
                    "inventory.apps_count", "inventory.projects_count"):
            self.assertIn(key, facts)

    def test_missing_inventory_is_named_not_faked(self):
        facts = between(APP_CODE, 'const facts = element("div", "device-facts");',
                        "replace(target, head, facts);")
        self.assertIn("inventory.available", facts,
                      "снятая карта машины снова выглядит как живая")

    def test_empty_process_list_says_why(self):
        block = between(APP_CODE, "const processes = asList(computer.processes);",
                        "const artifacts")
        self.assertIn("Снимок не несёт списка процессов", block,
                      "пустой раздел снова утверждает, что процессов нет")


class RoomsShowTheServerWording(unittest.TestCase):
    """Дефект: пилюля падала на сырой room.mode и печатала латиницу, а весь провенанс
    режима (причина, срок, автор, раскрытие визитки) не показывался вовсе."""

    def test_panel_sends_the_wording_and_provenance(self):
        block = between(PANEL_PY, "def rooms_list(", "def room_set(")
        for key in ('"mode_word"', '"reason"', '"until"', '"author"', '"disclosure"'):
            self.assertIn(key, block, "панель перестала отдавать поле, на которое опёрся апп")

    def test_card_uses_mode_word_and_provenance(self):
        card = between(APP_CODE, "function roomCard(room) {", chr(10) + "function membershipCard")
        self.assertIn("room.mode_word", card, "на экране Егора снова латинский идентификатор")
        self.assertIn("room.reason", card)
        self.assertIn("room.until", card)
        self.assertIn("room.author", card)
        self.assertIn("room.disclosure", card)
        self.assertNotIn('first(room.status, room.mode, "online")', card)


class TelegramPulseKeepsSilentInsteadOfClaimingHealth(unittest.TestCase):
    def test_card_is_built_from_arrived_data(self):
        block = between(APP_CODE, "function renderTelegram(snapshot) {", "const transactions")
        self.assertIn("telegramState.pending_followups", block)
        self.assertIn("снимок не сообщает", block)
        self.assertNotIn("healthy", block, "пилюля здоровья вернулась без источника")

    def test_server_still_sends_only_four_keys(self):
        root = Path(tempfile.mkdtemp())
        (root / "memory" / "maps").mkdir(parents=True)
        service = praxis_app.PraxisAppService(root, owner_id=1)
        with mock.patch.object(service.followups, "list", return_value=[]), \
                mock.patch.object(service.membership, "pending", return_value=[]):
            payload = service._telegram()
        self.assertEqual(set(payload), {"rooms", "followups", "pending_followups", "membership"},
                         "состав секции изменился — сверить, что экран о новом не молчит")


if __name__ == "__main__":
    unittest.main(verbosity=2)
