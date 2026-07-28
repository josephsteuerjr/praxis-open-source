"""
Канарейки: показатель обязан ловить то, ради чего заведён, и не ловить лишнего.

Отдельно проверяется, что канарейка НИЧЕГО не меняет в том, что измеряет: показатель,
который пишет в измеряемое, перестаёт быть показателем.

Запуск:  python praxis_test.py test_canary -v
"""

from __future__ import annotations

import datetime as _dt
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

import canary
import perception
import telegram_routes as tr

PEER = "-1001240718803"
HOUR = 3600.0


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_canary_"))
        (self.tmp / "memory" / "runs" / "2026-07").mkdir(parents=True)
        (self.tmp / "memory" / ".state" / "group_context").mkdir(parents=True)
        self.now = 1785000000.0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, run_id: str, *, status: str, at: float, chat: str = "",
             kind: str = "chat_turn", goal: str = ""):
        d = self.tmp / "memory" / "runs" / "2026-07" / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text(json.dumps({
            "run_id": run_id, "status": status,
            "created_at": _iso(at), "updated_at": _iso(at),
            "context": {"kind": kind, "delivery_chat_id": chat or None, "goal": goal},
        }, ensure_ascii=False), encoding="utf-8")

    def _skip(self, **rec):
        p = self.tmp / "memory" / ".state" / "perception_skips.jsonl"
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _flip(self, peer=PEER, *, at: float, status=tr.FALSE):
        p = (self.tmp / "memory" / ".state" / "group_context"
             / f"{tr._slug(peer)}.route.json")
        p.write_text(json.dumps({
            "schema": tr.SCHEMA, "peer_id": str(peer),
            "epochs": [{"epoch": 1, "forum_status": status,
                        "evidence": "no_topic_openers_in_range", "weight": 2,
                        "since": _iso(at), "last_seen_at": _iso(at),
                        "since_message_id": 1, "until_message_id": 999999,
                        "observations": 1}],
        }, ensure_ascii=False), encoding="utf-8")


class TestStuckRuns(Base):
    def test_counts_only_what_never_finished(self):
        self._run("a", status="done", at=self.now - 5 * HOUR)
        self._run("b", status="failed", at=self.now - 4 * HOUR)
        self._run("c", status="cancelled", at=self.now - 3 * HOUR)
        self._run("d", status="in_doubt", at=self.now - 2 * HOUR, goal="smoke narrate")
        out = canary.stuck_runs(self.tmp, now=self.now)
        self.assertEqual(out["total_runs"], 4)
        self.assertEqual(out["nonterminal"], 1)
        self.assertEqual(out["by_status"], {"in_doubt": 1})
        self.assertEqual(out["rows"][0]["run_id"], "d")
        self.assertAlmostEqual(out["rows"][0]["age_min"], 120.0, places=1)

    def test_stale_threshold_is_age_not_count(self):
        self._run("fresh", status="running", at=self.now - 60.0)
        self._run("old", status="running", at=self.now - 3 * HOUR)
        out = canary.stuck_runs(self.tmp, older_than_min=30.0, now=self.now)
        self.assertEqual(out["nonterminal"], 2)
        self.assertEqual(out["stale"], 1, "минутный ход ещё не завис")
        self.assertEqual(out["rows"][0]["run_id"], "old", "самый старый — первым")

    def test_reading_does_not_touch_the_runs(self):
        """Показатель, который пишет в измеряемое, перестаёт быть показателем."""
        self._run("a", status="running", at=self.now - HOUR)
        before = {p: (p.stat().st_mtime_ns, p.stat().st_size)
                  for p in (self.tmp / "memory" / "runs").rglob("*") if p.is_file()}
        canary.stuck_runs(self.tmp, now=self.now)
        canary.pass_rate(self.tmp)
        after = {p: (p.stat().st_mtime_ns, p.stat().st_size)
                 for p in (self.tmp / "memory" / "runs").rglob("*") if p.is_file()}
        self.assertEqual(before, after, "канарейка не имеет права менять ходы")


