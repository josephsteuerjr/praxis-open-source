"""
Тесты «Прожитого хода»: turns.py (кольцо, урезка на записи, тиры приватности)
и интеграция — voice_turn/guard пишут исход хода, outbound data advisor получает свежие
ходы scope-честно, STATE и [missed]-фрейм отвечают фактом, тул recent_turns, сон сводит
день кодом. Изоляция .state/turns.jsonl per-test — та же готча, что _dialog_name_cache.

Запуск:  python praxis_test.py test_turns -v
"""

from __future__ import annotations

import json
import os
import time
from unittest import mock

import rooms
import turns
import unanswered
from test_layer7 import ScriptedClient
from test_perceive import Base  # noqa: F401  (герметичный харнесс + фейки anthropic/dotenv)

import agent  # noqa: E402
import computer_memory  # noqa: E402
import llm  # noqa: E402
import notes  # noqa: E402


class TurnsBase(Base):
    def setUp(self):
        super().setUp()
        st = self.tmp / "memory" / ".state"
        patch = {
            turns: dict(BASE=self.tmp, STATE_DIR=st, PATH=st / "turns.jsonl"),
            rooms: dict(BASE=self.tmp, MEM_DIR=self.tmp / "memory",
                        ROOMS_DIR=self.tmp / "memory" / "rooms",
                        CARDS_PATH=st / "room_cards.json"),
            unanswered: dict(BASE=self.tmp, STATE_DIR=st, PATH=st / "unanswered.json"),
        }
        for module, attrs in patch.items():
            for k, val in attrs.items():
                self._orig.append((module, k, getattr(module, k)))
                setattr(module, k, val)
        turns._reset()
        self.addCleanup(turns._reset)  # кольцо в RAM — процесс-глобал, чистим и после

    def _mk(self, kind="chat", chat_id="777", scope="owner", who="Егор",
            out="ответ", ts=None, **fields):
        t = turns.begin(kind=kind, chat_id=chat_id, scope=scope, who=who,
                        gist_in=fields.pop("gist_in", f"{who}: привет"))
        t["out"] = out
        if ts is not None:
            t["ts"] = ts
        t.update(fields)
        turns.record(t)
        return t


# --------------------------------------------------------------------------- #
#  Ядро: кольцо, урезка, тиры, строки
# --------------------------------------------------------------------------- #

