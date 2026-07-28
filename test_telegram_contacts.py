from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import telegram_contacts as contacts


class Entity:
    def __init__(self, ident: int, first: str, last: str = "", username: str = ""):
        self.id = ident
        self.first_name = first
        self.last_name = last
        self.username = username or None
        self.usernames = ()


class ContactBookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_contactbook_")
        self.old = contacts.CONTACTS_PATH
        contacts.CONTACTS_PATH = Path(self.tmp.name) / "contacts.json"
        contacts.reset_cache()

    def tearDown(self):
        contacts.CONTACTS_PATH = self.old
        contacts.reset_cache()
        self.tmp.cleanup()

    def test_persists_contacts_aliases_and_seen_senders(self):
        contacts.observe(Entity(1, "Евгений", "Петров", "evpetrov"), contact=True,
                         aliases=("Женя",), seen_at=100)
        contacts.reset_cache()
        self.assertEqual(contacts.candidates("Женя")[0]["id"], "1")
        self.assertEqual(contacts.candidates("@evpetrov")[0]["id"], "1")

    def test_owner_given_alias_beats_fuzzy_recent_name(self):
        now = time.time()
        contacts.observe(Entity(1, "Евгений", "Петров"), dialog=True, seen_at=now - 1000)
        contacts.observe(Entity(2, "Евгений", "Сидоров"), dialog=True, seen_at=now - 10)
        contacts.add_alias(1, "Женя работа")
        self.assertEqual(contacts.candidates("Женя работа")[0]["id"], "1")
        self.assertEqual(contacts.candidates("Евгений")[0]["id"], "2")

    def test_outbound_history_guides_next_ambiguous_choice(self):
        contacts.observe(Entity(1, "Анна", "Первая"), dialog=True, seen_at=1000)
        contacts.observe(Entity(2, "Анна", "Вторая"), dialog=True, seen_at=1000)
        contacts.mark_outbound(2)
        self.assertEqual(contacts.candidates("Анна")[0]["id"], "2")


if __name__ == "__main__":
    unittest.main()
