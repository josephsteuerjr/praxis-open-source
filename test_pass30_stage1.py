"""PASS 30 Этап 1 — сердце: события будят её (швы Forge/agent/runner/selfgit).

Проверяемые контракты:
- завершение/падение воркера рождает durable-событие (эмит из процесса-писателя,
  реконсайлер фабрикует то, что некому записать: lost/overdue);
- доставка будит ОДИН ход под _ONE_MIND, mark-on-open (замок → пометка → модель);
- её собственный stop не будит (расписка уже в её ходе);
- страховочный снапшот шелла живёт ВНЕ ветки — master чист.

Запуск:  python praxis_test.py test_pass30_stage1 -v
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# mtproto_runner конструирует TelegramClient на импорте — фиктивные креды и temp-сессия.
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault("TELEGRAM_SESSION", str(Path(tempfile.gettempdir()) / "praxis_test_runner"))

import agent  # noqa: E402
import forge  # noqa: E402
import mtproto_runner  # noqa: E402
import selfgit  # noqa: E402
import turns  # noqa: E402
from core import events as core_events  # noqa: E402
from core import subagents as core_subagents  # noqa: E402


def _now_iso(minutes_ago: float = 0.0) -> str:
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    return t.isoformat()


class ForgeEventsBase(unittest.TestCase):
    """Изолированные TASKS_DIR/wake_seen + журнал core_events во временной папке."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p30s1_"))
        self._orig = {
            "tasks_dir": forge.TASKS_DIR, "seen": forge._WAKE_SEEN,
            "journal": core_events.JOURNAL, "delivered": core_events.DELIVERED,
            "recon_ts": dict(forge._RECON_STATE),
        }
        forge.TASKS_DIR = self.tmp / "tasks"
        forge._WAKE_SEEN = self.tmp / "wake_seen.json"
        core_events.JOURNAL = self.tmp / "core_events.jsonl"
        core_events.DELIVERED = self.tmp / "core_events_delivered.json"
        forge.TASKS_DIR.mkdir(parents=True, exist_ok=True)
        forge._RECON_STATE["ts"] = 0.0

    def tearDown(self):
        forge.TASKS_DIR = self._orig["tasks_dir"]
        forge._WAKE_SEEN = self._orig["seen"]
        core_events.JOURNAL = self._orig["journal"]
        core_events.DELIVERED = self._orig["delivered"]
        forge._RECON_STATE.update(self._orig["recon_ts"])

    def _unit(self, task_id, agent_id, *, result=None, request=None, priority="normal"):
        d = forge.TASKS_DIR / task_id / "agents" / agent_id
        d.mkdir(parents=True, exist_ok=True)
        (forge.TASKS_DIR / task_id / "task.json").write_text(
            json.dumps({"id": task_id, "goal": "цель", "priority": priority,
                        "status": "active"}), encoding="utf-8")
        if request is not None:
            (d / "request.json").write_text(json.dumps(request), encoding="utf-8")
        if result is not None:
            (d / "result.json").write_text(json.dumps(result), encoding="utf-8")
        return d


class EmitSeamTests(ForgeEventsBase):
    def test_emit_unit_event_writes_contract_payload(self):
        self._unit("code-1", "agent-a1", priority="urgent")
        forge.emit_unit_event("code-1", "agent-a1",
                              {"status": "done", "finished": _now_iso(), "role": "worker",
                               "result": "готово", "tool_calls": 3},
                              request={"brief": "б", "created": _now_iso(9)})
        pend = core_events.undelivered({"subagent_result"})
        self.assertEqual(len(pend), 1)
        p = pend[0]["payload"]
        self.assertEqual(p["schema"], core_subagents.SCHEMA)
        self.assertEqual(p["status"], "succeeded")
        self.assertEqual(p["priority"], "urgent")
        self.assertEqual(pend[0]["dedup_key"], "forge:code-1:agent-a1")

    def test_emit_never_raises(self):
        core_events.JOURNAL = self.tmp / "task.jsonl-как-файл" / "x" / "j.jsonl"
        (self.tmp / "task.jsonl-как-файл").write_text("f", encoding="utf-8")
        forge.emit_unit_event("t", "a", {"status": "done"})  # не бросает