class TestDroppedAddresses(Base):
    def test_counts_only_the_overwritten_addresses(self):
        self._skip(ts=self.now - 600, stage="group_wake", **{"class": "отложила"},
                   chat=f"{PEER}__topic__1", meta={"dropped": "addressed"})
        self._skip(ts=self.now - 500, stage="reflex", **{"class": "не_сочла_важным"},
                   chat=PEER, detail="шум")
        self._skip(ts=self.now - 400, stage="group_wake", **{"class": "отложила"},
                   chat=PEER, detail="кулдаун")
        out = canary.dropped_addresses(self.tmp, hours=24, now=self.now)
        self.assertEqual(out["drops"], 1, "только вытеснённые обращения")
        self.assertEqual(out["owner_lost"], 0)

    def test_owner_loss_is_counted_apart(self):
        self._skip(ts=self.now - 300, stage="group_wake", **{"class": "отложила"},
                   chat=PEER, meta={"dropped": "addressed", "owner_lost": "1"})
        self._skip(ts=self.now - 200, stage="group_wake", **{"class": "отложила"},
                   chat=PEER, meta={"dropped": "addressed"})
        out = canary.dropped_addresses(self.tmp, hours=24, now=self.now)
        self.assertEqual((out["drops"], out["owner_lost"]), (2, 1))

    def test_collapsed_repeats_are_not_lost(self):
        self._skip(ts=self.now - 100, stage="group_wake", **{"class": "отложила"},
                   chat=PEER, meta={"dropped": "addressed"}, prev_n=4)
        self.assertEqual(canary.dropped_addresses(self.tmp, now=self.now)["drops"], 5)

    def test_short_journal_says_how_far_it_sees(self):
        """«Ноль за сутки» и «ноль за час, дальше не видно» — разные ответы."""
        self._skip(ts=self.now - 2 * HOUR, stage="reflex", **{"class": "не_увидела"})
        out = canary.dropped_addresses(self.tmp, hours=24, now=self.now)
        self.assertEqual(out["drops"], 0)
        self.assertAlmostEqual(out["span_hours"], 2.0, places=1)

    def test_rooms_are_collapsed_in_the_tally(self):
        self._flip(at=self.now - 10 * HOUR)
        for topic in ("1", "2"):
            self._skip(ts=self.now - 60, stage="group_wake", **{"class": "отложила"},
                       chat=f"{PEER}__topic__{topic}", meta={"dropped": "addressed"})
        out = canary.dropped_addresses(self.tmp, now=self.now)
        self.assertEqual(out["by_room"], {PEER: 2})


class TestPassRate(Base):
    def test_split_keys_are_one_room(self):
        self._run("r1", status="done", at=self.now - 3 * HOUR, chat=PEER)
        self._run("r2", status="done", at=self.now - 2 * HOUR, chat=f"{PEER}__topic__7")
        self._run("r3", status="done", at=self.now - HOUR, chat=f"{PEER}__topic__9")
        out = canary.pass_rate(self.tmp)
        self.assertEqual(list(out), [PEER])
        self.assertEqual(out[PEER]["keys"], 3)
        self.assertEqual(out[PEER]["before"]["passes"], 3)

    def test_without_a_flip_everything_is_baseline(self):
        self._run("r1", status="done", at=self.now - HOUR, chat=PEER)
        row = canary.pass_rate(self.tmp)[PEER]
        self.assertEqual(row["flip_at"], "")
        self.assertEqual((row["before"]["passes"], row["after"]["passes"]), (1, 0))
        self.assertIsNone(row["ratio"])

    def test_the_flip_splits_before_from_after(self):
        flip = self.now - 2 * HOUR
        self._flip(at=flip)
        for i in range(4):                       # 4 прохода за 2 часа до перекладки
            self._run(f"b{i}", status="done", at=flip - HOUR - i * 60, chat=PEER)
        for i in range(2):
            self._run(f"a{i}", status="done", at=flip + 60 + i * 60,
                      chat=f"{PEER}__topic__3")
        row = canary.pass_rate(self.tmp)[PEER]
        self.assertEqual((row["before"]["passes"], row["after"]["passes"]), (4, 2))
        self.assertTrue(row["flip_at"])
        self.assertEqual(row["ratio"], round(2 / 4, 2))

    def test_intensity_is_per_working_hour_not_per_calendar_hour(self):
        """Простой не должен разводить темп вниз: она спит, её останавливают."""
        self._run("x", status="done", at=self.now - 100 * HOUR, chat=PEER)
        for i in range(5):
            self._run(f"y{i}", status="done", at=self.now - 60 - i, chat=PEER)
        row = canary.pass_rate(self.tmp)[PEER]
        self.assertEqual(row["before"]["active_hours"], 2)
        self.assertEqual(row["before"]["per_active_hour"], 3.0)
        self.assertEqual(row["before"]["busiest"], 5)

    def test_runs_without_a_chat_are_not_a_room(self):
        self._run("solo", status="done", at=self.now - HOUR, chat="", kind="task_window")
        self.assertEqual(canary.pass_rate(self.tmp), {})


