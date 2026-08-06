"""Артефакт ≠ доставка ≠ статус — форж-половина её третьего механизма.

01.08 на живом дереве: `code-32d32e70` восемь суток числилась `submitted`, а её
предложение `de64316c` Егор смёржил через двадцать минут после сдачи. Жнец брошенных
туда не доходит — он обходит только `active`; `state_line()` при этом считает
`submitted` активной работой, то есть её блок состояния восемь суток отвечал неправдой
на её же вопрос «что у меня в работе».

И вторая половина того же шва: `hcode-d8799ba2` («разбанить fail2ban по прямой просьбе
Егора») получила ярлык «потеряна из виду» через шесть часов — при том что в её журнале
три успешные хост-команды, последняя `Unbanning …`. Работа была сделана; текст
пробуждения говорил только о тишине.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import forge


class SubmittedLearnsItsFate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = forge.TASKS_DIR
        forge.TASKS_DIR = self.tmp / "tasks"
        forge.TASKS_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        forge.TASKS_DIR = self._orig

    def _submitted(self, task_id="code-32d32e70", proposal_id="de64316c", **extra):
        d = forge.TASKS_DIR / task_id
        d.mkdir(parents=True, exist_ok=True)
        born = dt.datetime.now(dt.timezone.utc).isoformat()
        row = {"id": task_id, "goal": "развести privacy deny и недоступность advisor",
               "status": "submitted", "proposal_id": proposal_id, "created": born,
               "updated": born, "finished": "", "root": str(self.tmp)}
        row.update(extra)
        (d / "task.json").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        return d

    def _read(self, task_id="code-32d32e70"):
        return json.loads((forge.TASKS_DIR / task_id / "task.json")
                          .read_text(encoding="utf-8"))

    def test_merged_proposal_closes_the_task(self):
        self._submitted()
        with mock.patch.object(forge.selfdev, "get", return_value={
                "id": "de64316c", "status": "merged", "decided_by": "egor"}):
            self.assertEqual(forge.reconcile_submitted_tasks(), 1)
        task = self._read()
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["submission_status"], "merged")
        self.assertTrue(task["finished"], "закрытая задача обязана иметь время конца")
        self.assertIn("de64316c", task["submission_result"])
        self.assertIn("egor", task["submission_result"])

    def test_rejected_proposal_closes_the_task_and_says_the_work_did_not_land(self):
        self._submitted()
        with mock.patch.object(forge.selfdev, "get", return_value={
                "id": "de64316c", "status": "rejected", "decided_by": "egor",
                "reason": "ломает контракт рельсов"}):
            self.assertEqual(forge.reconcile_submitted_tasks(), 1)
        task = self._read()
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["submission_status"], "rejected")
        self.assertIn("ломает контракт рельсов", task["submission_result"])
        self.assertIn("не легла", task["submission_result"])

    def test_a_proposal_still_awaiting_a_verdict_is_left_alone(self):
        """Пока решения нет — `submitted` это правда, и трогать нечего."""
        self._submitted()
        with mock.patch.object(forge.selfdev, "get", return_value={
                "id": "de64316c", "status": "building"}):
            self.assertEqual(forge.reconcile_submitted_tasks(), 0)
        self.assertEqual(self._read()["status"], "submitted")

    def test_a_proposal_missing_from_the_registry_is_recorded_not_buried(self):
        """Отсутствие записи — доказательство слабее вердикта: статус не трогаем."""
        self._submitted()
        with mock.patch.object(forge.selfdev, "get", return_value=None):
            self.assertEqual(forge.reconcile_submitted_tasks(), 0)
            task = self._read()
            self.assertEqual(task["status"], "submitted")
            self.assertTrue(task.get("submission_unknown_since"))
            # и слепота записывается ОДИН раз, а не каждый тик часов
            forge.reconcile_submitted_tasks()
        events = (forge.TASKS_DIR / "code-32d32e70" / "events.jsonl").read_text(encoding="utf-8")
        self.assertEqual(events.count("task_submission_unknown"), 1)

    def test_a_task_without_a_proposal_is_not_touched(self):
        self._submitted(proposal_id="")
        with mock.patch.object(forge.selfdev, "get", side_effect=AssertionError("не спрашивать")):
            self.assertEqual(forge.reconcile_submitted_tasks(), 0)
        self.assertEqual(self._read()["status"], "submitted")

    def test_settled_task_stops_counting_as_active_work(self):
        """Смысл всей сверки: её блок состояния перестаёт сообщать о работе, которой нет."""
        self._submitted()
        self.assertIn("coding: задач 1", forge.state_line())
        with mock.patch.object(forge.selfdev, "get", return_value={
                "id": "de64316c", "status": "merged", "decided_by": "egor"}):
            forge.reconcile_submitted_tasks()
        self.assertNotIn("coding: задач 1", forge.state_line())


class LostSaysWhatWasAlreadyDone(unittest.TestCase):
    def setUp(self):
        from core import events as core_events
        self.core_events = core_events
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = {"tasks": forge.TASKS_DIR, "journal": core_events.JOURNAL,
                      "delivered": core_events.DELIVERED}
        forge.TASKS_DIR = self.tmp / "tasks"
        forge.TASKS_DIR.mkdir(parents=True, exist_ok=True)
        core_events.JOURNAL = self.tmp / "core_events.jsonl"
        core_events.DELIVERED = self.tmp / "core_events_delivered.json"

    def tearDown(self):
        forge.TASKS_DIR = self._orig["tasks"]
        self.core_events.JOURNAL = self._orig["journal"]
        self.core_events.DELIVERED = self._orig["delivered"]

    def _abandoned(self, task_id, root, events=()):
        d = forge.TASKS_DIR / task_id
        d.mkdir(parents=True, exist_ok=True)
        age = forge.TASK_ABANDONED_SEC + 3600
        born = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age)).isoformat()
        (d / "task.json").write_text(json.dumps(
            {"id": task_id, "goal": "разбанить по прямой просьбе Егора", "root": str(root),
             "status": "active", "created": born, "updated": born}, ensure_ascii=False),
            encoding="utf-8")
        if events:
            (d / "events.jsonl").write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
                encoding="utf-8")
        stamp = time.time() - age
        for p in list(d.rglob("*")) + [d]:
            os.utime(p, (stamp, stamp))
        return d

    def _recap(self):
        pending = self.core_events.undelivered({"subagent_result"})
        self.assertEqual(len(pending), 1)
        return pending[0]["payload"]["recap"]

    def test_recap_names_the_last_observable_work(self):
        self._abandoned("hcode-d8799ba2", self.tmp, events=[
            {"at": "x", "kind": "task_started"},
            {"at": "x", "kind": "command", "summary": "host ok: Unbanning 203.0.113.59"},
        ])
        self.assertEqual(forge.reconcile_lost_tasks(), 1)
        recap = self._recap()
        self.assertIn("Unbanning 203.0.113.59", recap,
                      "вернувшись к задаче, она должна видеть, что уже сделано")
        self.assertNotIn("task_started", recap, "служебные отметки петли — не работа")

    def test_recap_warns_when_the_worktree_is_gone(self):
        self._abandoned("code-db853ef9", self.tmp / "снесённое-дерево")
        self.assertEqual(forge.reconcile_lost_tasks(), 1)
        self.assertIn("БОЛЬШЕ НЕТ", self._recap())

    def test_recap_says_the_root_survived_when_it_did(self):
        self._abandoned("code-alive", self.tmp)
        self.assertEqual(forge.reconcile_lost_tasks(), 1)
        self.assertIn("на месте", self._recap())

    def test_recap_without_any_events_still_works(self):
        self._abandoned("code-silent", self.tmp)
        self.assertEqual(forge.reconcile_lost_tasks(), 1)
        recap = self._recap()
        self.assertIn("не закрыла", recap)
        self.assertNotIn("Последнее наблюдаемое", recap)


if __name__ == "__main__":
    unittest.main()