class ReconcileTests(ForgeEventsBase):
    def test_terminal_result_without_event_is_fabricated_once(self):
        self._unit("code-2", "agent-b2",
                   request={"id": "agent-b2", "role": "worker", "created": _now_iso(30),
                            "supervisor_pid": 1},
                   result={"status": "error", "finished": _now_iso(1), "error": "упал"})
        n = forge.reconcile_subagent_events(force=True)
        self.assertEqual(n, 1)
        self.assertEqual(forge.reconcile_subagent_events(force=True), 0, "идемпотентно")
        p = core_events.undelivered()[0]["payload"]
        self.assertEqual(p["status"], "failed")

    def test_no_freshness_cliff_within_backfill(self):
        """Завершение при даунтайме >6ч (старый обрыв) но в окне бэкфилла — будит."""
        self._unit("code-old", "agent-o1",
                   request={"id": "agent-o1", "role": "worker",
                            "created": _now_iso(60 * 20), "supervisor_pid": 1},
                   result={"status": "done", "finished": _now_iso(60 * 10),
                           "result": "давно готово"})
        self.assertEqual(forge.reconcile_subagent_events(force=True), 1)

    def test_ancient_unit_archived_with_receipt_not_flood(self):
        """Деплой-день: древнее окна бэкфилла НЕ фабрикуется (потоп), а импортируется
        в wake_seen — запись о пропуске, не тишина."""
        self._unit("code-anc", "agent-an",
                   request={"id": "agent-an", "role": "worker",
                            "created": _now_iso(60 * 60), "supervisor_pid": 99},
                   result={"status": "done", "finished": _now_iso(60 * 40),
                           "result": "древность"})
        # и древний мёртвый юнит без result — тоже архив, не ложный lost
        self._unit("code-anc2", "agent-an2",
                   request={"id": "agent-an2", "role": "worker", "status": "running",
                            "created": _now_iso(60 * 60), "supervisor_pid": 424242})
        with mock.patch.object(forge, "_pid_alive", return_value=False):
            self.assertEqual(forge.reconcile_subagent_events(force=True), 0)
        seen = json.loads(forge._WAKE_SEEN.read_text(encoding="utf-8"))
        self.assertIn("code-anc:agent-an", seen)
        self.assertIn("code-anc2:agent-an2", seen)

    def test_wake_seen_import_no_rewake_of_history(self):
        self._unit("code-3", "agent-c3",
                   request={"id": "agent-c3", "role": "worker", "created": _now_iso(60)},
                   result={"status": "done", "finished": _now_iso(50), "result": "старое"})
        forge.mark_seen(["code-3:agent-c3"])  # старый путь уже показал
        self.assertEqual(forge.reconcile_subagent_events(force=True), 0,
                         "показанное старым wake-путём не будит заново")

    def test_dead_supervisor_without_result_becomes_failed_event(self):
        self._unit("code-4", "agent-d4",
                   request={"id": "agent-d4", "role": "worker", "status": "running",
                            "created": _now_iso(30), "supervisor_pid": 4194000})
        with mock.patch.object(forge, "_pid_alive", return_value=False):
            self.assertEqual(forge.reconcile_subagent_events(force=True), 1)
            self.assertEqual(forge.reconcile_subagent_events(force=True), 0)
        p = core_events.undelivered()[0]["payload"]
        self.assertEqual(p["status"], "failed")
        self.assertIn("supervisor", p["error"])

    def test_spawn_grace_no_false_death(self):
        self._unit("code-5", "agent-e5",
                   request={"id": "agent-e5", "role": "worker", "status": "starting",
                            "created": _now_iso(0)})
        with mock.patch.object(forge, "_pid_alive", return_value=False):
            self.assertEqual(forge.reconcile_subagent_events(force=True), 0,
                             "шов спавна (starting без pid) — не падение")

    def test_alive_overdue_worker_gets_one_timeout_signal(self):
        self._unit("code-6", "agent-f6",
                   request={"id": "agent-f6", "role": "worker", "status": "running",
                            "created": _now_iso(60 * 3), "supervisor_pid": 12345})
        with mock.patch.object(forge, "_pid_alive", return_value=True):
            self.assertEqual(forge.reconcile_subagent_events(force=True), 1)
            self.assertEqual(forge.reconcile_subagent_events(force=True), 0, "сигнал один раз")
        e = core_events.undelivered()[0]
        self.assertEqual(e["dedup_key"], "forge:code-6:agent-f6:overdue")
        self.assertEqual(e["payload"]["status"], "timeout")

    def test_scout_completion_also_reconciled(self):
        """23.07: реконсайлер не фильтрует роль — плод разведчика тоже её событие."""
        self._unit("code-7", "agent-g7",
                   request={"id": "agent-g7", "role": "scout", "created": _now_iso(30)},
                   result={"status": "done", "finished": _now_iso(1)})
        self.assertEqual(forge.reconcile_subagent_events(force=True), 1)

    def test_rate_limit_without_force(self):
        forge._RECON_STATE["ts"] = 0.0
        self._unit("code-8", "agent-h8",
                   request={"id": "agent-h8", "role": "worker", "created": _now_iso(30)},
                   result={"status": "done", "finished": _now_iso(1)})
        self.assertEqual(forge.reconcile_subagent_events(), 1)
        self._unit("code-8b", "agent-h9",
                   request={"id": "agent-h9", "role": "worker", "created": _now_iso(30)},
                   result={"status": "done", "finished": _now_iso(1)})
        self.assertEqual(forge.reconcile_subagent_events(), 0, "рейт-лимит 60с без force")


