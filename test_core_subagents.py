"""PASS 30 Этап 1: контракт praxis.subagent-result.v1 — статусы, расписки, приглашение.

Запуск:  python praxis_test.py test_core_subagents -v
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from core import subagents as core_subagents


class StatusMapTests(unittest.TestCase):
    def test_terminal_statuses_are_distinct_not_success_by_default(self):
        self.assertEqual(core_subagents.map_status("done"), "succeeded")
        self.assertEqual(core_subagents.map_status("error"), "failed")
        self.assertEqual(core_subagents.map_status("failed"), "failed")
        self.assertEqual(core_subagents.map_status("stopped"), "cancelled")
        self.assertEqual(core_subagents.map_status("timed_out"), "timeout")
        self.assertEqual(core_subagents.map_status("lost"), "failed")
        self.assertEqual(core_subagents.map_status("что-то-новое"), "failed",
                         "неизвестный статус — не успех по умолчанию")

    def test_event_key(self):
        self.assertEqual(core_subagents.event_key("t1", "a1"), "forge:t1:a1")
        self.assertEqual(core_subagents.event_key("t1", "a1", "overdue"),
                         "forge:t1:a1:overdue")

    def test_overdue_minutes_env(self):
        with mock.patch.dict(os.environ, {"PRAXIS_FORGE_AGENT_OVERDUE_MIN": "0"}):
            self.assertEqual(core_subagents.overdue_minutes(), 0)
        with mock.patch.dict(os.environ, {"PRAXIS_FORGE_AGENT_OVERDUE_MIN": "мусор"}):
            self.assertEqual(core_subagents.overdue_minutes(), 90)


class NormalizeTests(unittest.TestCase):
    def _payload(self, result, request=None, task=None):
        return core_subagents.normalize("code-1", "agent-ab", result,
                                        request=request, task=task,
                                        unit_dir="memory/.forge/tasks/code-1/agents/agent-ab")

    def test_done_worker(self):
        p = self._payload(
            {"status": "done", "finished": "2026-07-22T10:00:00+00:00", "role": "worker",
             "model": "m", "result": "сделано: смотри дифф", "tool_calls": 7,
             "diff_tail": "+x"},
            request={"brief": "почини", "created": "2026-07-22T09:00:00+00:00"},
            task={"goal": "цель задачи", "priority": "urgent"})
        self.assertEqual(p["schema"], core_subagents.SCHEMA)
        self.assertEqual(p["status"], "succeeded")
        self.assertEqual(p["priority"], "urgent")
        self.assertEqual(p["recap"], "сделано: смотри дифф")
        self.assertEqual(p["cost"]["calls"], 7)
        self.assertIn("result.json#diff_tail", p["diff_ref"])
        self.assertEqual(p["started_at"], "2026-07-22T09:00:00+00:00")
        self.assertEqual(p["finished_at"], "2026-07-22T10:00:00+00:00")

    def test_recap_capped(self):
        p = self._payload({"status": "done", "result": "д" * 9000})
        self.assertLessEqual(len(p["recap"]), core_subagents.RECAP_CHARS)

    def test_causality_receipts_not_self_assessment(self):
        p = self._payload({"status": "stopped"}, request={"brief": "b"})
        c = p["causality"]
        self.assertEqual(c["intent_author"], "praxis")
        self.assertEqual(c["method_author"], "subagent")
        self.assertTrue(c["cancel_available"])
        self.assertEqual(c["cancelled_by"], "praxis", "stop пишет родитель — расписка")
        self.assertTrue(c["receipts"]["request"].endswith("request.json"))
        self.assertTrue(c["receipts"]["result"].endswith("result.json"))

    def test_delegated_lineage(self):
        p = self._payload({"status": "done"},
                          request={"node_id": "n3", "owns": ["a.py"]})
        self.assertEqual(p["causality"]["intent_author"], "praxis-delegate")
        self.assertEqual(p["lineage"],
                         {"node_id": "n3", "owns": ["a.py"], "spawned_by": ""})

    def test_delegate_receipt_via_spawned_by(self):
        """Расписка манометра: бриф сочинил воркер-родитель, не она напрямую."""
        p = self._payload({"status": "done"}, request={"spawned_by": "agent-parent"})
        self.assertEqual(p["causality"]["intent_author"], "praxis-delegate")
        self.assertEqual(p["lineage"]["spawned_by"], "agent-parent")

    def test_lost_maps_to_failed(self):
        p = self._payload({"status": "lost", "finished": "2026-07-22T10:00:00+00:00",
                           "error": "supervisor process died before result.json"})
        self.assertEqual(p["status"], "failed")
        self.assertIn("supervisor", p["error"])

    def test_tests_receipt_from_checks(self):
        p = self._payload({"status": "done",
                           "checks": [{"status": "passed"}, {"status": "passed"},
                                      {"status": "failed"}],
                           "log": "logs/x.log"})
        self.assertEqual(p["tests"], {"passed": 2, "failed": 1, "log_ref": "logs/x.log"})


class InvitationTests(unittest.TestCase):
    def test_invitation_has_machine_ids_and_fruit_tone(self):
        p = core_subagents.normalize("code-9", "agent-ff",
                                     {"status": "done", "result": "готово"},
                                     task={"goal": "редизайн", "priority": "urgent"})
        text = core_subagents.invitation([p])
        self.assertIn("code-9", text)
        self.assertIn("agent-ff", text)
        self.assertIn("плод", text)
        self.assertIn("Решение", text)

    def test_timeout_is_signal_not_death(self):
        p = core_subagents.normalize("code-9", "agent-ff",
                                     {"status": "timed_out", "error": "долго"})
        text = core_subagents.invitation([p])
        self.assertIn("просрочен (ещё жив)", text)

    def test_empty(self):
        self.assertEqual(core_subagents.invitation([]), "")


class VisibleTruncationTests(unittest.TestCase):
    """Срез, который не видно, читается как «текст тут и кончился» (закон «не врать»)."""

    def test_boundary_marks_only_when_something_was_actually_cut(self):
        self.assertEqual(core_subagents._clip("а" * 200, 200), "а" * 200)
        self.assertIn("обрезано", core_subagents._clip("а" * 201, 200))

    def test_marker_lives_inside_the_cap_and_counts_the_real_remainder(self):
        """Метка поверх капа сделала бы «не длиннее N» новой неправдой, а счёт «сколько
        ещё» обязан сойтись с тем, что вправду отброшено — при любой длине входа."""
        for extra in (1, 9, 10, 99, 100, 999, 1000, 12345):
            value = "я" * (200 + extra)
            cut = core_subagents._clip(value, 200)
            self.assertLessEqual(len(cut), 200, f"кап пробит на +{extra}")
            head = cut.split(" …[обрезано, ещё ")[0]
            dropped = int(cut.split("ещё ")[1].split(" симв.")[0])
            self.assertEqual(len(head) + dropped, len(value), f"счёт не сошёлся на +{extra}")

    def test_long_goal_says_how_much_is_missing(self):
        # Живой случай: цель code-7e541aaa на проде 253 символа, и «Работать в уже созданной
        # proposal-копии a3cc5d8e» исчезало молча — вместе с тем, ГДЕ работать.
        goal = ("Найти и исправить ложное privacy-hold в группах, когда data/privacy advisor "
                "недоступен: отличить unavailable от реального запрета, обеспечить безопасную "
                "повторную оценку/доставку, добавить регрессионные тесты. Работать в уже "
                "созданной proposal-копии a3cc5d8e.")
        self.assertGreater(len(goal), core_subagents.GOAL_CHARS)
        p = core_subagents.normalize("code-7e541aaa", "task", {"status": "lost"},
                                     task={"goal": goal})
        self.assertIn("обрезано", p["goal"])
        self.assertLessEqual(len(p["goal"]), core_subagents.GOAL_CHARS)
        head = p["goal"].split(" …[обрезано, ещё ")[0]
        dropped = int(p["goal"].split("ещё ")[1].split(" симв.")[0])
        self.assertEqual(len(head) + dropped, len(goal))
        # именно этот хвост и пропадал молча — теперь его отсутствие видно и сосчитано
        self.assertNotIn("proposal-копии a3cc5d8e", head)

    def test_short_goal_is_untouched(self):
        p = core_subagents.normalize("code-1", "a", {"status": "done"}, task={"goal": "цель"})
        self.assertEqual(p["goal"], "цель")


def _lost_task_payload(task_id="hcode-c584ba6e", quiet_h=5.6, scope="host",
                       priority="urgent", goal="починить свадебный доступ",
                       recap=None, created="2026-07-26T18:56:19+00:00",
                       finished="2026-07-27T00:33:13+00:00"):
    """Ровно то, что кладёт жнец брошенных задач (`forge.reconcile_lost_tasks`)."""
    if recap is None:
        recap = (f"Задача открыта {created} и {quiet_h}ч не подаёт признаков: ни живых "
                 f"процессов, ни новых событий. Цель: «{goal}». Корень: /. Я ничего не "
                 f"закрыла и не отменила — статус lost это ярлык «потеряна из виду» по "
                 f"порогу тишины 6ч, не приговор. Продолжить — теми же тулами по тому же id "
                 f"(первое действие вернёт active), закрыть — coding_session(finish), "
                 f"отпустить — просто оставить как есть. ⚠ scope={scope}: удалённые операции "
                 f"отсюда не видны — если на той стороне что-то ещё крутится, спроси "
                 f"coding_process(list), прежде чем считать задачу мёртвой.")
    return core_subagents.normalize(
        task_id, "task",
        {"status": "lost", "role": "task", "finished": finished, "result": recap,
         "error": f"нет следов жизни {quiet_h}ч"},
        request={"created": created},
        task={"id": task_id, "goal": goal, "priority": priority, "scope": scope},
        unit_dir=f"memory/.forge/tasks/{task_id}")


class LostTaskInvitationTests(unittest.TestCase):
    """R3: жнец брошенных задач ходит контуром субагентов, и до правки `invitation`
    писала про ЛЮБОЙ payload «воркер по «…» упал». На проде 27.07 в очереди пять таких
    задач; у четырёх воркеров не было ни одного, а пятая — прямая просьба Егора."""

    def test_lost_task_is_not_reported_as_a_fallen_worker(self):
        text = core_subagents.invitation([_lost_task_payload()])
        self.assertNotIn("упал", text)
        self.assertNotIn("воркер по", text)     # «а не воркер» в шапке — признание, не диагноз
        self.assertIn("потеряна из виду", text)
        self.assertIn("не закрыта, не отменена", text)

    def test_lost_task_names_what_when_and_what_she_can_do(self):
        text = core_subagents.invitation([_lost_task_payload()])
        self.assertIn("hcode-c584ba6e", text)                     # что за задача
        self.assertIn("починить свадебный доступ", text)          # её цель
        self.assertIn("открыта 2026-07-26T18:56:19+00:00", text)  # когда создана
        self.assertIn("потеря замечена 2026-07-27T00:33:13+00:00", text)
        self.assertIn("нет следов жизни 5.6ч", text)              # когда последний след
        self.assertIn("порогу тишины", text)                      # почему считается потерянной
        self.assertIn("coding_session(finish)", text)             # что она может сделать

    def test_frame_admits_the_receipt_says_failed_and_task_is_no_poll_handle(self):
        # Расписка отдаёт status=failed (класса «потеряна» в контракте нет) и agent_id=«task».
        # Промолчать об этом — оставить ей на выбор две противоречащие версии одного факта.
        p = _lost_task_payload()
        self.assertEqual(p["status"], "failed")
        self.assertEqual(p["agent_id"], "task")
        text = core_subagents.invitation([p])
        self.assertIn("status=failed", text)
        self.assertIn("coding_agent(poll) по ней ничего не найдёт", text)

    def test_levers_survive_the_line_clip(self):
        """Расписка потерянной задачи — это и есть ответ «что с ней делать». Порежь её
        по 220 (как строку юнита) — и разбудишь списком потерь без единого рычага."""
        text = core_subagents.invitation([_lost_task_payload()])
        self.assertIn("coding_session(finish)", text)
        self.assertIn("coding_process(list)", text)      # предупреждение про scope=host
        self.assertNotIn("обрезано", text)

    def test_urgent_task_keeps_its_mark(self):
        urgent = core_subagents.invitation([_lost_task_payload(priority="urgent")])
        plain = core_subagents.invitation([_lost_task_payload(priority="normal")])
        self.assertIn("⚡", urgent)
        self.assertNotIn("⚡", plain)

    def test_bare_receipt_says_do_not_know_instead_of_inventing(self):
        """Расписки без текста (ни result, ни error) быть не должно, но если она придёт —
        кадр обязан признаться, а не досочинить причину и не промолчать."""
        bare = core_subagents.normalize("code-x", "task",
                                        {"status": "lost", "role": "task"},
                                        task={"goal": "цель"})
        self.assertEqual(bare["recap"], "")
        text = core_subagents.invitation([bare])
        self.assertIn("в расписке не написано", text)
        self.assertIn("«не знаю»", text)
        self.assertIn("сколько длилась тишина — в расписке не названо", text)
        self.assertNotIn("упал", text)

    def test_missing_created_is_named_not_guessed(self):
        p = _lost_task_payload(created="")
        text = core_subagents.invitation([p])
        self.assertIn("когда открыта — в расписке не записано", text)

    def test_mixed_wake_keeps_the_two_reasons_apart(self):
        worker = core_subagents.normalize("code-9", "agent-ff",
                                          {"status": "done", "role": "worker",
                                           "result": "готово"},
                                          task={"goal": "редизайн"})
        text = core_subagents.invitation([worker, _lost_task_payload()])
        self.assertIn("воркер по «редизайн» закончил", text)      # юнит остался юнитом
        self.assertIn("потеряна из виду", text)
        self.assertIn("плод", text)                               # рамка плода — только юнитам
        lost_block = text.split("ПОТЕРЯНЫ ИЗ ВИДУ")[1]
        self.assertNotIn("воркер по", lost_block)   # «а не воркер» в шапке — это признание
        self.assertNotIn("упал", lost_block)

    def test_worker_that_vanished_is_not_called_fallen_either(self):
        """`lost` у ЮНИТА = супервизор умер без result.json. Контракт кладёт это в failed,
        но «упал» приписало бы падение там, где известна одна пропажа."""
        p = core_subagents.normalize("code-1", "agent-ab",
                                     {"status": "lost", "role": "worker"},
                                     task={"goal": "цель"})
        text = core_subagents.invitation([p])
        self.assertIn("пропал без расписки", text)
        self.assertNotIn("упал", text)
        self.assertIn("agent-ab", text)                           # машинный id на месте


if __name__ == "__main__":
    unittest.main(verbosity=2)
