"""Прибор, который говорит из чего состоит её кадр, не имеет права этот кадр менять.

Спор о том, что занимает место в контексте Praxis, сутки шёл цифрами, которых никто не
мерил: 110 противоречий, половина — «сколько это в кадре». Прибор `frame_trace` отвечает
ровно на один вопрос — имя секции → зона → тип → смещение → длина — и обязан ответить,
не сдвинув ни байта.

Что здесь проверяется по существу:
  • `mark()` возвращает ТОТ ЖЕ объект, и кадр с прибором и без него побайтово один;
  • склейка помеченных отрезков равна тексту, который реально уехал (иначе смещения —
    красивая ложь);
  • три разных нуля различимы: «не влезло в бюджет» ≠ «ветка не выбрана» ≠ «пусто»;
  • в проде расхождение ЗАПИСЫВАЕТСЯ, а в тестах ПАДАЕТ;
  • сбой прибора стоит пустых метаданных, а не её хода.

⚠ Режим ставится через `frame_trace.set_mode` + addCleanup, а НЕ через окружение:
`_standenv` снимает все PRAXIS_* на входе стенда, а детектор утечек краснит любую
PRAXIS_*, оставшуюся после теста.

Запуск:  python praxis_test.py test_frame_trace -v
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("PRAXIS_TEST", "1")

import agent  # noqa: E402
import frame_layout  # noqa: E402
import frame_trace  # noqa: E402
import run_context  # noqa: E402
import run_manager  # noqa: E402
import run_resume  # noqa: E402

STATE_TEXT = "STATE: фиксированный блок состояния."
JSONL_TEXT = '{"row":1}\n{"row":2}'


class FrameBase(unittest.TestCase):
    """Своя память/душа и заглушённые источники, которые иначе меняются от хода к ходу."""

    MODE = "strict"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_frame_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        mem = self.tmp / "memory"
        soul = self.tmp / "soul"
        for folder in (mem / "people", mem / "rooms", mem / "journal", soul / "skills"):
            folder.mkdir(parents=True, exist_ok=True)
        self.soul = soul
        self.mem = mem
        (soul / "SOUL.md").write_text("# Конституция\nЯ — Praxis.\n", encoding="utf-8")
        (soul / "VOICE.md").write_text("# Голос\nПримеры регистра.\n", encoding="utf-8")

        paths = {
            "BASE": self.tmp, "SOUL_DIR": soul, "SKILLS_DIR": soul / "skills",
            "MEM_DIR": mem, "PEOPLE_DIR": mem / "people", "ROOMS_DIR": mem / "rooms",
            "JOURNAL_DIR": mem / "journal", "INDEX_MD": mem / "INDEX.md",
            "HOME_MD": mem / "HOME.md",
        }
        for name, value in paths.items():
            if hasattr(agent, name):
                self._patch(name, value)

        # Источники, которые честно меняются от хода к ходу (часы внутри STATE, recall,
        # почта). Прибор их не трогает, но сравнение «кадр с прибором и без» требует,
        # чтобы кадр вообще был воспроизводим.
        self._patch("build_state_block", lambda **_kw: STATE_TEXT)
        self._patch("build_state_evidence_block", lambda **_kw: JSONL_TEXT)
        self._patch("_mailbox_index", lambda: "")
        self._patch("other_rooms_digest", lambda **_kw: "")
        self._patch("my_sends_today_digest", lambda: "")
        self._patch("read_summary", lambda _chat_id: "")
        self._patch("_participant_memory_block", lambda _speaker, _ctx: "")
        self._patch("_active_desires_block", lambda limit=10: "")
        self._patch("_recall_block", lambda _query, _scope: "")

        previous = frame_trace.set_mode(self.MODE)
        self.addCleanup(frame_trace.set_mode, previous)

    def _patch(self, name: str, value) -> None:
        patcher = mock.patch.object(agent, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- формы контекста ---

    def owner_dm(self):
        return agent.ChannelContext(chat_id="42", principal_id="42", is_dm=True, owner=True)

    def own_run(self):
        return agent.ChannelContext(chat_id=None, principal_id=agent.PRAXIS_SELF_PRINCIPAL,
                                    is_dm=True, _scope_override="owner")

    def family_dm(self):
        return agent.ChannelContext(chat_id="77", principal_id="77", is_dm=True,
                                    known=True, family=True)

    def unknown_group(self):
        return agent.ChannelContext(chat_id="-100500", principal_id="9", is_dm=False,
                                    known=False)

    def known_group(self):
        return agent.ChannelContext(chat_id="-100501", principal_id="9", is_dm=False,
                                    known=True)

    # --- проход голоса с подменённым тул-циклом ---

    def run_turn(self, *, ctx=None, user="Егор: привет", history=None,
                 extra_system="", extra_evidence="", call_id="model-a", bind=True):
        """Один ход до самого шва сборки кадра. Мока ровно той формы, что у соседей.

        bind=False — не открывать свой след: вызывающий уже держит `capture()` и хочет
        сам спросить `assert_honest()` после хода.
        """
        seen: dict = {}

        def capture(*, system, messages, tools, max_iters=None, tool_trace=None):
            seen["system"] = system
            seen["messages"] = messages
            seen["tools"] = tools
            return "ок"

        def body():
            seen["reply"] = agent._voice_impl(
                user, list(history or []), "Егор", ctx=ctx or self.owner_dm(),
                extra_system=extra_system, extra_evidence=extra_evidence,
            )
            seen["trace"] = frame_trace.current()
            seen["sections"] = frame_trace.sections()
            seen["meta"] = frame_trace.metadata_for(seen["system"], call_id=call_id)

        with mock.patch.object(agent, "_terminal_tool_loop", capture):
            if bind:
                with frame_trace.capture():
                    body()
            else:
                body()
        return seen

    def spy_on_marks(self) -> list[tuple]:
        """Записать (имя, зона, текст) каждой метки — чтобы сверить срезы по смещениям."""
        recorded: list[tuple] = []
        real = frame_trace.mark

        def spy(name, zone, kind, text, **kwargs):
            out = real(name, zone, kind, text, **kwargs)
            recorded.append((name, zone, text))
            return out

        patcher = mock.patch.object(frame_trace, "mark", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return recorded


# ------------------------------------------------------------------ 1. неизменность


class MarkChangesNothing(FrameBase):
    def test_mark_returns_the_very_same_object(self):
        """Не «равную строку», а тот же объект: иначе кадр пересобирается вокруг метки."""
        with frame_trace.capture():
            for text in ("обычный текст", "", "х" * 5000):
                self.assertIs(frame_trace.mark("n", "dynamic", "text", text), text)

    def test_mark_without_a_bound_trace_is_a_pure_no_op(self):
        """Прямые распаковки `_build_prompt_parts` в тестах ходят именно этим путём."""
        text = "ни следа, ни записи"
        self.assertIs(frame_trace.mark("n", "dynamic", "text", text), text)
        self.assertEqual(frame_trace.sections(), [])

    def test_mark_never_raises_even_on_nonsense(self):
        with frame_trace.capture():
            self.assertIs(frame_trace.mark(None, None, None, "цел"), "цел")
            self.assertIsNone(frame_trace.absent("n", "dynamic", "text", "empty"))


class TheFrameIsByteIdenticalWithAndWithoutTheInstrument(FrameBase):
    """Железное требование среза, проверенное механически."""

    MODE = "on"

    def test_system_and_messages_are_equal_at_mode_off_and_mode_on(self):
        frame_trace.set_mode("off")
        without = self.run_turn()
        frame_trace.set_mode("on")
        with_trace = self.run_turn()
        self.assertEqual(without["system"], with_trace["system"],
                         "прибор сдвинул системный промпт")
        self.assertEqual(without["messages"], with_trace["messages"],
                         "прибор сдвинул сообщения")
        self.assertIsNone(without["meta"], "при mode=off метаданных быть не должно")
        self.assertIsNotNone(with_trace["meta"])


# ------------------------------------------------------------------- 3. склейка зон


class TheJoinOfMarksIsTheZoneItself(FrameBase):
    def test_all_three_zones_join_back_byte_for_byte(self):
        with frame_trace.capture():
            persona, tail_text, evidence = agent._build_prompt_parts(
                speaker="Егор", ctx=self.owner_dm())
            self.assertEqual(frame_trace.join("persona"), persona)
            self.assertEqual(frame_trace.join("dynamic"), tail_text)
            self.assertEqual(frame_trace.join("evidence"), evidence)
            rows = frame_trace.sections()
        for zone, text in (("persona", persona), ("dynamic", tail_text),
                           ("evidence", evidence)):
            total = sum(row["chars"] for row in rows
                        if row["zone"] == zone and row["included"])
            self.assertEqual(total, len(text), f"сумма длин зоны {zone} не равна зоне")

    def test_every_offset_cuts_out_exactly_its_own_mark(self):
        recorded = self.spy_on_marks()
        seen = self.run_turn()
        texts = {"persona": seen["system"][0]["text"],
                 "dynamic": seen["system"][1]["text"]}
        marks = {"persona": [t for _n, z, t in recorded if z == "persona"],
                 "dynamic": [t for _n, z, t in recorded if z == "dynamic"]}
        for zone, zone_text in texts.items():
            rows = [r for r in seen["sections"] if r["zone"] == zone and r["included"]]
            self.assertEqual(len(rows), len(marks[zone]))
            for row, marked in zip(rows, marks[zone]):
                cut = zone_text[row["offset"]:row["offset"] + row["chars"]]
                self.assertEqual(cut, marked, f"{row['name']}: срез по смещению — не она")

    def test_zone_honesty_and_hashes_are_published(self):
        seen = self.run_turn()
        zones = {row["zone"]: row for row in seen["meta"]["zones"]}
        # Пятая зона кадра публикуется наравне с тремя: она лежит внутри конверта
        # evidence, но меряется отдельно — иначе её длина растворяется в чужом числе.
        self.assertEqual(set(zones), {"persona", "dynamic", "evidence", "situation"})
        for row in zones.values():
            self.assertTrue(row["honest"], row)
        self.assertEqual(
            zones["persona"]["sha256"],
            hashlib.sha256(seen["system"][0]["text"].encode("utf-8")).hexdigest())
        self.assertEqual(zones["persona"]["container"], "system[0].text")
        self.assertEqual(zones["dynamic"]["container"], "system[1].text")
        self.assertEqual(zones["evidence"]["container"], "messages[-1]")


# --------------------------------------------------------------- 4. полнота реестра


class EveryRosterNameIsEitherMarkedOrDeclaredAbsent(FrameBase):
    """«Молча нет» — это ровно тот класс лжи, ради которого прибор и строился."""

    def _names(self, ctx) -> list[str]:
        with frame_trace.capture():
            agent._build_prompt_parts(speaker="Егор", ctx=ctx)
            return [row["name"] for row in frame_trace.sections()]

    def test_four_shapes_of_context_each_name_exactly_once(self):
        for label, ctx in (("owner-DM", self.owner_dm()),
                           ("собственный ход", self.own_run()),
                           ("family", self.family_dm()),
                           ("неизвестный в группе", self.unknown_group()),
                           ("известный в группе", self.known_group())):
            with self.subTest(shape=label):
                names = self._names(ctx)
                for wanted in frame_trace.SYSTEM_ROSTER:
                    self.assertEqual(names.count(wanted), 1,
                                     f"{label}: «{wanted}» назван {names.count(wanted)} раз")

    def test_the_owner_contract_is_marked_only_in_owner_shapes(self):
        for ctx, expected in ((self.owner_dm(), True), (self.own_run(), True),
                              (self.family_dm(), False), (self.unknown_group(), False)):
            with frame_trace.capture():
                agent._build_prompt_parts(speaker="Егор", ctx=ctx)
                row = next(r for r in frame_trace.sections()
                           if r["name"] == "contract.owner_tools")
            self.assertEqual(row["included"], expected, row)
            if not expected:
                self.assertEqual(row["reason"], "branch")

    def test_the_place_variant_names_which_of_three_literals_travelled(self):
        for ctx, variant in ((self.owner_dm(), "owner_dm"), (self.own_run(), "own_run")):
            with frame_trace.capture():
                agent._build_prompt_parts(speaker="Егор", ctx=ctx)
                row = next(r for r in frame_trace.sections()
                           if r["name"] == "state.owner_place")
            self.assertEqual(row["variant"], variant)


# ----------------------------------------------------------------- 5-7. три нуля


class ThreeDifferentZeroesStayDifferent(FrameBase):
    def test_budget_branch_and_empty_are_three_representations(self):
        (self.soul / "VOICE.md").write_text("", encoding="utf-8")
        with mock.patch.dict(os.environ, {"PRAXIS_CONTEXT_BUDGET": "200"}):
            with frame_trace.capture():
                agent._build_prompt_parts(speaker="Егор", ctx=self.owner_dm())
                rows = frame_trace.sections()
        by_name = {}
        for row in rows:
            by_name.setdefault(row["name"], []).append(row)

        dropped = [r for r in by_name["evidence.tier"]
                   if not r["included"] and r["reason"] == "context_budget"]
        self.assertTrue(dropped, "бюджет 200 обязан был что-то выбросить")
        self.assertTrue(all(r["chars"] > 0 for r in dropped),
                        "выброшенное бюджетом обязано нести длину того, что не влезло")
        self.assertTrue(all(r.get("label") for r in dropped), "ярлык выброшенного потерян")

        branch = by_name["contract.family_audience"][0]
        self.assertFalse(branch["included"])
        self.assertEqual(branch["reason"], "branch")
        self.assertNotIn("chars", branch, "у невыбранной ветки нет длины — её не было")

        empty = by_name["persona.voice"][0]
        self.assertFalse(empty["included"])
        self.assertEqual(empty["reason"], "empty")

        self.assertEqual(len({"context_budget", "branch", "empty"}
                             & {r.get("reason") for r in rows}), 3,
                         "три нуля схлопнулись в один")

    def test_a_tier_that_never_assembled_gets_no_record_at_all(self):
        """Четвёртый ноль — «до решения не дошли» — записи не получает по построению."""
        with frame_trace.capture():
            agent._build_prompt_parts(speaker="Егор", ctx=self.owner_dm())
            counts = frame_trace.current().counts
            rows = frame_trace.sections()
        tier_rows = [r for r in rows if r["name"] == "evidence.tier"]
        self.assertEqual(len(tier_rows), counts["tiers_offered"],
                         "записей о тирах больше, чем тиров вообще собиралось")


class TheBudgetIsMeasuredWithTheSameRuler(FrameBase):
    def test_used_start_is_the_expression_the_code_itself_uses(self):
        with frame_trace.capture():
            persona, tail_text, _evidence = agent._build_prompt_parts(
                speaker="Егор", ctx=self.owner_dm())
            budget = frame_trace.current().budget
        self.assertEqual(budget["used_start"], len(persona) + len(tail_text))
        self.assertEqual(budget["unit"], "chars")
        self.assertEqual(tuple(budget["excludes"]), frame_trace.BUDGET_EXCLUDES)

    def test_what_the_budget_dropped_equals_what_the_frame_said_out_loud(self):
        """И заодно — первая находка самого прибора, ради которой он и строился.

        `PRAXIS_CONTEXT_BUDGET` НЕ ограничивает кадр. Он гейтит только опциональные тиры,
        а стартовое `used` уже содержит персону и хвост: при бюджете 200 замер даёт
        used_final = 4274 при limit = 200, и это не поломка, а устройство. Утверждение
        «used_final <= limit» было бы ложью прибора о коде — поэтому проверяется то, что
        правда: превысить бюджет может только обязательная часть, ни один ВКЛЮЧЁННЫЙ
        тир этого сделать не может.
        """
        with mock.patch.dict(os.environ, {"PRAXIS_CONTEXT_BUDGET": "200"}):
            with frame_trace.capture():
                _persona, _tail, evidence = agent._build_prompt_parts(
                    speaker="Егор", ctx=self.owner_dm())
                rows = frame_trace.sections()
                budget = frame_trace.current().budget
        spoken = ""
        for line in evidence.splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("label") == "omitted_by_context_budget":
                spoken = row["content"]
        # Кадр называет выброшенное ЗАГОЛОВКОМ ярлыка: скобочная оговорка сборщика в
        # список не едет, иначе разделитель «; » встретился бы внутри самого ярлыка.
        traced = {frame_layout.split_title(r["label"])[0] for r in rows
                  if r.get("reason") == "context_budget" and r.get("label")}
        named = {part.strip() for part in spoken.split(";")
                 if part.strip() and part.strip() != "available through explicit recall"}
        self.assertEqual(traced, named, "след и кадр называют разное выброшенным")
        counts = {"included": len([r for r in rows if r["name"] == "evidence.tier"
                                   and r["included"]])}
        if counts["included"]:
            self.assertLessEqual(budget["used_final"], budget["limit"])
        else:
            self.assertEqual(budget["used_final"], budget["used_start"],
                             "ни один тир не влез, а счётчик всё равно вырос")
        self.assertGreater(budget["used_start"], budget["limit"],
                           "при таком бюджете обязательная часть обязана его превышать — "
                           "иначе находка выше перестала быть находкой")


# ------------------------------------------------------- 8. финальный хвост, не возврат


class TheTraceDescribesTheFinalTailNotTheBuildersReturn(FrameBase):
    def test_extra_system_appended_after_the_builder_is_in_the_trace(self):
        extra = "\n\nПУЛЬС: окно открыто, собеседника нет.\n"
        seen = self.run_turn(extra_system=extra)
        row = next(r for r in seen["sections"] if r["name"] == "frame.extra_system")
        self.assertTrue(row["included"])
        self.assertEqual(row["chars"], len(extra))
        dynamic = seen["system"][1]["text"]
        self.assertEqual(dynamic[row["offset"]:row["offset"] + row["chars"]], extra)
        zone = next(z for z in seen["meta"]["zones"] if z["zone"] == "dynamic")
        self.assertEqual(zone["chars"], len(dynamic))
        self.assertTrue(zone["honest"],
                        "съём на возврате билдера был бы ложью — здесь он правда")


# ------------------------------------------------------------ 9-10. честность/строгость


class _Corrupted:
    """Порча ровно одной метки: запись длиннее кадра на известное число символов."""

    EXTRA = "ЛИШНЕЕ"

    def __init__(self, target: str):
        self.target = target
        self.real = frame_trace.mark

    def __call__(self, name, zone, kind, text, **kwargs):
        if name == self.target:
            self.real(name, zone, kind, text + self.EXTRA, **kwargs)
            return text
        return self.real(name, zone, kind, text, **kwargs)


class ProductionRecordsTheMismatchAndKeepsGoing(FrameBase):
    MODE = "on"

    def test_a_broken_mark_costs_a_flag_not_her_turn(self):
        broken = _Corrupted("state.channel_facts")
        with mock.patch.object(frame_trace, "mark", broken):
            seen = self.run_turn()
        self.assertEqual(seen["reply"], "ок", "прибор украл у неё ход")
        meta = seen["meta"]
        self.assertIsNotNone(meta, "расхождение обязано быть ЗАПИСАНО, а не проглочено")
        self.assertFalse(meta["honesty"]["ok"])
        self.assertEqual(meta["honesty"]["zone"], "dynamic")
        dynamic = seen["system"][1]["text"]
        self.assertEqual(meta["honesty"]["at"], len(dynamic),
                         "первое расходящееся смещение названо неверно")
        self.assertEqual(meta["honesty"]["marked_chars"],
                         len(dynamic) + len(_Corrupted.EXTRA))
        self.assertEqual(meta["honesty"]["actual_chars"], len(dynamic))
        zone = next(z for z in meta["zones"] if z["zone"] == "dynamic")
        self.assertFalse(zone["honest"])
        self.assertTrue(next(z for z in meta["zones"] if z["zone"] == "persona")["honest"],
                        "нечестной объявлена не та зона")

    def test_offsets_are_still_published_for_what_was_marked(self):
        broken = _Corrupted("state.channel_facts")
        with mock.patch.object(frame_trace, "mark", broken):
            seen = self.run_turn()
        rows = [r for r in seen["meta"]["sections"] if r["zone"] == "dynamic"]
        self.assertTrue(all("offset" in r for r in rows if r["included"]))


class StrictModeFallsSoTheGateGoesRed(FrameBase):
    """strict краснит ГЕЙТ, а не её разговор.

    Бросок живёт в `assert_honest()`, который зовут тесты, и НИКОГДА не вылетает из шва
    сборки кадра: `PRAXIS_FRAME_TRACE=strict` принимает пульт (`panel.env_apply` пропускает
    любой неизвестный `PRAXIS_*`), и прежний бросок из `seal` стоил бы ей каждого хода.
    """

    MODE = "strict"

    def test_the_turn_survives_even_in_strict(self):
        broken = _Corrupted("state.channel_facts")
        with mock.patch.object(frame_trace, "mark", broken):
            seen = self.run_turn()
        self.assertEqual(seen["reply"], "ок", "strict украл у неё ход")

    def test_the_same_mismatch_raises_when_the_gate_asks(self):
        broken = _Corrupted("state.channel_facts")
        with mock.patch.object(frame_trace, "mark", broken):
            with frame_trace.capture():
                self.run_turn(bind=False)
                with self.assertRaises(frame_trace.FrameTraceMismatch):
                    frame_trace.assert_honest()

    def test_a_broken_instrument_also_reddens_the_gate(self):
        # Прежнее условие смотрело только на honesty, а она при упавшем _seal_impl
        # оставалась {"ok": True}: наблюдение исчезало, гейт оставался зелёным.
        with frame_trace.capture():
            self.run_turn(bind=False)
            trace = frame_trace.current()
            trace.usable = False
            with self.assertRaises(frame_trace.FrameTraceMismatch):
                frame_trace.assert_honest()

    def test_a_clean_frame_does_not_raise_in_strict(self):
        self.assertEqual(self.run_turn()["reply"], "ок")
        with frame_trace.capture():
            self.run_turn(bind=False)
            frame_trace.assert_honest()


# ------------------------------------------------------------- 11-13, 16, 19, 23. расписка


class ReceiptBase(FrameBase):
    MODE = "on"

    def setUp(self) -> None:
        super().setUp()
        self.runs = tempfile.TemporaryDirectory(prefix="praxis_runs_")
        self.addCleanup(self.runs.cleanup)
        self.manager = run_manager.RunManager(self.runs.name)
        self._patch("_RUN_MANAGER", self.manager)

    def durable_run(self, goal="frame trace"):
        ctx = run_context.RunContext.create(
            kind="test", goal=goal, principal_id="owner", scope="owner",
            origin_chat_id="1", delivery_chat_id="1",
        )
        ctx = self.manager.create(ctx, "FULL CONTEXT\n")
        self.manager.transition(ctx.run_id, "running")
        return ctx.with_status("running")

    def model_input_events(self, run_id: str) -> list[dict]:
        return [row for row in self.manager.events(run_id)
                if row.get("kind") == "model_input"]

    def voice_turn(self, run, *, ctx=None, user="Егор: привет"):
        response = types.SimpleNamespace(stop_reason="end_turn", text="готово", blocks=[])
        with run_context.bind_run(run), \
                mock.patch.object(agent.llm, "chat", return_value=response), \
                frame_trace.capture():
            return agent._voice_impl(user, [], "Егор", ctx=ctx or self.owner_dm())


class TheReceiptCarriesTheTraceAndResumeSurvivesIt(ReceiptBase):
    def test_model_input_event_carries_the_envelope(self):
        run = self.durable_run()
        self.assertEqual(self.voice_turn(run), "готово")
        rows = self.model_input_events(run.run_id)
        self.assertEqual(len(rows), 1)
        meta = rows[0]["metadata"]
        self.assertEqual(meta["schema"], "praxis.frame-trace.v1")
        # ⚠ Реестр двинулся вместе с кадром: v2 добавила пятую зону «сейчас» и её
        # закрытый список из восьми имён. Прежний номер при новом составе означал бы,
        # что расписки двух разных форм неразличимы задним числом.
        self.assertEqual(meta["roster"], "v2")
        self.assertEqual(meta["basis"], "live")
        self.assertEqual(meta["emit"], 1)

        payload = json.loads(
            (self.manager.path(run.run_id) / "results" / "0001-model-input.log")
            .read_text(encoding="utf-8"))
        persona_text = payload["system"][0]["text"]
        zone = next(z for z in meta["zones"] if z["zone"] == "persona")
        self.assertEqual(zone["sha256"],
                         hashlib.sha256(persona_text.encode("utf-8")).hexdigest(),
                         "хэш зоны не сходится с тем, что легло в расписку")

    def test_resume_planning_does_not_choke_on_the_new_metadata(self):
        run = self.durable_run()
        self.voice_turn(run)
        try:
            run_resume.plan_resume(self.manager, run.run_id)
        except run_resume.ResumeEvidenceError as exc:  # pragma: no cover - это и есть провал
            self.fail(f"след сломал планирование возобновления: {exc}")
        except Exception:
            pass  # прочие отказы плана (статус, отсутствие вывода) — не про метаданные

    def test_call_ids_stay_random_and_that_is_load_bearing(self):
        """Тревожная растяжка. Сделать call_id детерминированным — значит зарядить
        RunConflict на ЛЮБЫХ метаданных model_input: путь возобновления кадр не
        пересобирает и следа не имеет, поэтому на повторе observed != None."""
        run = self.durable_run()
        first = agent._model_call.__wrapped__ if hasattr(agent._model_call, "__wrapped__") else None
        self.assertIsNone(first, "обёртка вокруг _model_call изменила бы разбор ниже")
        response = types.SimpleNamespace(stop_reason="end_turn", text="ок", blocks=[])
        system = [{"type": "text", "text": "persona"}, {"type": "text", "text": "dynamic"}]
        with run_context.bind_run(run), \
                mock.patch.object(agent.llm, "chat", return_value=response):
            agent._model_call(system, [], [])
            agent._model_call(system, [], [])
        ids = [row["call_id"] for row in self.model_input_events(run.run_id)]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])
        for value in ids:
            self.assertRegex(value, r"^model-[0-9a-f]{32}$")


class TheSecondCallOnTheSameFrameIsMarkedReused(ReceiptBase):
    """Число расписок ≠ число кадров. Сорок итераций тул-цикла — один кадр."""

    def _sealed_frame(self):
        persona, dynamic, evidence = agent._build_prompt_parts(
            speaker="Егор", ctx=self.owner_dm())
        system = agent._system(persona, dynamic)
        frame_trace.seal(persona=persona, dynamic=dynamic, evidence=evidence,
                         system=system)
        return system

    def test_full_envelope_then_thin_reference(self):
        run = self.durable_run()
        response = types.SimpleNamespace(stop_reason="end_turn", text="ок", blocks=[])
        with run_context.bind_run(run), \
                mock.patch.object(agent.llm, "chat", return_value=response), \
                frame_trace.capture():
            system = self._sealed_frame()
            agent._model_call(system, [], [])
            agent._model_call(system, [], [])
        metas = [row.get("metadata") for row in self.model_input_events(run.run_id)]
        self.assertEqual(len(metas), 2)
        self.assertEqual(metas[0]["emit"], 1)
        self.assertNotIn("reused", metas[0])
        self.assertEqual(metas[1]["emit"], 2)
        self.assertTrue(metas[1]["reused"])
        self.assertEqual(metas[0]["frame_id"], metas[1]["frame_id"])
        self.assertNotIn("sections", metas[1])
        self.assertLess(
            len(json.dumps(metas[1], ensure_ascii=False, separators=(",", ":"))), 200,
            "тонкая ссылка растолстела — ради неё всё и затевалось")

    def test_the_same_call_id_twice_returns_an_equal_dict(self):
        """Мемоизация по call_id — прямая защита от RunConflict на повторе расписки."""
        with frame_trace.capture():
            system = self._sealed_frame()
            first = frame_trace.metadata_for(system, call_id="model-повтор")
            second = frame_trace.metadata_for(system, call_id="model-повтор")
        self.assertEqual(first, second)
        self.assertEqual(first["emit"], 1)
        self.assertEqual(second["emit"], 1, "повтор выдал себя за новый кадр")


class AForeignFrameNeverGetsTheTrace(FrameBase):
    MODE = "on"

    def test_a_deepcopy_of_someone_elses_system_is_refused(self):
        seen = self.run_turn()
        with frame_trace.capture():
            persona, dynamic, evidence = agent._build_prompt_parts(
                speaker="Егор", ctx=self.owner_dm())
            mine = agent._system(persona, dynamic)
            frame_trace.seal(persona=persona, dynamic=dynamic, evidence=evidence,
                             system=mine)
            self.assertIsNotNone(frame_trace.metadata_for(mine, call_id="a"))
            foreign = copy.deepcopy(mine)
            foreign[0]["text"] = foreign[0]["text"] + " чужое"
            self.assertIsNone(frame_trace.metadata_for(foreign, call_id="b"),
                              "подменённый кадр получил чужой след")
            # deepcopy БЕЗ правки — тот же текст, значит тот же кадр: это законно.
            self.assertIsNotNone(frame_trace.metadata_for(copy.deepcopy(mine),
                                                          call_id="c"))
        del seen

    def test_clear_detaches_the_trace_the_way_the_resume_path_does(self):
        with frame_trace.capture():
            persona, dynamic, evidence = agent._build_prompt_parts(
                speaker="Егор", ctx=self.owner_dm())
            system = agent._system(persona, dynamic)
            frame_trace.seal(persona=persona, dynamic=dynamic, evidence=evidence,
                             system=system)
            frame_trace.clear()
            self.assertIsNone(frame_trace.current())
            self.assertIsNone(frame_trace.metadata_for(system, call_id="a"))

    def test_the_resume_path_calls_clear_before_rebuilding_the_loop(self):
        source = inspect.getsource(agent._AgentResumeRuntime.continue_tool_response)
        self.assertIn("frame_trace.clear()", source)


class WithoutATraceTheReceiptHasNoMetadataKeyAtAll(ReceiptBase):
    def test_key_is_absent_not_empty(self):
        run = self.durable_run()
        response = types.SimpleNamespace(stop_reason="end_turn", text="ок", blocks=[])
        with run_context.bind_run(run), \
                mock.patch.object(agent.llm, "chat", return_value=response):
            agent._model_call("plain system", [], [])
        row = self.model_input_events(run.run_id)[0]
        self.assertNotIn("metadata", row, "None превратился в пустой словарь — это не одно и то же")


class AnInstrumentFailureNeverCostsHerTurn(ReceiptBase):
    def test_metadata_for_that_raises_leaves_the_receipt_intact(self):
        run = self.durable_run()
        stopped = []
        response = types.SimpleNamespace(stop_reason="end_turn", text="готово", blocks=[])

        def boom(*_args, **_kwargs):
            raise RuntimeError("прибор сломался")

        with mock.patch.object(frame_trace, "metadata_for", boom), \
                mock.patch.object(agent, "_stop_for_durability",
                                  lambda *a, **k: stopped.append(a)), \
                run_context.bind_run(run), \
                mock.patch.object(agent.llm, "chat", return_value=response), \
                frame_trace.capture():
            answer = agent._voice_impl("Егор: привет", [], "Егор", ctx=self.owner_dm())
        self.assertEqual(answer, "готово")
        self.assertEqual(stopped, [], "сбой прибора увёл ход в _stop_for_durability")
        row = self.model_input_events(run.run_id)[0]
        self.assertNotIn("metadata", row)

    def test_seal_that_raises_is_swallowed_and_the_frame_survives(self):
        with mock.patch.object(frame_trace, "_seal_impl",
                               mock.Mock(side_effect=RuntimeError("бум"))):
            seen = self.run_turn()
        self.assertEqual(seen["reply"], "ок")
        self.assertIsNone(seen["meta"], "сломанный след всё-таки что-то напечатал")


class TheScrubberDoesNotReAnchorTheTrace(ReceiptBase):
    def test_offsets_still_describe_the_live_text(self):
        run = self.durable_run()
        secret = "auth-secret-731946"
        messages = [{"role": "assistant", "content": [{
            "type": "tool_use", "id": "critical-1", "name": "telegram_account",
            "input": {"action": "call", "request": "functions.auth.SignInRequest",
                      "params": {"phone_number": "+100000000", "phone_code": secret}},
        }]}]
        response = types.SimpleNamespace(stop_reason="end_turn", text="ок", blocks=[])
        with run_context.bind_run(run), \
                mock.patch.object(agent.llm, "chat", return_value=response), \
                frame_trace.capture():
            persona, dynamic, evidence = agent._build_prompt_parts(
                speaker="Егор", ctx=self.owner_dm())
            system = agent._system(persona, dynamic)
            frame_trace.seal(persona=persona, dynamic=dynamic, evidence=evidence,
                             system=system)
            agent._model_call(system, messages, [])
        meta = self.model_input_events(run.run_id)[0]["metadata"]
        self.assertTrue(meta["receipt"]["scrubbed_possible"],
                        "ход с account-critical секретом обязан назвать развилку")
        self.assertEqual(meta["basis"], "live")
        zone = next(z for z in meta["zones"] if z["zone"] == "dynamic")
        self.assertEqual(zone["sha256"],
                         hashlib.sha256(dynamic.encode("utf-8")).hexdigest(),
                         "след пере-якорился на подчищенный текст расписки")
        self.assertEqual(zone["chars"], len(dynamic))


# ------------------------------------------------------- 14-15, 17-18, 20-22. форма


class ArityAndShapesOfNeighboursAreUntouched(FrameBase):
    """Цена ошибки здесь названа замером, а не по памяти: 18 прямых распаковок
    `_build_prompt_parts` в 8 тест-файлах плюс мока литеральным 3-кортежем в
    test_authority_context.py:155. Арность считается ЗДЕСЬ же, из живого дерева, —
    число, вписанное руками, отстанет от соседа молча."""

    def test_build_prompt_parts_still_returns_exactly_three(self):
        persona, dynamic, evidence = agent._build_prompt_parts(ctx=self.owner_dm())
        for value in (persona, dynamic, evidence):
            self.assertIsInstance(value, str)
        self.assertEqual(
            len(inspect.signature(agent._build_prompt_parts).parameters), 8)

    def test_the_direct_unpackings_in_the_neighbours_are_still_three_wide(self):
        here = Path(__file__).resolve().parent
        sites = 0
        files = set()
        for path in sorted(here.glob("test_*.py")):
            if path.name == Path(__file__).name:
                continue
            body = path.read_text(encoding="utf-8", errors="replace")
            found = body.count("agent._build_prompt_parts(")
            if found:
                sites += found
                files.add(path.name)
        self.assertGreaterEqual(sites, 15,
                                "распаковки исчезли — сверка арности стала вакуумной")
        self.assertIn("test_memory_v2.py", files)

    def test_system_returns_str_or_list(self):
        self.assertIsInstance(agent._system("a", "b"), list)
        with mock.patch.dict(os.environ, {"PRAXIS_PROMPT_CACHE": "0"}):
            self.assertIsInstance(agent._system("a", "b"), str)

    def test_terminal_tool_loop_keywords_did_not_move(self):
        params = inspect.signature(agent._terminal_tool_loop).parameters
        self.assertEqual(set(params),
                         {"system", "messages", "tools", "max_iters", "tool_trace",
                          "start_iteration"})

    def test_the_receipt_payload_line_stayed_literal(self):
        """test_system_not_repr читает исходник `_model_call` и требует этой строки."""
        source = inspect.getsource(agent._model_call)
        self.assertIn('"system": _scrub_critical_value', source)
        self.assertIn("metadata=frame_meta", source)


class TheEnvelopeIsStrictJson(FrameBase):
    MODE = "on"

    def test_round_trip_through_the_run_manager_normalization_changes_nothing(self):
        meta = self.run_turn()["meta"]
        encoded = json.dumps(meta, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        self.assertEqual(json.loads(encoded), meta,
                         "нормализация run_manager переставила бы содержимое")

    def test_only_json_native_scalars_travel(self):
        meta = self.run_turn()["meta"]

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertIsInstance(key, str)
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
            else:
                self.assertIsInstance(node, (str, int, bool))
                self.assertNotIsInstance(node, float)

        walk(meta)

    def test_the_envelope_fits_its_own_ceiling(self):
        meta = self.run_turn()["meta"]
        size = len(json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
                   .encode("utf-8"))
        self.assertLess(size, frame_trace.MAX_METADATA_BYTES)


class TheCeilingIsObeyedWithoutRaising(unittest.TestCase):
    def setUp(self) -> None:
        previous = frame_trace.set_mode("on")
        self.addCleanup(frame_trace.set_mode, previous)

    def test_five_hundred_tiers_are_capped_and_said_out_loud(self):
        blocks = [f'{{"label":"тир {i}","content":"{"я" * 200}"}}\n' for i in range(500)]
        with frame_trace.capture():
            for index, block in enumerate(blocks):
                frame_trace.mark("evidence.tier", "evidence", "json", block,
                                 label=f"довольно длинный русский ярлык тира {index}")
            evidence = "".join(blocks)
            system = [{"type": "text", "text": "p"}, {"type": "text", "text": "d"}]
            frame_trace.seal(persona="p", dynamic="d", evidence=evidence, system=system)
            meta = frame_trace.metadata_for(system, call_id="c")
        self.assertIsNotNone(meta)
        self.assertLessEqual(len(meta["sections"]), frame_trace.MAX_SECTIONS)
        self.assertGreater(meta["caps"]["sections_dropped"], 0)
        size = len(json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
                   .encode("utf-8"))
        self.assertLessEqual(size, frame_trace.MAX_METADATA_BYTES)
        self.assertTrue(next(z for z in meta["zones"]
                             if z["zone"] == "evidence")["honest"],
                        "сброс публикации не имеет права ломать инвариант склейки")

    def test_the_emit_counter_stops_lying_past_its_own_ceiling(self):
        system = [{"type": "text", "text": "p"}, {"type": "text", "text": "d"}]
        with frame_trace.capture():
            frame_trace.mark("persona.soul", "persona", "md", "p")
            frame_trace.mark("contract.base", "dynamic", "text", "d")
            frame_trace.seal(persona="p", dynamic="d", evidence="", system=system)
            for index in range(frame_trace.MAX_EMIT_KEYS):
                self.assertIsNotNone(frame_trace.metadata_for(system,
                                                              call_id=f"c{index}"))
            overflow = frame_trace.metadata_for(system, call_id="за-потолком")
        self.assertNotIn("emit", overflow, "порядковый номер выдуман, а не измерен")
        self.assertTrue(overflow["reused"])


class NoTokensAndNoContent(FrameBase):
    MODE = "on"

    def test_not_a_single_token_quantity(self):
        """Решение Егора, закреплённое механически: прибор печатает наблюдённое."""
        encoded = json.dumps(self.run_turn()["meta"], ensure_ascii=False).lower()
        self.assertNotIn("token", encoded)
        self.assertNotIn("токен", encoded)

    def test_not_a_single_byte_of_frame_content(self):
        canary_soul = "КАНАРЕЙКА_ДУШИ_9f2c"
        canary_state = "КАНАРЕЙКА_СОСТОЯНИЯ_7d0e"
        canary_tier = "КАНАРЕЙКА_ТИРА_4c31"
        (self.soul / "SOUL.md").write_text(f"# Конституция\n{canary_soul}\n",
                                           encoding="utf-8")
        self._patch("build_state_block", lambda **_kw: f"STATE: {canary_state}")
        self._patch("_recall_block", lambda _q, _s: canary_tier)
        seen = self.run_turn()
        encoded = json.dumps(seen["meta"], ensure_ascii=False)
        for canary in (canary_soul, canary_state, canary_tier):
            self.assertNotIn(canary, encoded, f"содержимое кадра утекло в след: {canary}")
        # ...и при этом канарейки ДЕЙСТВИТЕЛЬНО были в кадре — иначе сверка вакуумна.
        frame = seen["system"][0]["text"] + seen["system"][1]["text"]
        self.assertIn(canary_soul, frame)
        self.assertIn(canary_state, frame)

    def test_tier_labels_are_code_literals_and_may_travel(self):
        seen = self.run_turn()
        # ⚠ Ярлыки теперь двух ПОРОД: у тира — заголовок секции, у строки зоны «сейчас» —
        # её собственная подпись слева. Смешивать их в одну сверку значит спрашивать с
        # каждого чужую форму; поэтому две проверки, а не одна размытая.
        labels = {row["label"] for row in seen["meta"]["sections"]
                  if row.get("label") and row.get("name") == "evidence.tier"}
        zone_labels = {row["label"] for row in seen["meta"]["sections"]
                       if row.get("label") and str(row.get("zone")) == "situation"}
        self.assertTrue(labels, "ни одного ярлыка — сверка выше вакуумна")
        evidence_text = seen["messages"][-1]["content"]
        if not isinstance(evidence_text, str):
            evidence_text = "".join(str(b.get("text", "")) for b in evidence_text
                                    if isinstance(b, dict))
        # ⚠ Ярлык ПРИЕЗЖАЕТ В КАДР РАЗОБРАННЫМ, а не строкой: заголовок стоит в правиле
        # секции капсом, а скобочный хвост — это оговорка СБОРЩИКА и живёт в подписи.
        # Предмет теста от этого не меняется: ярлык обязан быть литералом кода, который
        # ВИДЕН в кадре целиком, — просто видимость проверяется по обеим его частям.
        for label in labels:
            head, remark = frame_layout.split_title(label)
            self.assertIn(head.upper(), evidence_text,
                          "заголовок ярлыка не из кадра — это новая утечка")
            if remark:
                self.assertIn(remark, evidence_text,
                              "оговорка ярлыка исчезла из кадра — сказанное потерялось")
        for label in zone_labels:
            self.assertIn(label, evidence_text,
                          "ярлык строки зоны не из кадра — это новая утечка")


class TheEvidenceEnvelopeIsDescribedHonestly(FrameBase):
    MODE = "on"

    def test_the_material_can_be_cut_back_out_of_the_last_message(self):
        built: dict = {}
        real = agent._build_prompt_parts

        def spy(*args, **kwargs):
            parts = real(*args, **kwargs)
            built["evidence"] = parts[2]
            return parts

        with mock.patch.object(agent, "_build_prompt_parts", spy):
            seen = self.run_turn(extra_evidence="ход поднят пульсом")
        zone = next(z for z in seen["meta"]["zones"] if z["zone"] == "evidence")
        embed = zone["embed"]
        self.assertEqual(embed["prefix_chars"], len("<praxis_context_evidence>\n"))
        # ⚠ Хвост конверта вырос ДВАЖДЫ: между закрытием evidence и открытием реплики
        # встала зона «СЕЙЧАС», а сам тег реплики ПОДПИСАН (author/tg/message). Литерал
        # голого тега стал бы здесь обещанием, которого кадр не даёт: сверка берёт ту же
        # строку у того же кода, что её печатает, и длину зоны — из её же записи.
        reply_open = frame_layout.reply_open()
        situation_chars = next((row["chars"] for row in seen["meta"]["zones"]
                                if row["zone"] == "situation"), 0)
        self.assertEqual(embed["suffix_chars"],
                         len("\n</praxis_context_evidence>\n") + len(reply_open)
                         + len("\n</current_user_message>") + situation_chars)
        tail = seen["messages"][-1]["content"]
        text = tail if isinstance(tail, str) else "".join(
            str(b.get("text", "")) for b in tail if isinstance(b, dict))
        self.assertTrue(text.startswith("<praxis_context_evidence>\n"))
        material = text[embed["prefix_chars"]:
                        embed["prefix_chars"] + embed["material_chars"]]
        self.assertTrue(material.endswith("ход поднят пульсом"))
        self.assertGreater(embed["siblings_chars"], 0, "сосед в конверте не назван")
        self.assertEqual(embed["separator_chars"], 2)

        # Зона следа — РОВНО та строка, что вернул билдер; конверт съел у неё хвост.
        evidence = built["evidence"]
        self.assertEqual(zone["sha256"],
                         hashlib.sha256(evidence.encode("utf-8")).hexdigest())
        own = material[:embed["material_chars"] - embed["separator_chars"]
                       - embed["siblings_chars"]]
        self.assertEqual(evidence[embed["trim_head"]:
                                  len(evidence) - embed["trim_tail"]], own,
                         "по объявленным числам зона evidence обратно не вырезается")
        self.assertEqual(embed["trim_head"], 0)
        self.assertGreater(embed["trim_tail"], 0, ".strip() съел ноль — сверка вакуумна")

    def test_without_a_sibling_the_numbers_say_so(self):
        seen = self.run_turn()
        embed = next(z for z in seen["meta"]["zones"]
                     if z["zone"] == "evidence")["embed"]
        self.assertEqual(embed["siblings_chars"], 0)
        self.assertEqual(embed["separator_chars"], 0)


class TwoCoordinateSystemsAreNamedNotMerged(FrameBase):
    MODE = "on"

    def test_blocks_by_default(self):
        meta = self.run_turn()["meta"]
        self.assertEqual(meta["system_form"], "blocks")
        self.assertNotIn("dynamic_offset", meta)
        containers = {z["zone"]: z["container"] for z in meta["zones"]}
        self.assertEqual(containers["persona"], "system[0].text")
        self.assertEqual(containers["dynamic"], "system[1].text")

    def test_a_plain_string_system_gets_its_own_offset_and_not_a_shared_axis(self):
        with mock.patch.dict(os.environ, {"PRAXIS_PROMPT_CACHE": "0"}):
            blocks_free = self.run_turn()
        meta = blocks_free["meta"]
        self.assertEqual(meta["system_form"], "string")
        self.assertIsInstance(blocks_free["system"], str)
        persona_len = next(z for z in meta["zones"] if z["zone"] == "persona")["chars"]
        self.assertEqual(meta["dynamic_offset"], persona_len)
        first_dynamic = next(r for r in meta["sections"]
                             if r["zone"] == "dynamic" and r["included"])
        self.assertEqual(first_dynamic["offset"], 0,
                         "смещение зоны dynamic отсчиталось от начала строки, а не зоны")
        cut_at = meta["dynamic_offset"] + first_dynamic["offset"]
        self.assertEqual(blocks_free["system"][cut_at:cut_at + first_dynamic["chars"]],
                         blocks_free["system"][persona_len:
                                               persona_len + first_dynamic["chars"]])


class TheInstrumentIsActuallyBoundOnTheLiveSeam(FrameBase):
    """Единственный тест, который краснеет, если прибор снять с ПРОДА.

    Все остальные зовут `_voice_impl` внутри собственного `capture()` — то есть проверяют
    прибор, а не его привязку. Убери `frame_trace.start()/finish()` из `agent._voice` — и
    они останутся зелёными, а наблюдение молча исчезнет на всём боевом пути. Это ровно тот
    класс, который в этом проекте уже стоил суток: гейт сказал OK при выключенном покрытии.
    Поэтому здесь — настоящий `agent._voice`, БЕЗ своего `capture()`, и след читается прямо
    со шва, пока контекст ещё жив.
    """

    MODE = "on"

    def test_a_real_voice_pass_leaves_a_trace(self):
        seen: dict = {}

        def loop(*, system, messages, tools, max_iters=None, tool_trace=None):
            # Момент истины: seal уже отработал, finish ещё нет.
            seen["bound"] = frame_trace.current() is not None
            seen["sections"] = frame_trace.sections()
            seen["meta"] = frame_trace.metadata_for(system, call_id="model-live")
            return "ок"

        with mock.patch.object(agent, "_terminal_tool_loop", loop), \
             mock.patch.object(agent, "_create_durable_run", lambda **_kw: None):
            reply = agent._voice("Егор: привет", [], "Егор", ctx=self.owner_dm())

        self.assertEqual(reply, "ок")
        self.assertTrue(seen["bound"],
                        "след не привязан на боевом шве: frame_trace.start() снят из _voice")
        self.assertTrue(seen["sections"], "на боевом шве не записано ни одной секции")
        self.assertIsNotNone(seen["meta"], "боевая расписка осталась без следа")
        self.assertTrue(seen["meta"]["honesty"]["ok"])
        self.assertEqual(seen["meta"]["covers"],
                         ["persona", "dynamic", "evidence", "situation"])

    def test_the_trace_is_released_when_the_turn_ends(self):
        with mock.patch.object(agent, "_terminal_tool_loop",
                               lambda **_kw: "ок"), \
             mock.patch.object(agent, "_create_durable_run", lambda **_kw: None):
            agent._voice("Егор: привет", [], "Егор", ctx=self.owner_dm())
        self.assertIsNone(frame_trace.current(),
                          "след пережил ход: следующий кадр получил бы чужую разметку")


class TheEnvelopeNamesWhatItDidNotMeasure(FrameBase):
    """Сумма трёх зон — не её кадр. Схемы рук и разговор здесь не считаны, и это сказано."""

    MODE = "on"

    def test_covers_and_unmeasured_are_published(self):
        meta = self.run_turn()["meta"]
        # Пятая зона меряется наравне с тремя — назвать её неизмеренной значило бы
        # сказать, что кадр меряется не весь.
        self.assertEqual(meta["covers"], ["persona", "dynamic", "evidence", "situation"])
        self.assertEqual(meta["unmeasured"], ["messages", "tools"])
        measured = {row["zone"] for row in meta["zones"]}
        self.assertEqual(measured, set(meta["covers"]),
                         "опубликована зона, которой в covers нет — или наоборот")

    def test_the_thin_reference_carries_the_scrubber_flag(self):
        seen = self.run_turn()
        first = seen["meta"]
        second = frame_trace.metadata_for(seen["system"], call_id="model-b",
                                          receipt_scrubbed=True)
        self.assertIsNone(second, "след опечатан за пределами хода — так быть не должно")
        # Внутри хода: вторая расписка того же кадра — тонкая, но про скраббер говорит.
        with frame_trace.capture():
            inner = self.run_turn(bind=False)
            thin = frame_trace.metadata_for(inner["system"], call_id="model-c",
                                            receipt_scrubbed=True)
        self.assertTrue(thin["reused"])
        self.assertTrue(thin["receipt"]["scrubbed_possible"],
                        "тонкая ссылка молчит о том, что расписка подчищена")
        self.assertTrue(first["honesty"]["ok"])

    def test_the_budget_excludes_name_the_omitted_marker(self):
        self.assertIn("omitted_marker", frame_trace.BUDGET_EXCLUDES)


class TheEmptyConstitutionIsAnAbsenceNotAShortSection(FrameBase):
    MODE = "on"

    def test_a_blank_soul_is_declared_absent(self):
        (self.soul / "SOUL.md").write_text("", encoding="utf-8")
        seen = self.run_turn()
        row = next(r for r in seen["meta"]["sections"] if r["name"] == "persona.soul")
        self.assertFalse(row["included"])
        self.assertEqual(row["reason"], "empty")
        self.assertTrue(seen["meta"]["honesty"]["ok"],
                        "разметка разошлась с кадром: байты изменились")


class TheEnvelopeGeometryNamesItsShape(FrameBase):
    MODE = "on"

    def test_a_flat_turn_says_so_and_the_slice_works(self):
        seen = self.run_turn(extra_evidence="")
        embed = next(z for z in seen["meta"]["zones"] if z["zone"] == "evidence")["embed"]
        self.assertEqual(embed["flat_form"], 1)
        self.assertEqual(embed["fold_chars"] + embed["closing_chars"], embed["suffix_chars"])
        flat = seen["messages"][-1]["content"]
        cut = flat[embed["prefix_chars"]:embed["prefix_chars"] + embed["material_chars"]]
        self.assertTrue(cut.startswith("# My live memory"),
                        "рецепт вырезания материала по опубликованным числам не работает")

    def test_a_multimodal_turn_says_it_is_not_flat(self):
        seen = self.run_turn(user=[{"type": "image", "source": {"data": "x"}}])
        embed = next(z for z in seen["meta"]["zones"] if z["zone"] == "evidence")["embed"]
        self.assertEqual(embed["flat_form"], 0,
                         "блочный ход выдан за одну сплошную строку")
        blocks = seen["messages"][-1]["content"]
        self.assertIsInstance(blocks, list)
        head = blocks[0]["text"]
        cut = head[embed["prefix_chars"]:embed["prefix_chars"] + embed["material_chars"]]
        self.assertTrue(cut.startswith("# My live memory"))
        self.assertEqual(len(blocks[-1]["text"]), embed["closing_chars"],
                         "closing_chars не описывает последний блок")

    def test_an_empty_evidence_names_its_zero(self):
        self._patch("build_state_evidence_block", lambda **_kw: "")
        self._patch("_participant_memory_block", lambda _speaker, _ctx: "")
        seen = self.run_turn()
        zone = next((z for z in seen["meta"]["zones"] if z["zone"] == "evidence"), None)
        embed = (zone or {}).get("embed") or {}
        if zone and zone["chars"] == 0:
            self.assertEqual(embed.get("material_chars"), 0,
                             "конверта не было, и это выражено отсутствием ключей")


class TheOverflowIsNamedNotInferred(unittest.TestCase):
    def test_records_dropped_is_published(self):
        previous = frame_trace.set_mode("on")
        self.addCleanup(frame_trace.set_mode, previous)
        with frame_trace.capture() as trace:
            for index in range(frame_trace._MAX_RECORDS + 5):
                frame_trace.mark(f"x{index}", "persona", "text", "a")
            frame_trace.seal(persona="", dynamic="", evidence="",
                             system=[{"type": "text", "text": ""}])
            meta = frame_trace.metadata_for([{"type": "text", "text": ""}], call_id="c")
        self.assertTrue(trace.overflow)
        if meta is not None:
            self.assertTrue(meta["caps"]["records_dropped"],
                            "потолок записей неотличим от разъехавшейся разметки")


class TheModuleSaysWhatItCannotDo(unittest.TestCase):
    """Читатель следа не должен сделать неверный вывод — граница написана в докстринге."""

    def test_the_docstring_names_the_tool_result_prohibition_and_the_units(self):
        doc = frame_trace.__doc__ or ""
        self.assertIn("tool_result", doc)
        self.assertIn("basis", doc)
        self.assertIn("PRAXIS_CONTEXT_BUDGET", doc)
        self.assertIn("PRAXIS_FRAME_TRACE", doc)

    def test_the_default_is_on_because_the_stand_strips_praxis_variables(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRAXIS_FRAME_TRACE", None)
            frame_trace.set_mode(None)
            try:
                self.assertEqual(frame_trace.mode(), "on")
            finally:
                frame_trace.set_mode("on")
        frame_trace.set_mode(None)

    def test_the_lever_stays_out_of_the_rails_manifest_in_this_slice(self):
        """Рельса здесь НЕТ намеренно — и это железное требование среза, а не забывчивость.

        Рельс `frame_trace` в `rails.py` стоил бы ей строки в кадре: `soul/rails.md` знает
        52 заголовка, реестр стал бы знать 53, `manifest_drift()` вернул бы missing и
        `capabilities.state_line()` напечатал бы ей «манифест отстал» в КАЖДОМ owner-ходе —
        через `build_state_block`, то есть внутри той самой зоны, которую прибор измеряет.
        Наблюдатель не имеет права двигать наблюдаемое. Рычаг назван в докстринге модуля;
        рельс заводится отдельным осознанным шагом вместе с перегенерацией манифеста.
        """
        import rails
        ids = {row["id"] for row in rails.registry(with_values=False)}
        self.assertNotIn("frame_trace", ids)
        self.assertTrue(rails.manifest_drift().get("ok"),
                        "манифест рельсов разошёлся: её STATE скажет «отстал»")


if __name__ == "__main__":
    unittest.main()