class ForgeEventTurnTests(ForgeEventsBase):
    """agent.forge_event_turn: ход с kind=forge_event, durable run СУЩЕСТВУЮЩЕГО класса."""

    def _events(self):
        p = core_subagents.normalize("code-1", "agent-a1",
                                     {"status": "done", "result": "готово"},
                                     task={"goal": "цель", "priority": "urgent"})
        return [{"kind": "subagent_result", "payload": p, "dedup_key": "forge:code-1:agent-a1"}]

    def test_turn_records_forge_event_kind_and_reuses_run_class(self):
        recorded = []
        run_kinds = []

        def fake_create(ctx=None, kind="", goal="", conversation="", extra=None, **kw):
            run_kinds.append(kind)
            return None  # без durable-спайна: проверяем шов, не run_manager

        with mock.patch.object(agent, "_voice", return_value="посмотрела, приняла") as v, \
             mock.patch.object(agent, "_create_durable_run", side_effect=fake_create), \
             mock.patch.object(agent.turns, "record", side_effect=recorded.append), \
             mock.patch.object(agent.llm, "configured", return_value=True):
            out = agent.forge_event_turn(self._events())
        self.assertEqual(out, "посмотрела, приняла")
        self.assertEqual(run_kinds, ["task_window"],
                         "Event-пробуждение НЕ создаёт новый класс run (закон плана)")
        self.assertEqual(recorded[0]["kind"], "forge_event")
        seed = v.call_args.args[0]
        self.assertIn("code-1", seed)
        self.assertIn("agent-a1", seed)
        self.assertIn("subagent-result.v1", seed)

    def test_error_path_records_receipt_and_reraises(self):
        """Расписка held=error + ПРОБРОС: упавший ход — не доставка (иначе транзиентный
        401 съедал бы завершения навсегда); повтор ограничен капом попыток."""
        recorded = []
        with mock.patch.object(agent, "_voice", side_effect=RuntimeError("мозг лёг")), \
             mock.patch.object(agent, "_create_durable_run", return_value=None), \
             mock.patch.object(agent.turns, "record", side_effect=recorded.append), \
             mock.patch.object(agent.llm, "configured", return_value=True):
            with self.assertRaises(RuntimeError):
                agent.forge_event_turn(self._events())
        self.assertEqual(recorded[0]["held"], "error")

    def test_empty_events_noop(self):
        self.assertEqual(agent.forge_event_turn([]), "")

    def test_spine_failure_leaves_receipt_and_reraises(self):
        """Реальный контракт спайна — fail-closed raise (не None): отказ
        _create_durable_run обязан оставить расписку и пробросить (не тихая потеря)."""
        recorded = []
        with mock.patch.object(agent, "_create_durable_run",
                               side_effect=RuntimeError("spine down")), \
             mock.patch.object(agent, "_voice") as voice, \
             mock.patch.object(agent.turns, "record", side_effect=recorded.append), \
             mock.patch.object(agent.llm, "configured", return_value=True):
            with self.assertRaises(RuntimeError):
                agent.forge_event_turn(self._events())
        voice.assert_not_called()
        self.assertEqual(recorded[0]["held"], "error",
                         "расписка в прожитых ходах есть даже при мёртвом спайне")

    def test_format_line_honest_about_transport(self):
        line = turns.format_line({"ts": 1.0, "kind": "forge_event", "in": "x"})
        self.assertIn("событием своего субагента", line)