class TestRegistryAgreement(Base):
    """Канарейка читает ленту эпох своим путём — она обязана сходиться с реестром."""

    def test_flip_moment_agrees_with_the_registry(self):
        orig = tr.DIR
        tr.DIR = self.tmp / "memory" / ".state" / "group_context"
        try:
            tr.observe(PEER, kind="no_topic_openers_in_range",
                       since_message_id=1, until_message_id=999999)
            self.assertEqual(tr.current(PEER)["forum_status"], tr.FALSE)
            self.assertIsNotNone(canary._flip_at(self.tmp, PEER))
        finally:
            tr.DIR = orig

    def test_a_forum_never_reads_as_a_flip(self):
        orig = tr.DIR
        tr.DIR = self.tmp / "memory" / ".state" / "group_context"
        try:
            tr.observe(PEER, kind="topic_opener_seen", message_id=5)
            self.assertEqual(tr.current(PEER)["forum_status"], tr.TRUE)
            self.assertIsNone(canary._flip_at(self.tmp, PEER),
                              "в форуме склейки не было — сравнивать не с чем")
        finally:
            tr.DIR = orig

    def test_unknown_room_has_no_flip(self):
        self.assertIsNone(canary._flip_at(self.tmp, "-100777"))


def _runner():
    """Раннеру нужен telethon только на импорте — подкладываем заглушку, как в PASS 4."""
    if "mtproto_runner" in sys.modules:
        return sys.modules["mtproto_runner"]
    import os
    os.environ.setdefault("PRAXIS_TEST", "1")
    os.environ.setdefault("TELEGRAM_API_ID", "1")
    os.environ.setdefault("TELEGRAM_API_HASH", "test")
    os.environ.setdefault("TELEGRAM_SESSION", ":memory:")
    prior = sys.modules.get("telethon")
    fake = types.ModuleType("telethon")
    fake.TelegramClient = type("C", (), {"__init__": lambda self, *a, **k: None,
                                         "on": lambda self, *a, **k: (lambda fn: fn)})
    fake.events = type("E", (), {
        "NewMessage": staticmethod(lambda *a, **k: object()),
        "MessageEdited": staticmethod(lambda *a, **k: object()),
        "ChatAction": staticmethod(lambda *a, **k: object())})
    sys.modules["telethon"] = fake
    try:
        import mtproto_runner
    finally:
        if prior is None:
            sys.modules.pop("telethon", None)
        else:
            sys.modules["telethon"] = prior
    return mtproto_runner


class TestOverwrittenAddressIsRecorded(Base):
    """Тихая перезапись обращения обязана оставлять след — это и есть канарейка."""

    def setUp(self):
        super().setUp()
        self.runner = _runner()
        self._skips = perception.SKIPS_PATH
        perception.SKIPS_PATH = self.tmp / "memory" / ".state" / "perception_skips.jsonl"
        perception._LAST_SKIP.clear()

    def tearDown(self):
        perception.SKIPS_PATH = self._skips
        perception._LAST_SKIP.clear()
        super().tearDown()

    def _wake(self, mid, *, addressed=True, owner=False, speaker="A", kind="mention"):
        return self.runner.GroupWake(
            message_id=mid, message_ts=float(mid), kind=kind, speaker=speaker,
            sender_id=mid, owner=owner, known=True, family=False,
            context_snapshot="", reply_targets_snapshot=(), media_snapshot=(),
            addressed=addressed)

    def _notes(self):
        return canary.dropped_addresses(self.tmp, hours=24, now=_dt.datetime.now().timestamp())

    def test_address_replacing_an_address_is_noted(self):
        wakes = {}
        self.runner._group_wakes = wakes
        self.assertTrue(self.runner._install_group_wake("room", self._wake(1)))
        self.assertTrue(self.runner._install_group_wake("room", self._wake(2)))
        out = self._notes()
        self.assertEqual(out["drops"], 1)
        self.assertIn("#1", out["rows"][0]["detail"])
        self.assertIn("#2", out["rows"][0]["detail"])

    def test_losing_egor_authority_is_visible(self):
        self.runner._group_wakes = {}
        self.runner._install_group_wake("room", self._wake(1, owner=True, speaker="Егор"))
        self.runner._install_group_wake("room", self._wake(2, owner=False, speaker="Коля"))
        self.assertEqual(self._notes()["owner_lost"], 1,
                         "обращение Егора ушло в проход без его полномочий — это видно")

    def test_ambient_traffic_notes_nothing(self):
        self.runner._group_wakes = {}
        self.runner._install_group_wake("room", self._wake(1, addressed=False))
        self.runner._install_group_wake("room", self._wake(2, addressed=False))
        self.assertEqual(self._notes()["drops"], 0, "фон — не обращение, терять нечего")

    def test_refused_ambient_notes_nothing(self):
        self.runner._group_wakes = {}
        self.runner._install_group_wake("room", self._wake(1))
        self.assertFalse(self.runner._install_group_wake("room", self._wake(2, addressed=False)))
        self.assertEqual(self._notes()["drops"], 0, "обращение уцелело — это не потеря")

    def test_reinstalling_the_same_message_notes_nothing(self):
        self.runner._group_wakes = {}
        self.runner._install_group_wake("room", self._wake(7))
        self.runner._install_group_wake("room", self._wake(7))
        self.assertEqual(self._notes()["drops"], 0, "тот же #id — обновление, не потеря")


if __name__ == "__main__":
    unittest.main()