class TestTurnsCore(TurnsBase):
    def test_roundtrip_and_gist_is_last_line(self):
        self._mk(gist_in="Praxis: раньше\nЕгор: глянь логи\n", out="смотрю")
        rows = turns.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["in"], "Егор: глянь логи")
        self.assertEqual(rows[0]["out"], "смотрю")
        self.assertTrue(turns.PATH.exists(), "запись должна дойти до jsonl")

    def test_clip_on_write_not_on_read(self):
        t = turns.begin(gist_in="Егор: " + "х" * 900)
        t["out"] = "о" * 900
        t["why"] = "п" * 900
        t["tools"] = [f"tool_{i}(" + "a" * 300 + ")→рез" for i in range(20)]
        turns.record(t)
        raw = json.loads(turns.PATH.read_text(encoding="utf-8").splitlines()[0])
        self.assertLessEqual(len(raw["in"]), turns.GIST_CHARS)
        self.assertEqual(raw["out"], "о" * 900,
                         "out хранится целиком (клип теперь только на показе)")
        self.assertLessEqual(len(raw["why"]), turns.WHY_CHARS)
        self.assertLessEqual(len(raw["tools"]), turns.TOOLS_MAX + 1)
        for line in raw["tools"]:
            self.assertLessEqual(len(line), turns.TOOL_LINE_CHARS)
        body = sum(len(l) for l in raw["tools"])
        self.assertLessEqual(body, turns.TOOLS_TOTAL_CHARS + turns.TOOL_LINE_CHARS,
                             "суммарный потолок тулов на записи")

    def test_out_stored_full_but_clipped_with_marker_on_display(self):
        """Промежуточный пасс, пункт E: молчаливая обрезка out на записи теряла хвост
        (внешний читатель принимал клип за конец ответа). Хранение — полное, клип —
        только на показе и с явным маркером."""
        long_out = "яркое начало " + "с" * 700 + " важный хвост"
        self._mk(out=long_out)
        raw = json.loads(turns.PATH.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(raw["out"], long_out)
        line = turns.format_line(raw)
        self.assertIn("обрезано для показа", line)
        self.assertNotIn("важный хвост", line, "показ остаётся коротким")
        # мульти-чанковый ответ (>4096, Telegram шинкует) хранится целиком
        multi = "чанк один " * 500  # ~5000 символов
        self._mk(out=multi)
        raw_multi = json.loads(turns.PATH.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(raw_multi["out"], multi, "мульти-чанк не режется на 4000")
        # страховочный потолок всё же есть — но обрезка НЕ молчаливая (маркер)
        t = turns.begin(gist_in="Егор: тест")
        t["out"] = "б" * (turns.OUT_CHARS + 500)
        turns.record(t)
        raw2 = json.loads(turns.PATH.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(len(raw2["out"]), turns.OUT_CHARS)
        self.assertTrue(raw2["out"].endswith(turns._OUT_TRUNC_MARK),
                        "клип на записи не молчит — дописан маркер обрезки")

    def test_rolled_off_turns_are_archived_not_deleted(self):
        """Кольцо держит KEEP ради стоимости промпта — но старое уезжало в мусор.
        Замер 24.07: файл покрывал 84 часа, дальше её решения исчезали отовсюду
        (в поиск `.state/turns.jsonl` не попадал ни по одному правилу индексации)."""

        self._orig.append((turns, "KEEP", turns.KEEP))
        self._orig.append((turns, "ARCHIVE_DIR", turns.ARCHIVE_DIR))
        turns.KEEP = 3
        turns.ARCHIVE_DIR = self.tmp / "memory" / "self"
        turns._reset()
        for i in range(9):
            self._mk(out=f"вердикт {i}")

        files = list(turns.ARCHIVE_DIR.glob("turns-*.jsonl"))
        self.assertTrue(files, "выпавшее из кольца обязано лечь в архив")
        rows = [json.loads(l) for f in files
                for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        archived = " ".join(r["text"] for r in rows)
        self.assertIn("вердикт 0", archived, "самый старый ход не потерян")
        self.assertNotIn("вердикт 8", archived,
                         "свежие ходы живут в кольце и не дублируются в архив")
        self.assertIn("вердикт 8", turns.PATH.read_text(encoding="utf-8"))
        row = rows[0]
        self.assertEqual(row["kind"], "lived_turn")
        self.assertEqual(row["schema"], "praxis.self.lived-turn.v1")
        self.assertTrue(row["event_id"].startswith("livedturn-"))
        self.assertIn("summary", row["meta"])

    def test_other_rooms_are_kept_but_stay_out_of_the_indexed_path(self):
        """Ходы комнат СОХРАНЯЮТСЯ, но не попадают на индексируемый путь.

        ⚠ Тест назывался `..._never_enter_the_archive` и собирал улики нерекурсивным
        `glob("turns-*.jsonl")`. После разделения на `self/` и `self/rooms/<комната>/`
        он физически перестал видеть половину архива и стал зелёным ни о чём —
        ровно тот класс ложного покрытия, который мы весь пасс и вычищаем.

        Проверяемая граница осталась прежней: recall намеренно слеп к аудитории, и
        пустить комнату в общий индекс значило бы унести тему не в ту аудиторию. Но
        удалять её прожитое ради этого не нужно — достаточно не класть его на путь,
        который берёт правило индексации `^self/[^/]+\\.jsonl$`."""

        self._orig.append((turns, "KEEP", turns.KEEP))
        self._orig.append((turns, "ARCHIVE_DIR", turns.ARCHIVE_DIR))
        turns.KEEP = 2
        turns.ARCHIVE_DIR = self.tmp / "memory" / "self"
        turns._reset()

        self._mk(chat_id="-1001999", scope="group", who="Вася",
                 gist_in="Вася: у меня развод и долг 400 тысяч",
                 out="ответила Васе про его долг")
        self._mk(chat_id="555000100", scope="owner", who="Егор",
                 out="запушила baseline 305fb99")
        for i in range(6):
            self._mk(chat_id="-1002222", scope="group", who="кто-то", out=f"шум {i}")

        import re as _re
        import memory_fts

        indexed = " ".join(f.read_text(encoding="utf-8")
                           for f in turns.ARCHIVE_DIR.glob("turns-*.jsonl"))
        everything = " ".join(f.read_text(encoding="utf-8")
                              for f in turns.ARCHIVE_DIR.rglob("turns-*.jsonl"))

        # 1. На индексируемом пути — только её собственная рабочая запись.
        self.assertNotIn("развод", indexed, "чужая комната не попадает в общий индекс")
        self.assertNotIn("400 тысяч", indexed)
        self.assertNotIn("шум", indexed)
        self.assertIn("305fb99", indexed, "её собственная рабочая запись — остаётся")

        # 2. Но прожитое комнаты НЕ уничтожено, и несёт метку происхождения.
        self.assertIn("ответила Васе про его долг", everything,
                      "её ход в комнате — часть её жизни, а не мусор")
        rooms_rows = [json.loads(l)
                      for f in (turns.ARCHIVE_DIR / "rooms").rglob("turns-*.jsonl")
                      for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertTrue(rooms_rows)
        # ⚠ Сверка шла по `rooms_rows[0]`, а порядок здесь задаёт `rglob` — то есть
        # файловая система. Комнат в этом стенде две, и на другом хосте первой оказывалась
        # другая: тест краснел, ничего не поймав. Предмет теста — что КАЖДЫЙ ход комнаты
        # несёт метку происхождения, а нужная комната среди них есть; порядок к этому
        # отношения не имеет и не должен решать вердикт.
        origins = [row["meta"]["origin"] for row in rooms_rows]
        for origin in origins:
            self.assertEqual(origin["scope"], "group")
            self.assertTrue(origin.get("chat_id"), "ход комнаты без метки происхождения")
        self.assertIn("-1001999", [origin["chat_id"] for origin in origins])

        # 3. Граница держится ПРАВИЛОМ, а не тем, что тест смотрит не туда. Ход комнаты
        #    индексируется отдельным видом `room_turn` — то есть находится явным
        #    recall, — но в промпт молча не входит.
        import memory_provenance
        rule = _re.compile(r"^self/[^/]+\.jsonl$")
        for f in (turns.ARCHIVE_DIR / "rooms").rglob("turns-*.jsonl"):
            rel = f.relative_to(turns.ARCHIVE_DIR.parent).as_posix()
            self.assertIsNone(rule.match(rel),
                              f"{rel} не должен путаться с её личной записью")
            selected = memory_fts._selected_jsonl(f, turns.ARCHIVE_DIR.parent)
            self.assertIsNotNone(selected, f"{rel} обязан быть находимым")
            self.assertEqual(selected[0], "room_turn")
            self.assertFalse(
                memory_provenance.automatic_recall_allowed(
                    source_type="room_turn", path=f"memory/{rel}", text="x",
                    memory_dir=turns.ARCHIVE_DIR.parent),
                f"{rel} влезает в промпт молча — это утечка комнаты в комнату")

    def test_archive_does_not_store_a_second_full_copy_of_the_answer(self):
        """`out` живёт до 24000 символов; без потолка архив нёс вторую полную копию,
        а индекс затем третью и четвёртую (замер: 24.5 МБ архива + 55 МБ индекса/мес)."""

        self._orig.append((turns, "KEEP", turns.KEEP))
        self._orig.append((turns, "ARCHIVE_DIR", turns.ARCHIVE_DIR))
        turns.KEEP = 2
        turns.ARCHIVE_DIR = self.tmp / "memory" / "self"
        turns._reset()
        self._mk(out="начало " + "я" * 9000)
        for i in range(6):
            self._mk(out=f"дальше {i}")

        rows = [json.loads(l) for f in turns.ARCHIVE_DIR.glob("turns-*.jsonl")
                for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        big = [r for r in rows if "начало" in r["meta"]["summary"]]
        self.assertTrue(big)
        self.assertLessEqual(len(big[0]["meta"]["summary"]), turns.ARCHIVE_SUMMARY_CHARS)

    def test_archive_lands_where_recall_already_looks(self):
        """`memory/self/*.jsonl` индексатор берёт как self_event — новых правил не нужно."""

        import memory_fts
        self.assertEqual(
            memory_fts._selected_jsonl(
                self.tmp / "memory" / "self" / "turns-2026-07.jsonl",
                self.tmp / "memory"),
            ("self_event", "owner"))

    def test_her_verdict_is_actually_searchable_end_to_end(self):
        """Смысл архива — найти своё решение спустя время. Проверяем поиском, а не
        существованием файла: гитхаб-случай был именно про это."""

        import memory_fts
        self._orig.append((turns, "KEEP", turns.KEEP))
        self._orig.append((turns, "ARCHIVE_DIR", turns.ARCHIVE_DIR))
        turns.KEEP = 2
        turns.ARCHIVE_DIR = self.tmp / "memory" / "self"
        turns._reset()
        self._mk(out="репозиторий praxis-paper-trading опубликован, baseline 305fb99")
        for i in range(6):
            self._mk(out=f"дальше {i}")

        hits = memory_fts.search("praxis-paper-trading", base=self.tmp,
                                 memory_dir=self.tmp / "memory", limit=10)
        found = " ".join(str(h.get("text") or "") for h in hits)
        self.assertIn("305fb99", found,
                      "своё решение обязано находиться спустя время, а не пересказываться")

    def test_archive_is_findable_but_not_ambient(self):
        """Триста ходов в сутки не имеют права топить её фоновый контекст: искать —
        да, всплывать само — нет."""

        import memory_provenance
        self.assertFalse(memory_provenance.self_event_automatic_recall_allowed(
            {"kind": "lived_turn"}))
        self.assertTrue(memory_provenance.self_event_automatic_recall_allowed(
            {"kind": "observation"}))

    def test_archive_failure_never_drops_a_turn(self):
        self._orig.append((turns, "ARCHIVE_DIR", turns.ARCHIVE_DIR))
        self._orig.append((turns, "KEEP", turns.KEEP))
        turns.KEEP = 2
        turns.ARCHIVE_DIR = self.tmp / "файл-вместо-папки"
        turns.ARCHIVE_DIR.write_text("я не папка", encoding="utf-8")
        turns._reset()
        for i in range(6):
            self._mk(out=f"ход {i}")
        self.assertEqual(len(turns.recent(n=10)), 2, "ход записан несмотря на сбой архива")

    def test_ring_in_ram_and_file_compaction(self):
        self._orig.append((turns, "KEEP", turns.KEEP))
        turns.KEEP = 5
        turns._reset()
        for i in range(turns.KEEP * 2 + 2):
            self._mk(out=f"ответ {i}")
        rows = turns.recent(n=100)
        self.assertEqual(len(rows), 5, "RAM-кольцо держит KEEP последних")
        self.assertEqual(rows[-1]["out"], "ответ 11")
        n_lines = len(turns.PATH.read_text(encoding="utf-8").splitlines())
        self.assertLess(n_lines, 12, "компакт должен был сработать (иначе рос бы вечно)")
        self.assertLessEqual(n_lines, turns.KEEP * 2, "инвариант: файл не больше 2×KEEP")

    def test_persists_across_process_restart(self):
        self._mk(out="до рестарта")
        turns._reset()  # «новый процесс»: RAM пуст, файл жив
        rows = turns.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["out"], "до рестарта")

    def test_corrupt_lines_skipped(self):
        turns.STATE_DIR.mkdir(parents=True, exist_ok=True)
        good = json.dumps({"ts": time.time(), "kind": "chat", "chat_id": "1",
                           "scope": "owner", "out": "жив"}, ensure_ascii=False)
        turns.PATH.write_text("{битый json\n" + good + "\n", encoding="utf-8")
        turns._reset()
        rows = turns.recent()
        self.assertEqual([r["out"] for r in rows], ["жив"])

    def test_scope_tiers(self):
        self._mk(chat_id="777", scope="owner", out="owner-ход")
        self._mk(chat_id="-100", scope="group", who="вася", out="групповой")
        self._mk(kind="heartbeat", chat_id=None, scope="owner", out="тихий час")
        self.assertEqual(len(turns.recent(scope="owner")), 3, "owner видит всё")
        grp = turns.recent(scope="group", chat_id="-100")
        self.assertEqual([r["out"] for r in grp], ["групповой"],
                         "чужой канал видит только свои ходы")
        self.assertEqual(turns.recent(scope="group", chat_id="-999"), [])
        self.assertEqual(turns.recent(scope="family", chat_id=None), [],
                         "ходы её окон (chat_id=None) вне owner не видны")

    def test_state_line_marks_pre_restart(self):
        self._mk(out="последнее слово", ts=time.time() - 60)
        line = turns.state_line(boot_ts=time.time())
        self.assertIn("до рестарта", line)
        self.assertIn("последнее слово", line)
        line2 = turns.state_line(boot_ts=time.time() - 3600)
        self.assertNotIn("до рестарта", line2, "ход из этой жизни процесса — без маркера")

    def test_chat_catchup_line(self):
        boot = time.time()
        self._mk(chat_id="777", out="перед сном", ts=boot - 120)
        self._mk(chat_id="777", out="после подъёма", ts=boot + 120)
        line = turns.chat_catchup_line("777", boot_ts=boot)
        self.assertIn("перед сном", line)
        self.assertNotIn("после подъёма", line)
        self.assertEqual(turns.chat_catchup_line("888", boot_ts=boot), "")

    def test_day_line_counts_facts(self):
        # «Сказала» в суточной сводке теперь тоже считается по ФАКТУ доставки, а не по
        # наличию текста: иначе построчно было бы «не сказала», а в ночном дневнике —
        # который и уезжает в долгую память — «сказала». Два голоса об одном дне.
        self._mk(out="раз", tools=["recall(x)→нашла"], delivery="accepted")
        self._mk(out="два", delivery="accepted")
        self._mk(out="", held="voice", why="нечего добавить")
        self._mk(out="", held="evaluator", verdict="молчи", why="утечка")
        self._mk(out="правленый", verdict="правь", rewrote=True, delivery="accepted")
        self._mk(kind="task_window", chat_id=None, out="закрыла нить")
        line = turns.day_line()
        self.assertIn("ходов 6", line)
        # 26.07: было «своих окон». Неточно и раньше (сюда падали heartbeat и forge_event),
        # а с появлением вида wake — прямая неправда: пробуждение не окно, связь в нём цела.
        self.assertIn("своих, без собеседника 1", line)
        # Ход без адресата больше не идёт в «сказала»: суточная сводка уходит в
        # дневник, и раньше она ежедневно завышала сказанное её внутренним текстом.
        self.assertIn("сказала наружу 3", line)
        self.assertIn("записала себе 1", line)
        self.assertIn("промолчала сама 1", line)
        self.assertIn("удержано по data-authority 1", line)
        self.assertIn("legacy-правок 1", line)
        self.assertIn("тул-вызовов 1", line)
        self._mk(out="вчерашний", ts=time.time() - 30 * 3600)
        self.assertIn("ходов 6", turns.day_line(), "старше суток — не в счёте")

    def test_day_line_reports_undelivered_and_keeps_legacy_rows_as_said(self):
        """Недоставленное названо, а не стёрто; строки без поля читаются по-старому."""
        self._mk(out="ушло", delivery="accepted")
        self._mk(out="перебила", delivery="superseded")
        self._mk(out="упало", delivery="failed")
        # Настоящая легаси-строка — та, что УЖЕ лежит на диске с прошлых месяцев.
        # Через record() её не подделать: _clip достроит поле по умолчанию. Пишем сырую
        # строку в файл и поднимаем кольцо заново — иначе тест проверял бы не то.
        with turns.PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.time(), "kind": "chat", "chat_id": "777", "scope": "owner",
                "who": "Егор", "in": "Егор: ?", "out": "старая строка без поля",
                "held": "", "tools": [],
            }, ensure_ascii=False) + "\n")
        turns._reset()
        line = turns.day_line()
        self.assertIn("сказала наружу 2", line, "легаси-строка считается сказанной")
        self.assertIn("не дошло 2", line, "исход назван, а не спрятан")

    def test_describe_is_scope_honest(self):
        self.assertIn("ни одного", turns.describe())
        self._mk(chat_id="777", scope="owner", out="секретик")
        self._mk(chat_id="-100", scope="group", who="вася", out="публичное")
        owner_view = turns.describe(scope="owner")
        self.assertIn("секретик", owner_view)
        self.assertIn("публичное", owner_view)
        grp_view = turns.describe(scope="group", chat_id="-100")
        self.assertIn("публичное", grp_view)
        self.assertNotIn("секретик", grp_view)
        self.assertIn("только этот канал", grp_view)

    def test_format_line_outcomes(self):
        cases = {
            "voice": "промолчала сама",
            "evaluator": "оценщик придержал",
            "anti-repeat": "анти-повтор",
            "drift": "заморожена дрейфом",
            "error": "ход упал",
            "empty": "не родился",
        }
        for held, marker in cases.items():
            t = turns.begin(gist_in="Егор: ?")
            t["held"] = held
            self.assertIn(marker, turns.format_line(t), held)
        # Глагол теперь берётся из ФАКТА доставки, а не из наличия черновика:
        # без расписки ход честно читается как написанный, а не произнесённый.
        t = turns.begin(gist_in="Егор: ?")
        t.update(out="ответ", rewrote=True, why="лесть")
        line = turns.format_line(t)
        self.assertIn("написала (доставка не подтверждена)", line)
        self.assertIn("правлено оценщиком: лесть", line)

        t["delivery"] = "accepted"
        self.assertIn("сказала", turns.format_line(t))
        for outcome, marker in (("superseded", "перебила себя"),
                                ("failed", "доставка не удалась"),
                                ("partial", "сказала частично"),
                                ("blocked", "застряла")):
            t["delivery"] = outcome
            self.assertIn(marker, turns.format_line(t), outcome)

        # Легаси-строки (поля нет вовсе) читаются по-старому: прошлое не переписываем.
        legacy = {"ts": t["ts"], "kind": "chat", "chat_id": "777", "scope": "owner",
                  "in": "Егор: ?", "out": "ответ"}
        self.assertIn("сказала", turns.format_line(legacy))

    def test_turn_without_an_addressee_is_not_recorded_as_spoken(self):
        """Её журнал не имеет права утверждать, что она сказала то, чего никто не слышал.

        forge_event, своё окно и тихий час идут с chat_id=None — канала доставки нет
        по построению, а их текст падал в ветку «сказала». За трое суток так осело
        8 из 19 её реакций на завершения субагентов.
        """

        for kind in ("forge_event", "task_window", "heartbeat"):
            t = turns.begin(kind=kind, gist_in="субагент закончил")
            t["out"] = "Приняла работу как свою"
            line = turns.format_line(t)
            self.assertIn("записала себе", line, kind)
            self.assertIn("Приняла работу как свою", line, kind)
            self.assertNotIn("сказала", line, kind)

    def test_old_rows_are_healed_on_read_not_left_in_the_old_lie(self):
        """Вывод идёт по чтению, поэтому месяцы уже записанных строк перестают врать
        сразу — без правки прошлого и без кольца, говорящего двумя голосами."""

        legacy = {"ts": time.time(), "kind": "forge_event", "chat_id": None,
                  "scope": "owner", "out": "старая запись себе", "held": "",
                  "tools": [], "in": "", "who": "", "title": ""}
        self.assertIn("записала себе", turns.format_line(legacy))

    def test_it_does_not_claim_silence_when_a_tool_actually_sent(self):
        """Обратная ложь: её forge-фрейм прямо предлагает narrate, и 11 из 19 таких
        ходов реально что-то отправляли. Объявлять весь ход немым нельзя."""

        t = turns.begin(kind="forge_event", gist_in="субагент закончил")
        t["out"] = "Приняла работу как свою"
        t["tools"] = ["narrate(task_id=hcode-1, text=…) → Отправила → AbstractDL Chat"]
        line = turns.format_line(t)
        self.assertIn("что ушло — см. «делала»", line)
        self.assertNotIn("отправок в следе нет", line)

    def test_a_wake_without_text_is_not_counted_as_a_note_to_self(self):
        """Большинство таких пробуждений текста не рождает. Считать их «записала
        себе» — та же ложь, что и «сказала», только в новом счётчике; а строка
        уходит в дневник каждую ночь."""

        for _ in range(6):
            self._mk(kind="forge_event", chat_id=None, out="")
        self._mk(kind="forge_event", chat_id=None, out="одна настоящая запись")
        line = turns.day_line()
        self.assertIn("записала себе 1", line)
        self.assertIn("сказала наружу 0", line)

    def test_no_contour_claims_a_connection_it_did_not_check(self):
        """Связь ЧИТАЕТСЯ ИЗ ЗАПИСИ, снятой в момент хода, а не утверждается по виду.

        Сначала врало окно («Telegram жив» при намеренно отключённом Telethon), потом —
        forge-контур: там связь обычно жива, но если reconnect после окна упал, его ходы
        идут в мире без связи, а журнал уверенно писал обратное. Праксис назвала это сама.
        Вид хода не знает состояния линии; знает только тот, кто её спросил.
        """
        window = turns.begin(kind="task_window", gist_in="своё окно")
        window["telegram"] = "closed_for_window"
        line = turns.format_line(window)
        self.assertIn("Telegram закрыт на время окна", line)
        self.assertNotIn("Telegram жив", line)

        forge = turns.begin(kind="forge_event", gist_in="событие")
        forge["telegram"] = "connected"
        self.assertIn("Telegram жив", turns.format_line(forge))
        forge["telegram"] = "disconnected"
        self.assertIn("связи не было", turns.format_line(forge))

        # Не спрашивали — не утверждаем ни в одну сторону (легаси-записи и подделки).
        self.assertNotIn("Telegram жив",
                         turns.format_line(turns.begin(kind="forge_event", gist_in="событие")))


# --------------------------------------------------------------------------- #
#  Интеграция: voice_turn/guard пишут исход хода
# --------------------------------------------------------------------------- #

class TestLiveRecording(TurnsBase):
    def test_spoken_turn_recorded(self):
        self._client("ответ по делу")
        out = agent.voice_turn("777", "Егор: привет", speaker="Егор", is_owner=True)
        self.assertEqual(out, "ответ по делу")
        rows = turns.recent()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["kind"], r["scope"], r["out"], r["held"]),
                         ("chat", "owner", "ответ по делу", ""))
        self.assertEqual(r["in"], "Егор: привет")

    def test_own_silence_recorded(self):
        self._client("[молчу]")
        out = agent.voice_turn("-100", "вася: ну?", speaker="вася", is_dm=False)
        self.assertEqual(out, "")
        r = turns.recent()[-1]
        self.assertEqual(r["held"], "voice")
        self.assertEqual(r["why"], "")

    def test_data_authority_hold_recorded(self):
        self._orig.append((agent, "evaluate_reply", agent.evaluate_reply))
        agent.evaluate_reply = lambda text, context="", **kw: ("deny", "чужой секрет")
        self._client("вот тебе секрет Маши")
        out = agent.voice_turn("888", "вася: расскажи", speaker="вася", is_owner=False)
        self.assertEqual(out, "")
        r = turns.recent()[-1]
        self.assertEqual((r["held"], r["verdict"]), ("privacy", "deny"))
        self.assertEqual((r["advisor"], r["praxis_decision"]),
                         ("privacy", "hold_for_data_authority"))
        self.assertEqual(r["advisor_verdict"], "deny")
        self.assertIn("секрет", r["why"])

    def test_data_authority_ok_records_advice_and_praxis_decision(self):
        self._orig.append((agent, "evaluate_reply", agent.evaluate_reply))
        agent.evaluate_reply = lambda text, context="", **kw: ("ok", "")
        self._client("ответ по делу")
        out = agent.voice_turn("888", "вася: привет", speaker="вася", is_owner=False)
        self.assertEqual(out, "ответ по делу")
        r = turns.recent()[-1]
        self.assertEqual((r["advisor"], r["advisor_verdict"], r["praxis_decision"]),
                         ("privacy", "ok", "send_authored"))

    def test_owner_dm_records_exact_unmediated_output(self):
        self._orig.append((agent, "evaluate_reply", agent.evaluate_reply))
        agent.evaluate_reply = lambda text, context="", **kw: ("молчи", "перебор с лестью")
        self._client("Мне повезло с архитектором. Тесты зелёные.")
        out = agent.voice_turn("777", "Егор: как тесты?", speaker="Егор", is_owner=True)
        self.assertEqual(out, "Мне повезло с архитектором. Тесты зелёные.")
        r = turns.recent()[-1]
        self.assertFalse(r["rewrote"])
        self.assertEqual(r["verdict"], "")
        self.assertEqual((r["advisor"], r["praxis_decision"]),
                         ("not_run_owner_audience", "send_authored"))
        self.assertEqual(r["out"], out)

    def test_authored_repeat_is_recorded_as_delivery(self):
        self._orig.append((agent, "evaluate_reply", agent.evaluate_reply))
        agent.evaluate_reply = lambda text, context="", **kw: ("ok", "")
        self._client("повторяюсь")
        agent.voice_turn("-100", "вася: ну?", speaker="вася", is_dm=False)
        self._client("повторяюсь")
        out = agent.voice_turn("-100", "вася: и?", speaker="вася", is_dm=False)
        self.assertEqual(out, "повторяюсь")
        self.assertEqual(turns.recent()[-1]["held"], "")

    def test_voice_error_recorded(self):
        self._orig.append((agent, "_voice", agent._voice))
        def boom(*a, **kw):
            raise RuntimeError("сломалась")
        agent._voice = boom
        self._client("не дойдёт")
        out = agent.voice_turn("777", "Егор: ау", speaker="Егор", is_owner=True)
        self.assertEqual(out, "")
        self.assertEqual(turns.recent()[-1]["held"], "error")

    def test_heartbeat_and_window_are_turns_too(self):
        self._client("написала Егору о находке")
        agent.heartbeat_turn("хвост дневника")
        self._client("закрыла нить про отпуск")
        agent.task_window("разобрать нить")
        kinds = [r["kind"] for r in turns.recent()]
        self.assertEqual(kinds, ["heartbeat", "task_window"])
        for r in turns.recent():
            self.assertEqual(r["scope"], "owner")
            self.assertIsNone(r["chat_id"])

    def test_direct_guard_without_turn_records_nothing(self):
        out = agent.guard_outbound_reply(
            "просто текст", ctx=agent.ChannelContext(chat_id="9", owner=True),
        )
        self.assertEqual(out, "просто текст")
        self.assertEqual(turns.recent(), [], "turn=None — guard ничего не пишет (absence-путь)")