class RunnerPumpTests(ForgeEventsBase):
    """_run_forge_event_pass: мозг жив? → счёт попыток → ход → пометка; трайлок."""

    def setUp(self):
        super().setUp()
        mtproto_runner._FORGE_EVENT_LAST.clear()
        mtproto_runner._FORGE_EVENT_LAST["ts"] = 0.0
        mtproto_runner._FORGE_EVENTS_TASK = None
        self._brain = mock.patch.object(mtproto_runner.agent.llm, "configured",
                                        return_value=True)
        self._brain.start()
        self.addCleanup(self._brain.stop)

    def _emit(self, task_id, agent_id, status="done", cancelled_by=""):
        p = core_subagents.normalize(task_id, agent_id, {"status": status})
        if cancelled_by:
            p["causality"]["cancelled_by"] = cancelled_by
        core_events.emit("subagent_result", "forge", p,
                         dedup_key=core_subagents.event_key(task_id, agent_id))

    def test_delivery_marks_after_turn_wakes_once_coalesced(self):
        self._emit("t1", "a1", "done")
        self._emit("t2", "a2", "error")
        woken = []
        with mock.patch.object(mtproto_runner.agent, "forge_event_turn",
                               side_effect=lambda evs: woken.append(evs) or ""):
            asyncio.run(mtproto_runner._run_forge_event_pass())
        self.assertEqual(len(woken), 1, "коалесценция: один ход на всё")
        self.assertEqual(len(woken[0]), 2)
        self.assertEqual(core_events.undelivered(), [], "доставлено и помечено ПОСЛЕ хода")
        seen = json.loads(forge._WAKE_SEEN.read_text(encoding="utf-8"))
        self.assertIn("t1:a1", seen, "мост к часовому контексту (не покажет второй раз)")

    def test_dead_brain_consumes_nothing(self):
        """P1 скептиков: пометка до хода при мёртвом мозге съедала завершение навсегда."""
        self._emit("tb", "ab", "done")
        with mock.patch.object(mtproto_runner.agent.llm, "configured",
                               return_value=False), \
             mock.patch.object(mtproto_runner.agent, "forge_event_turn") as turn:
            asyncio.run(mtproto_runner._run_forge_event_pass())
        turn.assert_not_called()
        self.assertEqual(len(core_events.undelivered()), 1, "событие ЖДЁТ мозга, не съедено")
        self.assertEqual(core_events._load_state()["attempts"], {},
                         "попытки не сжигаются, пока мозг лежит")

    def test_crash_mid_turn_redelivers_then_poison_caps_loudly(self):
        self._emit("tc", "ac", "done")
        key = core_subagents.event_key("tc", "ac")
        # попытка 1 и 2: процесс «крашится» посреди хода (to_thread пробрасывает)
        for expected_attempt in (1, 2):
            with mock.patch.object(mtproto_runner.agent, "forge_event_turn",
                                   side_effect=RuntimeError("crash")):
                asyncio.run(mtproto_runner._run_forge_event_pass())
            self.assertEqual(len(core_events.undelivered()), 1,
                             "после краша событие снова ждёт доставки")
            self.assertEqual(core_events._load_state()["attempts"][key], expected_attempt)
        # попытка 3: ядовитый кап — гасится БЕЗ хода, но помечено (не вечная петля)
        with mock.patch.object(mtproto_runner.agent, "forge_event_turn") as turn:
            asyncio.run(mtproto_runner._run_forge_event_pass())
        turn.assert_not_called()
        self.assertEqual(core_events.undelivered(), [],
                         "ядовитое событие погашено, руминация-петля не вернулась")

    def test_own_stop_is_silent(self):
        self._emit("t3", "a3", "stopped", cancelled_by="praxis")
        woken = []
        with mock.patch.object(mtproto_runner.agent, "forge_event_turn",
                               side_effect=lambda evs: woken.append(evs) or ""):
            asyncio.run(mtproto_runner._run_forge_event_pass())
        self.assertEqual(woken, [], "её собственный stop не будит — расписка уже в её ходе")
        self.assertEqual(core_events.undelivered(), [], "но событие доставлено (не зомби)")

    def test_transient_turn_failure_keeps_loud_delivers_quiet(self):
        """Ход упал (например 401): loud остаются к повторной доставке, quiet уходят."""
        self._emit("tf", "af", "done")
        self._emit("tf2", "af2", "stopped", cancelled_by="praxis")
        with mock.patch.object(mtproto_runner.agent, "forge_event_turn",
                               side_effect=RuntimeError("401")):
            asyncio.run(mtproto_runner._run_forge_event_pass())
        pend = core_events.undelivered()
        self.assertEqual([p["dedup_key"] for p in pend], ["forge:tf:af"],
                         "упавший ход ≠ доставка; тихое доставлено")
        # вторая попытка с живым мозгом — доезжает
        mtproto_runner._FORGE_EVENT_LAST["ts"] = 0.0
        with mock.patch.object(mtproto_runner.agent, "forge_event_turn",
                               return_value="") as turn:
            asyncio.run(mtproto_runner._run_forge_event_pass())
        turn.assert_called_once()
        self.assertEqual(core_events.undelivered(), [])

    def test_reviewer_and_scout_completions_wake_her(self):
        """23.07 (диагностика простоя): роль НЕ глушит пробуждение. Её конвейер —
        worker→reviewer→worker→reviewer; молчание после ревьюера стоило 4-6ч на шаг."""
        for role, unit in (("reviewer", "ar"), ("scout", "as")):
            p = core_subagents.normalize("ts", unit, {"status": "done", "role": role})
            core_events.emit("subagent_result", "forge", p,
                             dedup_key=core_subagents.event_key("ts", unit))
        woken = []
        with mock.patch.object(mtproto_runner.agent, "forge_event_turn",
                               side_effect=lambda evs: woken.append(evs) or ""):
            asyncio.run(mtproto_runner._run_forge_event_pass())
        self.assertEqual(len(woken), 1, "одним коалесцированным ходом")
        roles = {(e.get("payload") or {}).get("role") for e in woken[0]}
        self.assertEqual(roles, {"reviewer", "scout"})
        self.assertEqual(core_events.undelivered(), [])

    def test_failed_reviewer_is_not_silence(self):
        """Упавший ревьюер (RemoteProtocolError 23.07 02:09) — тоже событие."""
        self._unit("tf", "arv",
                   request={"id": "arv", "role": "reviewer", "created": _now_iso(30),
                            "supervisor_pid": 1},
                   result={"status": "error", "finished": _now_iso(1),
                           "error": "RemoteProtocolError: peer closed connection"})
        self.assertEqual(forge.reconcile_subagent_events(force=True), 1)
        p = core_events.undelivered()[0]["payload"]
        self.assertEqual((p["status"], p["role"]), ("failed", "reviewer"))

    def test_rollback_return_no_avalanche(self):
        """Показанное старым путём за время отката env не будит второй раз при возврате."""
        self._emit("tr", "ar", "done")
        forge.mark_seen(["tr:ar"])  # старый путь уже показал, пока контур был выключен
        with mock.patch.object(mtproto_runner.agent, "forge_event_turn") as turn:
            asyncio.run(mtproto_runner._run_forge_event_pass())
        turn.assert_not_called()
        self.assertEqual(core_events.undelivered(), [], "тихо доставлено, не лавина")

    def test_overdue_signal_does_not_bury_future_completion(self):
        """Мост для overdue пишет СУФФИКСНЫЙ ключ: реальное завершение юнита не
        похоронено для старого пути, а повторный overdue после компакта журнала
        не воскресает (durable-гвард)."""
        p = core_subagents.normalize("to", "ao", {"status": "timed_out",
                                                  "error": "долго"})
        core_events.emit("subagent_result", "forge", p,
                         dedup_key=core_subagents.event_key("to", "ao", "overdue"))
        with mock.patch.object(mtproto_runner.agent, "forge_event_turn", return_value=""):
            asyncio.run(mtproto_runner._run_forge_event_pass())
        self.assertEqual(core_events.undelivered(), [])
        seen = json.loads(forge._WAKE_SEEN.read_text(encoding="utf-8"))
        self.assertNotIn("to:ao", seen, "базовый ключ не тронут")
        self.assertIn("to:ao:overdue", seen, "durable-гвард повторного сигнала")
        # компакт «забыл» журнал — реконсайлер всё равно не сигналит повторно
        core_events.JOURNAL.unlink()
        self._unit("to", "ao",
                   request={"id": "ao", "role": "worker", "status": "running",
                            "created": _now_iso(60 * 3), "supervisor_pid": 777})
        with mock.patch.object(forge, "_pid_alive", return_value=True):
            self.assertEqual(forge.reconcile_subagent_events(force=True), 0,
                             "overdue не воскресает после компакта")

    def test_live_dialog_wins_over_fruit(self):
        """Человек в дебаунсе — его ход первее; события durable, подождут."""
        self._emit("td", "ad", "done")

        async def scenario():
            async def hold():
                await asyncio.sleep(3600)
            t = asyncio.get_running_loop().create_task(hold())
            mtproto_runner._debounce["chat-x"] = t
            try:
                await mtproto_runner._forge_events_once()
            finally:
                t.cancel()
                mtproto_runner._debounce.pop("chat-x", None)

        with mock.patch.object(mtproto_runner.agent, "forge_event_turn") as turn:
            asyncio.run(scenario())
        turn.assert_not_called()
        self.assertEqual(len(core_events.undelivered()), 1)

    def test_busy_one_mind_consumes_nothing(self):
        self._emit("t4", "a4", "done")

        async def run_locked():
            async with mtproto_runner._ONE_MIND:
                await mtproto_runner._run_forge_event_pass()

        with mock.patch.object(mtproto_runner.agent, "forge_event_turn") as turn:
            asyncio.run(run_locked())
        turn.assert_not_called()
        self.assertEqual(len(core_events.undelivered()), 1,
                         "занята живым ходом: событие ждёт следующего тика, не съедено")

    def test_kill_switch_leaves_events_for_old_path(self):
        self._emit("t5", "a5", "done")
        with mock.patch.dict(os.environ, {"PRAXIS_FORGE_EVENTS": "0"}), \
             mock.patch.object(mtproto_runner.agent, "forge_event_turn") as turn, \
             mock.patch.object(forge, "reconcile_subagent_events") as recon:
            asyncio.run(mtproto_runner._forge_events_once())
        turn.assert_not_called()
        recon.assert_not_called()

    def test_gap_knob_defers_delivery(self):
        self._emit("t6", "a6", "done")
        mtproto_runner._FORGE_EVENT_LAST["ts"] = mtproto_runner.time.time()
        with mock.patch.object(mtproto_runner.agent, "forge_event_turn") as turn:
            asyncio.run(mtproto_runner._forge_events_once())
        turn.assert_not_called()
        self.assertEqual(len(core_events.undelivered()), 1, "зазор: подождёт, не потеряется")

    def test_hourly_fallback_sleeps_while_events_enabled(self):
        """Гонка двойного показа: часовой контекст не потребляет завершения, пока
        событийный контур жив — иначе одно завершение всплывёт прозой И ходом."""
        import heartbeat
        self._unit("code-h", "agent-hh",
                   request={"id": "agent-hh", "role": "worker", "created": _now_iso(5)},
                   result={"status": "done", "finished": _now_iso(1), "role": "worker",
                           "result": "готово"})
        self.assertEqual(heartbeat._forge_completions_context(), "")
        self.assertFalse(forge._WAKE_SEEN.exists(), "и НЕ потребил их")
        with mock.patch.dict(os.environ, {"PRAXIS_FORGE_EVENTS": "0"}):
            text = heartbeat._forge_completions_context()
        self.assertIn("code-h", text, "фолбэк жив при выключенном контуре")

    def test_mtime_guard_skips_parse_on_quiet_journal(self):
        self._emit("t7", "a7", "done")
        with mock.patch.object(mtproto_runner.agent, "forge_event_turn", return_value=""):
            asyncio.run(mtproto_runner._run_forge_event_pass())      # доставлено
            asyncio.run(mtproto_runner._forge_events_once())          # пусто -> запомнил mtime
        with mock.patch.object(core_events, "undelivered") as und:
            asyncio.run(mtproto_runner._forge_events_once())
        und.assert_not_called()

    def test_old_urgent_poller_sleeps_while_events_enabled(self):
        self._unit("code-u", "agent-u1", priority="urgent",
                   request={"id": "agent-u1", "role": "worker", "created": _now_iso(5)},
                   result={"status": "done", "finished": _now_iso(1), "role": "worker",
                           "result": "готово"})
        with mock.patch.object(mtproto_runner, "tasks", create=True) as t:
            asyncio.run(mtproto_runner._forge_wake_once())
        self.assertFalse(forge._WAKE_SEEN.exists(),
                         "старый поллер спит при живом событийном контуре")


