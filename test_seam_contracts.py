"""
Контрактные тесты ШВОВ (см. CONTRACTS.md).

Здесь проверяется не «работает ли функция», а «не врёт ли орган соседнему».
Правило отбора: если тест можно пройти, соврав соседу, — это не тест шва.

Красный тест в этом файле = незакрытый долг, а не поломка. Файл сознательно НЕ
входит в основной гейт, пока долг не закрыт.

Запуск:  python praxis_test.py test_seam_contracts -v
"""

from __future__ import annotations

import re
from pathlib import Path

import rails
import turns
from test_turns import TurnsBase  # noqa: F401  (герметичный харнесс + изоляция turns)

import agent  # noqa: E402
import capabilities  # noqa: E402
import notes  # noqa: E402


def _src(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8", errors="replace")


def _runner_src() -> str:
    """Исходник раннера с диска — импорт тянул бы telethon и живой .env."""
    return (Path(agent.__file__).parent / "mtproto_runner.py").read_text(
        encoding="utf-8", errors="replace")


def _block_after(source: str, anchor: str, stop: str) -> str:
    """Кусок исходника от anchor до stop — чтобы проверять конкретную ветку, а не файл."""
    i = source.index(anchor)
    j = source.index(stop, i)
    return source[i:j]


class SeamBase(TurnsBase):
    def setUp(self):
        super().setUp()
        soul = self.tmp / "soul"
        soul.mkdir(parents=True, exist_ok=True)
        rails_md = soul / "rails.md"
        rails_md.write_text("# rails (фикстура)\n", encoding="utf-8")
        for k, val in (("BASE", self.tmp), ("RAILS_MD", rails_md),
                       ("DENIALS", self.tmp / "memory" / ".state" / "denials.jsonl")):
            if hasattr(rails, k):
                self._orig.append((rails, k, getattr(rails, k)))
                setattr(rails, k, val)
        self.rails_md = rails_md

    def _ctx(self, **kw):
        base = dict(chat_id="777", principal_id="555000100", is_dm=True,
                    owner=True, known=True)
        base.update(kw)
        return agent.ChannelContext(**base)

    def _guard(self, reply, ctx=None, **turn_fields):
        ctx = ctx or self._ctx()
        turn = turns.begin(kind="chat", chat_id=ctx.chat_id, scope=ctx.scope,
                           who="Егор", gist_in="Егор: привет")
        turn.update(turn_fields)
        token = agent._TURN_CHANNEL.set(ctx)
        try:
            out = agent.guard_outbound_reply(reply, "Егор: привет", ctx=ctx, turn=turn)
        finally:
            agent._TURN_CHANNEL.reset(token)
        return out, turn


# --------------------------------------------------------------------------- #
#  C1 — правда доставки: авторство не есть речь
# --------------------------------------------------------------------------- #

class TestC1DeliveryTruth(SeamBase):

    def test_note_does_not_claim_speech_before_acceptance(self):
        """Заметка «сказала» — проекция расписки, а не намерения."""
        self._guard("Хорошо, посмотрю.")
        scratch = notes.read("777") or ""
        self.assertNotIn(
            "сказала", scratch,
            "C1: заметка утверждает речь до приёмки Telegram — "
            "неудавшаяся доставка станет для неё «уже сказанным»")

    def test_promise_is_not_armed_before_acceptance(self):
        """Обещание взводится от принятой реплики, иначе оно про несуществующий текст."""
        calls = []
        orig = agent.promises.note_outbound
        agent.promises.note_outbound = lambda *a, **kw: calls.append((a, kw))
        self.addCleanup(setattr, agent.promises, "note_outbound", orig)
        self._guard("Сейчас проверю и доложу.")
        self.assertEqual(
            calls, [],
            "C1: обещание взведено до расписки — через 15 минут она напомнит "
            "о тексте, которого никто не получил")

    def test_lived_turn_carries_delivery_state(self):
        """У прожитого хода должно быть поле исхода доставки, а не только `out`."""
        _out, turn = self._guard("Хорошо, посмотрю.")
        recorded = turns.recent(1, scope="owner", chat_id="777")
        self.assertTrue(recorded, "ход вообще не записан")
        self.assertIn(
            "delivery", recorded[-1],
            "C1: в записи хода нет исхода доставки — «сказала» неотличимо от «не ушло»")
        del turn


# --------------------------------------------------------------------------- #
#  C2/C3 — отмена и повтор
# --------------------------------------------------------------------------- #

class TestC2C3Compensation(SeamBase):

    def _authored(self, run_id="run-c2", text="Сейчас проверю и доложу."):
        """Ход, доведённый до состояния «написала», но не доставленный."""
        out, turn = self._guard(text, **{"run_id": run_id})
        turn["run_id"] = run_id
        turns.record(turn)
        return out

    def test_supersede_leaves_no_trace_of_speech(self):
        """Терминальная отмена снимает следы речи, а не только закрывает ран.

        ⚠ Этот тест был грепом по исходнику `run_delivery_superseded` на подстроки
        turns./notes./promises. — и он бы прошёл от любой строчки с этими символами и
        упал бы от невинного рефакторинга. Проверяем поведение: что видно в памяти
        после отмены.
        """
        calls = []
        orig = agent.promises.note_outbound
        agent.promises.note_outbound = lambda *a, **kw: calls.append(a)
        self.addCleanup(setattr, agent.promises, "note_outbound", orig)

        self._authored(run_id="run-c2")
        agent.project_delivery_outcome("run-c2", "superseded")

        row = turns.recent(1, scope="owner", chat_id="777")[-1]
        self.assertEqual(row.get("delivery"), "superseded",
                         "C2: ход обязан нести фактический исход, а не авторство")
        self.assertNotIn("сказала", notes.read("777") or "",
                         "C2: отменённый черновик не оставляет заметки о речи")
        self.assertEqual(calls, [], "C2: отменённый черновик не взводит обещание")

    def test_acceptance_is_what_creates_the_record_of_speech(self):
        """И симметрично: принятая доставка — создаёт. Иначе мы просто убрали память."""
        calls = []
        orig = agent.promises.note_outbound
        agent.promises.note_outbound = lambda *a, **kw: calls.append(a)
        self.addCleanup(setattr, agent.promises, "note_outbound", orig)

        self._authored(run_id="run-c1-ok", text="Сейчас проверю и доложу.")
        agent.project_delivery_outcome("run-c1-ok", "accepted",
                                       text="Сейчас проверю и доложу.")

        row = turns.recent(1, scope="owner", chat_id="777")[-1]
        self.assertEqual(row.get("delivery"), "accepted")
        self.assertIn("сказала", notes.read("777") or "",
                      "принятая доставка обязана оставить след речи")
        self.assertTrue(calls, "принятая доставка взводит обещание")

    def _stub_runs(self, status):
        """Подменить менеджер ранов ровно настолько, чтобы дойти до проверки статуса."""
        class _M:
            def manifest(self_inner, run_id):
                return {"status": status}

            def __getattr__(self_inner, _name):
                return lambda *a, **kw: None
        orig = agent._runs
        agent._runs = lambda: _M()
        self.addCleanup(setattr, agent, "_runs", orig)

    def _watch_projector(self):
        seen = []
        orig = agent.project_delivery_outcome
        agent.project_delivery_outcome = lambda rid, outcome, **kw: seen.append((rid, outcome))
        self.addCleanup(setattr, agent, "project_delivery_outcome", orig)
        return seen

    def test_supersede_routes_through_the_projector(self):
        """Отмена идёт через проектор — но ПОСЛЕ проверки статуса, а не до неё.

        ⚠ Тест раньше просто ловил факт вызова, и это закрепляло дефект: проекция
        стояла первой строкой функции и штамповала ход даже тогда, когда функция
        ничего не делала. Проверяем обе стороны условия."""
        seen = self._watch_projector()
        self._stub_runs("running")
        try:
            agent.run_delivery_superseded("run-live", reason="тест")
        except Exception:
            pass
        self.assertIn(("run-live", "superseded"), seen,
                      "живой ран обязан получить исход")

    def test_terminal_run_is_not_restamped(self):
        seen = self._watch_projector()
        self._stub_runs("done")
        try:
            agent.run_delivery_superseded("run-done", reason="тест")
        except Exception:
            pass
        self.assertEqual(seen, [],
                         "уже завершённый ран не имеет права переписывать память хода")

    def test_delivery_outcome_never_goes_backwards(self):
        """Приёмка Telegram — факт. Поздняя слабая расписка не отменяет её."""
        self._authored(run_id="run-lattice")
        agent.project_delivery_outcome("run-lattice", "accepted", text="ушло")
        for weaker in ("blocked", "failed", "superseded", "authored"):
            agent.project_delivery_outcome("run-lattice", weaker)
            row = turns.recent(1, scope="owner", chat_id="777")[-1]
            self.assertEqual(row.get("delivery"), "accepted",
                             f"{weaker} откатил принятую доставку")

    def test_turn_keeps_run_id(self):
        """Без run_id на ходе спроецировать доставку в память нечем."""
        t = turns.begin(kind="chat", chat_id="777", scope="owner", who="Егор")
        t["out"] = "ответ"
        t["run_id"] = "run-contract-1"
        turns.record(t)
        got = turns.recent(1, scope="owner", chat_id="777")[-1]
        self.assertEqual(
            got.get("run_id"), "run-contract-1",
            "C3: turns._clip пересобирает запись по белому списку полей и роняет run_id — "
            "любая починка, которая просто проставит run_id, тихо не доедет до диска")


# --------------------------------------------------------------------------- #
#  A1/A2 — самоотчёт
# --------------------------------------------------------------------------- #

class TestA1A2SelfReport(SeamBase):

    def test_capabilities_do_not_deny_hands_offered_this_turn(self):
        """В owner-адресованном ходе в группе руки предложены — значит они есть."""
        ctx = self._ctx(is_dm=False, chat_id="-1001240718803")
        token = agent._TURN_CHANNEL.set(ctx)
        try:
            text = agent.tool_my_capabilities()
        finally:
            agent._TURN_CHANNEL.reset(token)
        self.assertNotIn(
            "нет произвольного shell/Forge/root", text,
            "A1: самоотчёт описывает аудиторию как её руки — в этом же ходе "
            "shell/Forge/host_ctl были предложены модели")

    def test_capabilities_is_read_only(self):
        """Наблюдение не меняет наблюдаемое."""
        before = self.rails_md.read_bytes()
        token = agent._TURN_CHANNEL.set(self._ctx())
        try:
            agent.tool_my_capabilities()
        finally:
            agent._TURN_CHANNEL.reset(token)
        self.assertEqual(
            before, self.rails_md.read_bytes(),
            "A2: интроспекция переписала tracked soul/rails.md — "
            "вопрос «что я умею» порождает коммит в исходник")

    def test_capabilities_scope_line_names_audience_not_hands(self):
        """Про аудиторию говорить можно; формулировка не должна отрицать способность."""
        self.assertNotIn(
            "в самом ходе нет", capabilities.describe("group"),
            "A1: «в самом ходе нет …» — проверяемое утверждение о ране, и оно ложно")


# --------------------------------------------------------------------------- #
#  T1 — транспорт описан как есть
# --------------------------------------------------------------------------- #

class TestT1TransportHonesty(SeamBase):

    def test_task_window_frame_does_not_claim_telegram_online(self):
        self.assertNotIn(
            "Telegram remains online", agent._TASK_WINDOW_FRAME,
            "T1: окно намеренно отключает Telethon (mtproto_runner._task_window), "
            "а фрейм обещает связь")

    def test_coding_window_seed_does_not_claim_telegram_alive(self):
        self.assertFalse(
            "Telegram жив]" in _src(agent),
            "T1: seed coding-окна говорит «Telegram жив», а _CODING_WINDOW_FRAME "
            "в том же промпте — «Telegram закрыт»")


# --------------------------------------------------------------------------- #
#  M1 — архив не подтвердил, кольцо не режем
# --------------------------------------------------------------------------- #

class TestM1ArchiveFailRetain(SeamBase):

    def test_ring_is_not_compacted_when_archive_fails(self):
        self._orig.append((turns, "KEEP", turns.KEEP))
        turns.KEEP = 3
        turns._reset()

        # Отказ инжектируем ВНУТРИ архива, а не поверх него: _archive глотает свои
        # исключения сам (turns.py:231), возвращает управление как ни в чём не бывало,
        # и только потом _compact_file переписывает кольцо. Подмена самой функции
        # проверяла бы не тот путь — она бы всплыла в record() и компакт бы не случился.
        blocker = self.tmp / "memory" / "self"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("это файл, а не каталог — mkdir внутри _archive упадёт",
                           encoding="utf-8")
        self._orig.append((turns, "ARCHIVE_DIR", turns.ARCHIVE_DIR))
        turns.ARCHIVE_DIR = blocker

        for i in range(9):
            t = turns.begin(kind="chat", chat_id="777", scope="owner", who="Егор")
            t["out"] = f"реплика-{i}"
            turns.record(t)

        on_disk = turns.PATH.read_text(encoding="utf-8") if turns.PATH.exists() else ""
        missing = [i for i in range(9) if f"реплика-{i}" not in on_disk]
        self.assertEqual(
            missing, [],
            f"M1: архив не записался, а кольцо всё равно обрезано — потеряны {missing}. "
            "Её собственные ходы исчезли отовсюду")


# --------------------------------------------------------------------------- #
#  R1/R2 — манифест ограничений
# --------------------------------------------------------------------------- #

class TestR1R2Manifest(SeamBase):

    def test_sleep_window_gate_leaves_a_trace(self):
        """Гейт может существовать. Молча — нет."""
        # Читаем исходник с диска, а не импортируем: mtproto_runner тянет telethon
        # и живой .env, и контрактному тесту это не нужно.
        block = _block_after(_runner_src(), "async def _social_pulse_once",
                             "pulse_id = await")
        self.assertTrue(
            any(w in block for w in ("note_skip", "record_decision", "defer(", "log.")),
            "R1: окно сна глушит её единственное живое автономное пробуждение голым "
            "return — ни лога, ни skip, ни receipt; при этом rails.md заявляет "
            "«поведенческих гейтов нет»")

    def test_every_denied_rail_exists_in_registry(self):
        known = set()
        for row in rails.registry():
            if isinstance(row, dict):
                known.add(str(row.get("id") or row.get("key") or ""))
        used = set()
        for path in sorted(Path(agent.__file__).parent.glob("*.py")):
            if path.name.startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            used.update(re.findall(r'rails\.deny\(\s*"([a-z_]+)"', text))
        orphan = sorted(used - known)
        self.assertEqual(
            orphan, [],
            f"R2: denial ссылается на рельсы, которых нет в реестре: {orphan}. "
            "Она получает отказ по правилу, которого нет в её манифесте")

    def test_manifest_does_not_deny_an_existing_gate(self):
        gate_exists = "_in_sleep_window()" in _runner_src()
        text = "\n".join(str(r) for r in rails.registry())
        claims_none = "поведенческих гейтов нет" in text
        self.assertFalse(
            gate_exists and claims_none,
            "R2: манифест утверждает отсутствие гейта, который в коде есть")
