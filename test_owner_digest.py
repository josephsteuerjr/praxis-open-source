"""PASS: owner-only 'other rooms right now' digest — heals cross-thread dissociation."""
import json, os, tempfile, unittest
from pathlib import Path

os.environ.setdefault("PRAXIS_TEST", "1")
import agent
import notes
import rooms


class TestOtherRoomsDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".state").mkdir()
        (self.tmp / ".scratch").mkdir()
        self._old_state = agent.STATE_DIR
        self._old_scratch = notes.SCRATCH_DIR
        self._old_mode = rooms.effective_mode
        agent.STATE_DIR = self.tmp / ".state"
        notes.SCRATCH_DIR = self.tmp / ".scratch"
        rooms.effective_mode = lambda cid, now=None: (
            "dead" if str(cid) == "-100dead" else "normal")
        meta = {
            "123456789": {"last_ts": 100.0, "last_author": "Praxis", "is_dm": True, "name": "Owner"},
            "-100777": {"last_ts": 500.0, "last_author": "Member", "is_dm": False, "name": "Project Chat"},
            "234567890": {"last_ts": 400.0, "last_author": "Praxis", "is_dm": True, "name": "Person"},
            "-100dead": {"last_ts": 999.0, "last_author": "x", "is_dm": False, "name": "DeadRoom"},
        }
        (self.tmp / ".state" / "buf_meta.json").write_text(json.dumps(meta), encoding="utf-8")
        notes.append("-100777", "сказала: «обсудили формат»")
        notes.append("234567890", "промолчала: собеседник переваривает разбор")
        notes.append("-100dead", "давно мёртвая комната")
        notes.append("123456789", "с владельцем прямо сейчас")

    def tearDown(self):
        agent.STATE_DIR = self._old_state
        notes.SCRATCH_DIR = self._old_scratch
        rooms.effective_mode = self._old_mode

    def test_shows_other_rooms_sorted_by_recency(self):
        d = agent.other_rooms_digest(exclude_chat_id="123456789")
        self.assertIn("Project Chat", d)
        self.assertIn("Person", d)
        self.assertIn("обсудили формат", d)
        # current owner chat is excluded (that's where she is now)
        self.assertNotIn("с владельцем прямо сейчас", d)
        # dead room is filtered out
        self.assertNotIn("DeadRoom", d)
        # Project Chat (ts 500) appears before Person (ts 400)
        self.assertLess(d.index("Project Chat"), d.index("Person"))

    def test_empty_when_no_meta(self):
        (self.tmp / ".state" / "buf_meta.json").unlink()
        self.assertEqual(agent.other_rooms_digest(), "")

    def test_group_vs_dm_label(self):
        d = agent.other_rooms_digest(exclude_chat_id="123456789")
        self.assertIn("(группа", d)  # Project Chat is a group
        self.assertIn("(ЛС", d)      # Person is a DM


if __name__ == "__main__":
    unittest.main()