class ZombieLivenessTests(unittest.TestCase):
    """Живой смок №2 поймал дыру: осиротевший супервизор после смерти висит зомби
    под чужим PID 1 (рестарт раннера!), kill(pid,0) проходит — «running» навсегда,
    lost-событие не рождается. Зомби — НЕ живой."""

    @unittest.skipIf(os.name == "nt", "зомби — POSIX-сущность")
    def test_zombie_is_dead(self):
        import subprocess
        import sys
        import time as _t
        import process_liveness
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            for _ in range(100):  # ждём выхода ребёнка, НЕ реапя его (без poll/wait)
                try:
                    state = Path(f"/proc/{proc.pid}/stat").read_bytes().rsplit(b")", 1)[-1]
                    if state.strip()[:1] == b"Z":
                        break
                except OSError:
                    break
                _t.sleep(0.05)
            self.assertFalse(process_liveness.is_process_alive(proc.pid),
                             "зомби должен считаться мёртвым (lost-событие обязано родиться)")
        finally:
            proc.wait()  # реапнуть за собой

    def test_self_is_alive(self):
        import process_liveness
        self.assertTrue(process_liveness.is_process_alive(os.getpid()))


class SafetyPointTests(unittest.TestCase):
    """selfgit.safety_point: страховка ВНЕ ветки, master чист, индекс не тронут."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p30_git_"))
        self._orig_base = selfgit.BASE
        selfgit.BASE = self.tmp
        self._git("init", "-q")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        (self.tmp / "a.py").write_text("x = 1\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "init")

    def tearDown(self):
        selfgit.BASE = self._orig_base

    def _git(self, *args):
        return subprocess.run(["git", "-C", str(self.tmp), *args],
                              capture_output=True, text=True)

    def _head(self):
        return self._git("rev-parse", "HEAD").stdout.strip()

    def test_master_stays_clean_snapshot_recoverable(self):
        before = self._head()
        (self.tmp / "a.py").write_text("x = 2\n", encoding="utf-8")
        (self.tmp / "new_untracked.py").write_text("y = 3\n", encoding="utf-8")
        sha = selfgit.safety_point("safety before shell")
        self.assertIsNotNone(sha)
        self.assertEqual(self._head(), before, "master НЕ получил коммит")
        self.assertEqual(self._git("diff", "--cached", "--name-only").stdout.strip(), "",
                         "настоящий индекс не тронут (её staged-выбор цел)")
        self.assertEqual((self.tmp / "a.py").read_text(encoding="utf-8"), "x = 2\n",
                         "рабочее дерево не тронуто")
        points = selfgit.list_safety_points()
        self.assertEqual(len(points), 1)
        # восстановимость: untracked-файл живёт в снапшот-коммите
        show = self._git("show", f"{sha}:new_untracked.py")
        self.assertEqual(show.stdout, "y = 3\n")

    def test_clean_tree_no_snapshot(self):
        self.assertIsNone(selfgit.safety_point("nothing"))
        self.assertEqual(selfgit.list_safety_points(), [])

    def test_prune_keeps_last_n(self):
        for i in range(3):
            (self.tmp / "a.py").write_text(f"x = {i + 10}\n", encoding="utf-8")
            self.assertIsNotNone(selfgit.safety_point(f"s{i}"))
        selfgit._prune_snapshot_refs(keep=1)
        self.assertEqual(len(selfgit.list_safety_points()), 1)

    def test_no_repo_is_quiet(self):
        selfgit.BASE = self.tmp / "нет-репо"
        self.assertIsNone(selfgit.safety_point("x"))

    def test_snapshot_paths_leaves_foreign_dirt_alone(self):
        """Атрибуция: pathspec-коммит берёт только названное; чужая грязь лежит как лежала."""
        (self.tmp / "foreign.py").write_text("deploy = 1\n", encoding="utf-8")  # «чужое»
        (self.tmp / "hers.py").write_text("mine = 1\n", encoding="utf-8")       # «её»
        sha = selfgit.snapshot_paths(["hers.py"], "self-edit: hers.py")
        self.assertIsNotNone(sha)
        show = self._git("show", "--stat", "--name-only", "HEAD")
        self.assertIn("hers.py", show.stdout)
        self.assertNotIn("foreign.py", show.stdout, "чужая грязь НЕ заметена в её коммит")
        status = self._git("status", "--porcelain").stdout
        self.assertIn("foreign.py", status, "чужой файл остался незакоммиченным")

    def test_snapshot_paths_empty_noop(self):
        self.assertIsNone(selfgit.snapshot_paths([], "x"))

    def test_autocommit_pre_dirt_skips_foreign_dirt_and_foreign_deletion(self):
        """Дверь судьи-9: fs_write/fs_edit-путь тоже не приписывает ей чужую грязь;
        чужое УДАЛЕНИЕ (файла не было уже до команды) — тоже не её."""
        (self.tmp / "gone.py").write_text("x = 0\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "seed")
        (self.tmp / "foreign.py").write_text("deploy = 1\n", encoding="utf-8")  # чужая грязь
        (self.tmp / "gone.py").unlink()                                          # чужое удаление
        before = self._head()
        with mock.patch.object(agent, "BASE", self.tmp), \
             mock.patch.object(agent, "tool_journal") as journal, \
             mock.patch.object(agent.immune, "enqueue"):
            pre = agent._pre_shell_dirt()
            self.assertEqual(pre.get("gone.py"), -1.0, "сентинел чужого удаления")
            # «команда» ничего не тронула — коммита нет вовсе
            agent._autocommit_self_edit(pre)
            self.assertEqual(self._head(), before, "чужая грязь не закоммичена")
            journal.assert_not_called()
            # «команда» написала её файл — коммитится ТОЛЬКО он
            (self.tmp / "hers.py").write_text("mine = 2\n", encoding="utf-8")
            agent._autocommit_self_edit(pre)
        self.assertNotEqual(self._head(), before)
        show = self._git("show", "--name-only", "HEAD").stdout
        self.assertIn("hers.py", show)
        self.assertNotIn("foreign.py", show)
        self.assertNotIn("gone.py", show, "чужое удаление не приписано ей")
        status = self._git("status", "--porcelain").stdout
        self.assertIn("foreign.py", status, "чужая грязь лежит, как её оставили")


if __name__ == "__main__":
    unittest.main(verbosity=2)