# --------------------------------------------------------------------------- #
#  Мета-слои видят прожитое: data authority, STATE, [missed], тул, сон
# --------------------------------------------------------------------------- #

class TestConsumers(TurnsBase):
    def test_group_data_advisor_gets_only_group_prior_turns(self):
        self._mk(chat_id="777", scope="owner", out="MARKER_OWNER сделала grep")
        self._mk(chat_id="-100", scope="group", who="вася", out="MARKER_GRP ответила в группе")
        sc2 = ScriptedClient(["ответ в группу", "PRIVACY_OK"])
        llm.use_test_client(sc2)
        agent.voice_turn("-100", "вася: а помнишь?", speaker="вася", is_dm=False)
        eval2 = sc2.calls[1]["messages"][0]["content"]
        self.assertIn("lived turns", eval2)
        self.assertIn("MARKER_GRP", eval2, "свои ходы канала — видны")
        self.assertNotIn("MARKER_OWNER", eval2, "owner-DM не утекает в групповой оценщик")

    def test_no_prior_turns_no_block(self):
        sc = ScriptedClient(["первый ответ", "PRIVACY_OK"])
        llm.use_test_client(sc)
        agent.voice_turn("-777", "Гость: привет", speaker="Гость", is_dm=False)
        eval_content = sc.calls[1]["messages"][0]["content"]
        self.assertNotIn("lived turns", eval_content, "пустое кольцо — блок не добавляется")

    def test_state_block_names_last_turn(self):
        self._mk(out="MARKER_STATE закончила ревью")
        state = agent.build_state_block()
        evidence = agent.build_state_evidence_block()
        self.assertIn('"fact":"latest_turn","present":true', state)
        self.assertNotIn("MARKER_STATE", state)
        self.assertIn("MARKER_STATE", evidence)

    def test_state_block_keeps_direct_artifact_after_later_lived_turn(self):
        with mock.patch.dict(os.environ, {"PRAXIS_BASE": str(self.tmp)}):
            row = computer_memory.record(
                {"task_id": "run-direct", "device_id": "windows-pc"},
                "artifact.fetch", {}, "interactive",
                {"ok": True, "artifact": {"name": "report.txt", "size": 7,
                                          "sha256": "a" * 64}},
            )
            evidence = agent.build_state_evidence_block()
            self.assertIn("recent_durable_artifact_evidence", evidence)
            self.assertIn("report.txt", evidence)
            self.assertIn(row["receipt"], evidence)

            self._mk(out="MARKER_LATER lived turn")
            later = agent.build_state_evidence_block()
            self.assertIn("MARKER_LATER", later)
            self.assertIn("report.txt", later)

    def test_state_block_omits_artifact_record_when_none_exist(self):
        with mock.patch.dict(os.environ, {"PRAXIS_BASE": str(self.tmp)}):
            self.assertNotIn("recent_durable_artifact_evidence",
                             agent.build_state_evidence_block())

    def test_missed_frame_answers_with_fact(self):
        boot = agent._BOOT_TS.timestamp()
        self._mk(chat_id="777", out="MARKER_CATCH обещала глянуть утром", ts=boot - 300)
        ctx = agent.ChannelContext(chat_id="777", is_dm=True, missed_hours=3.0)
        frame = agent._presence_frame(ctx)
        evidence = agent._presence_evidence(ctx)
        self.assertIn("[missed]", frame)
        self.assertNotIn("MARKER_CATCH", frame)
        self.assertIn("MARKER_CATCH", evidence)
        ctx2 = agent.ChannelContext(chat_id="888", is_dm=True, missed_hours=3.0)
        self.assertNotIn("MARKER_CATCH", agent._presence_evidence(ctx2),
                         "catch-up строго по этому чату")

    def test_tool_recent_turns_respects_current_scope(self):
        self._mk(chat_id="777", scope="owner", out="приватное")
        self._mk(chat_id="-100", scope="group", who="вася", out="публичное")
        for k in ("_CURRENT_SCOPE", "_CURRENT_CHAT"):
            self._orig.append((agent, k, getattr(agent, k)))
        agent._CURRENT_SCOPE, agent._CURRENT_CHAT = "owner", "777"
        self.assertIn("приватное", agent.tool_recent_turns())
        agent._CURRENT_SCOPE, agent._CURRENT_CHAT = "group", "-100"
        out = agent.tool_recent_turns()
        self.assertIn("публичное", out)
        self.assertNotIn("приватное", out)

    def test_recent_turns_tool_is_owner_registered(self):
        self.assertIn("recent_turns", agent.TOOL_IMPL)
        self.assertIn("recent_turns", [t["name"] for t in agent.OWNER_TOOLS])

    def test_sleep_journals_day_line(self):
        import consolidate
        import sleep as sleep_mod
        self._mk(out="ответ")
        self._mk(out="", held="voice")
        stubs = {
            consolidate: dict(run=lambda: "svs"),
            sleep_mod: dict(svs_dossier_pass=lambda **kw: (0, 0), prune_duplicate_edges=lambda: 0,
                            rumination_pass=lambda: (0, 0), unpark_expired_loops=lambda: 0,
                            rem_pass=lambda rng=None, **kw: (0, False), sweep_inbox=lambda: 0),
        }
        for module, attrs in stubs.items():
            for k, val in attrs.items():
                self._orig.append((module, k, getattr(module, k)))
                setattr(module, k, val)
        sleep_mod.run()
        text = "\n".join(p.read_text(encoding="utf-8")
                         for p in (self.tmp / "memory" / "journal").glob("*.md"))
        self.assertIn("прожитый день, кодом", text)
        self.assertIn("ходов 2", text)
        self.assertIn("промолчала сама 1", text)


if __name__ == "__main__":
    import unittest

    unittest.main()
