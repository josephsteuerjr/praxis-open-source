import json
import hashlib
import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class MemoryCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"PRAXIS_BASE": str(self.base), "PRAXIS_OWNER_ID": "101"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _room(self, chat_id: str, text: str) -> None:
        path = self.base / "memory" / "rooms" / f"{chat_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    @staticmethod
    def _room_line(rendered: str, chat_id: str) -> str:
        for line in rendered.splitlines():
            if line.startswith("- ") and chat_id in line:
                return line
        return ""

    def test_catalogue_maps_layers_and_keeps_sql_derived(self):
        import memory_catalog
        (self.base / "memory/people").mkdir(parents=True)
        (self.base / "memory/people/egor.md").write_text("# Егор\n\n- владелец\n", encoding="utf-8")
        (self.base / "memory/computer").mkdir(parents=True)
        (self.base / "memory/computer/MAP.md").write_text("# map\n", encoding="utf-8")
        text = memory_catalog.rebuild().read_text(encoding="utf-8")
        self.assertIn("## Люди", text)
        self.assertIn("people/egor.md", text)
        self.assertIn("computer/MAP.md", text)
        self.assertIn("только пересобираемые индексы", text)

    def test_catalogue_builds_six_deterministic_maps_without_mutating_canon(self):
        import memory_catalog

        memory = self.base / "memory"
        sources = {
            memory / "people/egor.md": (
                "# Egor\n\n- owner\n- [ ] ask whether the kraken replied\n"
            ),
            memory / "rooms/-100.md": (
                "# Kraken room\n\nmode: observer\nmembership: left\n"
                "left_at: 2026-07-13T12:00:00Z\n"
            ),
            memory / "rooms_departed.json": '["-100"]',
            memory / "projects/praxis.md": "# Praxis\n\nLocal execution body.\n",
            memory / "computer/MAP.md": "# Computers\n\n- windows-pc\n",
            memory / "computer/devices/windows-pc.md": "# windows-pc\n",
            memory / "runs/2026-07/run-1/RECAP.md": "# RECAP\n\nDone.\n",
        }
        for path, text in sources.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        run_json = memory / "runs/2026-07/run-1/manifest.json"
        run_json.write_text(
            json.dumps(
                {
                    "context": {"run_id": "run-1", "kind": "computer"},
                    "status": "completed",
                    "updated_at": "2026-07-13T12:30:00Z",
                }
            ),
            encoding="utf-8",
        )
        workspace = self.base / "workspace" / "projects" / "body"
        workspace.mkdir(parents=True)
        (workspace / "README.md").write_text("# Body\n", encoding="utf-8")

        canonical = {path: path.read_bytes() for path in [*sources, run_json]}
        index = memory_catalog.rebuild(memory_dir=memory)
        generated = {
            path.name: path.read_bytes()
            for path in (memory / "maps").glob("*.md")
        }
        self.assertEqual(set(generated), {f"{name}.md" for name in memory_catalog.MAP_NAMES})

        router = index.read_text(encoding="utf-8")
        for name in memory_catalog.MAP_NAMES:
            self.assertIn(f"maps/{name}.md", router)
        rooms = (memory / "maps/ROOMS.md").read_text(encoding="utf-8")
        self.assertIn("left", rooms)
        self.assertIn("observer", rooms)
        projects = (memory / "maps/PROJECTS.md").read_text(encoding="utf-8")
        self.assertIn("praxis.md", projects)
        self.assertIn("workspace/projects/body/README.md", projects)
        self.assertIn("kraken replied", (memory / "maps/THREADS.md").read_text(encoding="utf-8"))
        self.assertIn("run-1", (memory / "maps/RUNS.md").read_text(encoding="utf-8"))
        self.assertIn("windows-pc.md", (memory / "maps/COMPUTERS.md").read_text(encoding="utf-8"))

        memory_catalog.rebuild(memory_dir=memory)
        regenerated = {
            path.name: path.read_bytes()
            for path in (memory / "maps").glob("*.md")
        }
        self.assertEqual(generated, regenerated)
        self.assertEqual(canonical, {path: path.read_bytes() for path in canonical})

    def test_departed_room_is_never_called_active(self):
        """Живьём 27.07: «[Комната -1003908850919] — active; mode=dead» при её id в departed."""
        import memory_catalog

        memory = self.base / "memory"
        self._room("-1003908850919", "# Комната -1003908850919\n\nmode: dead\n"
                                     "mode_reason: Telegram: канал недоступен\n")
        self._room("-1003843005958", "# Комната -1003843005958\n\nmode: dead\n")
        self._room("-1004301095307", "# Комната -1004301095307\n\nmode: normal\n")
        self._room("-100777", "# Комната -100777\n\nmode: normal\nmembership: left\n"
                              "left_at: 2026-01-01T00:00+00:00\n")
        (memory / "rooms_departed.json").write_text(
            json.dumps(["-1003908850919", "-100500"]), encoding="utf-8")
        (memory / "rooms_allowlist.json").write_text(
            json.dumps(["-1004301095307", "-100777", "-100999"]), encoding="utf-8")
        with mock.patch.dict(os.environ, {"TELEGRAM_ALLOWED_CHATS": ""}):
            memory_catalog.rebuild(memory_dir=memory)
        rendered = (memory / "maps" / "ROOMS.md").read_text(encoding="utf-8")

        departed = self._room_line(rendered, "-1003908850919")
        self.assertIn("left", departed)
        self.assertNotIn("active", departed)
        self.assertIn("rooms_departed.json", departed)
        self.assertIn("mode=dead", departed)

        # mode: dead без маски — тоже не «активная»: Telegram эту комнату не отдаёт.
        unreachable = self._room_line(rendered, "-1003843005958")
        self.assertIn("unreachable", unreachable)
        self.assertNotIn("active", unreachable)

        # Обратная ложь: вернувшаяся комната со старой пометкой в профиле — живая.
        returned = self._room_line(rendered, "-100777")
        self.assertTrue(returned.split(" — ")[1].startswith("active"), returned)
        self.assertIn("пометка ухода", returned)

        self.assertTrue(self._room_line(rendered, "-1004301095307").endswith("active; mode=normal"))
        self.assertIn("-100999", rendered)               # доверенная комната без профиля видна
        self.assertIn("профиля ещё нет", rendered)
        self.assertEqual("", self._room_line(rendered, "-100500"))   # ушла и профиля нет

    def test_map_names_the_freeze_that_the_profile_hides(self):
        import memory_catalog

        memory = self.base / "memory"
        self._room("-100111", "# Комната -100111\n\nmode: normal\n")
        (memory / "frozen_chats.json").write_text(json.dumps(["-100111"]), encoding="utf-8")
        with mock.patch.dict(os.environ, {"TELEGRAM_ALLOWED_CHATS": ""}):
            memory_catalog.rebuild(memory_dir=memory)
        line = self._room_line(
            (memory / "maps" / "ROOMS.md").read_text(encoding="utf-8"), "-100111")
        self.assertIn("frozen_chats.json", line)
        self.assertIn("mode=normal", line)

    def test_expired_room_mode_is_not_presented_as_still_running(self):
        import memory_catalog
        import rooms

        # Граница: срок ровно наступил — режима уже нет; за секунду до — ещё есть.
        expiry = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.timezone.utc)
        stamp = "2026-07-20T12:00+00:00"
        self.assertTrue(rooms.mode_until_expired(stamp, now=expiry.timestamp()))
        self.assertFalse(rooms.mode_until_expired(stamp, now=expiry.timestamp() - 1))
        self.assertFalse(rooms.mode_until_expired("", now=expiry.timestamp()))

        memory = self.base / "memory"
        self._room("-100222", f"# Комната -100222\n\nmode: quiet\nmode_until: {stamp}\n")
        self._room("-100333", "# Комната -100333\n\nmode: quiet\nmode_until: 2099-01-01T00:00+00:00\n")
        with mock.patch.dict(os.environ, {"TELEGRAM_ALLOWED_CHATS": ""}):
            memory_catalog.rebuild(memory_dir=memory)
        rendered = (memory / "maps" / "ROOMS.md").read_text(encoding="utf-8")
        self.assertIn("срок вышел", self._room_line(rendered, "-100222"))
        self.assertNotIn("срок вышел", self._room_line(rendered, "-100333"))

    def test_leaving_a_room_records_it_in_the_room_profile_and_returning_clears_it(self):
        import rooms

        memory = self.base / "memory"
        (memory / "rooms").mkdir(parents=True)
        with mock.patch.multiple(
            rooms, MEM_DIR=memory, ROOMS_DIR=memory / "rooms",
            ALLOWLIST=memory / "rooms_allowlist.json",
            FROZEN=memory / "frozen_chats.json",
        ):
            rooms.add_room("-100777")
            self._room("-100777", "# Комната -100777\n\nmode: normal\n")
            state = rooms.membership_state("-100777", mem_dir=memory)
            self.assertEqual(state["state"], "active")
            self.assertIn(state["state"], rooms.MEMBERSHIP_STATES)

            rooms.remove_room("-100777")
            left = rooms.profile_read("-100777")
            self.assertEqual(left["membership"], "left")
            self.assertTrue(left["left_at"])
            self.assertEqual(left["mode"], "normal", "уход не переписывает её режим")
            self.assertEqual(rooms.membership_state("-100777", mem_dir=memory)["state"], "left")

            rooms.remove_room("-100777")
            self.assertEqual(rooms.profile_read("-100777")["left_at"], left["left_at"],
                             "повторный уход не переписывает первую дату")

            rooms.add_room("-100777")
            back = rooms.profile_read("-100777")
            self.assertEqual(back["membership"], "")
            self.assertEqual(back["left_at"], "")
            self.assertEqual(rooms.membership_state("-100777", mem_dir=memory)["state"], "active")

            # Профиля нет — уход его не создаёт, иначе карта обзаведётся комнатой-призраком.
            rooms.remove_room("-100888")
            self.assertFalse((memory / "rooms" / "-100888.md").exists())

    def test_rebuild_survives_broken_files_and_stays_idempotent(self):
        import memory_catalog
        import memory_provenance

        memory = self.base / "memory"
        (memory / "rooms").mkdir(parents=True)
        (memory / "rooms" / "-100.md").write_bytes(b"\xff\xfe# \x00broken\nmode: dead\n")
        self._room("-101", "# Живая\n\nmode: normal\n")
        runs = memory / "runs" / "2026-07"
        (runs / "run-1").mkdir(parents=True)
        (runs / "run-1" / "manifest.json").write_text("{ это не json", encoding="utf-8")
        (runs / "run-2").mkdir(parents=True)
        (runs / "run-2" / "manifest.json").write_text(json.dumps(
            {"id": "run-2", "status": "completed", "updated_at": "2026-07-20T10:00:00Z"}),
            encoding="utf-8")
        (memory / "desires").mkdir(parents=True)
        (memory / "desires" / "events.jsonl").write_bytes(b"\xff\xfe\x00\x01")
        people = memory / "people"
        people.mkdir(parents=True)
        (people / "ok.md").write_text("# Ок\n\n- [ ] спросить\n", encoding="utf-8")
        (people / "broken.md").write_text(
            "# Сломанный\n\n- факт [source:clm-0000000000000000]\n", encoding="utf-8")

        with mock.patch.dict(os.environ, {"TELEGRAM_ALLOWED_CHATS": ""}), \
                mock.patch.object(memory_provenance, "claim_source",
                                  side_effect=RuntimeError("боом")):
            memory_catalog.rebuild(memory_dir=memory)
            first = {path.name: path.read_bytes() for path in (memory / "maps").glob("*.md")}
            memory_catalog.rebuild(memory_dir=memory)
            second = {path.name: path.read_bytes() for path in (memory / "maps").glob("*.md")}
        self.assertEqual(first, second, "пересборка не идемпотентна")

        rooms_map = (memory / "maps" / "ROOMS.md").read_text(encoding="utf-8")
        self.assertIn("-100.md", rooms_map)                 # битый профиль не потерян
        self.assertIn("-101.md", rooms_map)
        runs_map = (memory / "maps" / "RUNS.md").read_text(encoding="utf-8")
        self.assertIn("run-2", runs_map)
        self.assertIn("манифест не читается", runs_map)     # ≠ «unknown» у прогона без манифеста
        threads = (memory / "maps" / "THREADS.md").read_text(encoding="utf-8")
        self.assertIn("спросить", threads)
        self.assertIn("не читаются", threads)               # молчания о нечитаемых желаниях нет
        people_map = (memory / "maps" / "PEOPLE.md").read_text(encoding="utf-8")
        self.assertIn("Ок", people_map)
        self.assertIn("не прочиталось", people_map)

    def test_people_map_never_links_a_dossier_that_does_not_exist(self):
        import memory_catalog

        memory = self.base / "memory"
        (memory / "people").mkdir(parents=True)
        (memory / "people" / "живой.md").write_text("# Живой\n\n- факт\n", encoding="utf-8")
        index = memory_catalog.rebuild(
            memory_dir=memory, extra_people=[("удалённый", "когда-то был")],
        )
        people_map = (memory / "maps" / "PEOPLE.md").read_text(encoding="utf-8")
        self.assertIn("[живой](../people/живой.md)", people_map)
        self.assertNotIn("(../people/удалённый.md)", people_map)
        self.assertIn("досье ещё не заведено", people_map)
        router = index.read_text(encoding="utf-8")
        self.assertNotIn("(people/удалённый.md)", router)
        self.assertIn("люди и отношения (1; ещё 1 без досье)", router)

    def test_people_map_is_bounded(self):
        import memory_catalog

        people = self.base / "memory" / "people"
        people.mkdir(parents=True)
        for index in range(22):
            (people / f"person-{index:02}.md").write_text(
                f"# Person {index}\n\n- fact {index}\n", encoding="utf-8"
            )
        with mock.patch.dict(os.environ, {"PRAXIS_MEMORY_MAP_LIMIT": "20"}):
            memory_catalog.rebuild(memory_dir=self.base / "memory")
        text = (self.base / "memory" / "maps" / "PEOPLE.md").read_text(encoding="utf-8")
        self.assertIn("ещё 2", text)
        self.assertNotIn("person-21.md", text)

    def test_people_hooks_never_promote_unsourced_legacy_fact(self):
        import memory_catalog
        import memory_provenance

        memory = self.base / "memory"
        people = memory / "people"
        people.mkdir(parents=True)
        (people / "legacy.md").write_text(
            "# Legacy Person\n\n- model invented this claim\n", encoding="utf-8",
        )
        def claim_id(subject: str, statement: str) -> str:
            key = hashlib.sha256(
                f"{subject.casefold()}\0{statement.casefold()}".encode()
            ).hexdigest()[:16]
            return f"clm-{key}"

        sourced_id = claim_id("Sourced Person", "verified claim")
        contested_id = claim_id("Contested Person", "disputed claim")
        (people / "sourced.md").write_text(
            "# Sourced Person\n\n"
            f"- [private] (s2) verified claim _(2026-07-14)_ [source:{sourced_id}]\n",
            encoding="utf-8",
        )
        (people / "contested.md").write_text(
            "# Contested Person\n\n"
            f"- [private] (s2) disputed claim _(2026-07-14)_ [source:{contested_id}]\n",
            encoding="utf-8",
        )
        events = memory / "life" / "events" / "2026-07-14.jsonl"
        events.parent.mkdir(parents=True)
        event_id = "evt-20260714T120000000000Z-00000000"
        events.write_text(json.dumps({
            "schema": "praxis.life.event.v1", "id": event_id,
            "ts": "2026-07-14T12:00:00.000Z",
            "kind": "conversation_message", "stream": "7", "chat_id": "7",
            "actor": "Owner", "direction": "in", "text": "primary evidence",
            "source": "telegram", "source_id": "1", "salience": 2,
            "refs": [], "meta": {},
        }) + "\n", encoding="utf-8")
        claims = memory / "life" / "claims"
        claims.mkdir(parents=True)
        for cid, subject, statement, status in (
            (sourced_id, "Sourced Person", "verified claim", "supported"),
            (contested_id, "Contested Person", "disputed claim", "contested"),
        ):
            path = claims / f"{cid}.md"
            meta = {
                "schema": memory_provenance.CLAIM_SCHEMA,
                "id": cid, "subject": subject, "kind": "person",
                "status": status,
                "confidence": "observed",
                "salience": 2, "visibility": "private",
                "evidence_ids": [event_id], "contradicts": [],
                "updated_at": "2026-07-14T12:00:00.000Z",
                "last_run": "rr-20260714T120000000000Z-00000000",
                "path": path.relative_to(self.base).as_posix(),
            }
            path.write_text(
                f"<!-- praxis-claim: {json.dumps(meta, separators=(',', ':'))} -->\n"
                f"# Claim {cid}\n\n## Утверждение\n**{subject}** — {statement}\n\n"
                f"## Статус\n- status: `{status}`\n- confidence: `observed`\n"
                "- salience: `2`\n- visibility: `private`\n"
                f"- evidence: {event_id}\n- contradicts: нет\n\n## Revisions\n"
                f"- 2026-07-14T12:00:00.000Z · {meta['last_run']} · **{status}** · test\n",
                encoding="utf-8",
            )

        index = memory_catalog.rebuild(memory_dir=memory)
        text = (memory / "maps" / "PEOPLE.md").read_text(encoding="utf-8")

        self.assertIn("Legacy Person — legacy dossier; verify", text)
        self.assertNotIn("model invented this claim", text)
        self.assertIn("Sourced Person — verified claim", text)
        self.assertNotIn("disputed claim", text)
        self.assertIn("Contested Person — claim not current; verify", text)
        router = index.read_text(encoding="utf-8")
        self.assertIn("people/sourced.md", router)
        self.assertNotIn("verified claim", router)
        self.assertNotIn("disputed claim", router)

    def test_external_hook_cannot_inject_markdown_structure(self):
        import memory_catalog

        memory = self.base / "memory"
        index = memory_catalog.rebuild(
            memory_dir=memory,
            extra_people=[("guest", "safe hook\n# injected\n- instruction")],
            people_hooks={"guest": "safe hook\n# injected\n- instruction"},
        )
        people_map = (memory / "maps" / "PEOPLE.md").read_text(encoding="utf-8")
        self.assertNotIn("\n# injected", people_map)
        self.assertNotIn("\n- instruction", people_map)
        self.assertIn("safe hook # injected - instruction", people_map)
        self.assertNotIn("# injected", index.read_text(encoding="utf-8"))

    def test_access_is_owner_rooted_and_non_delegable(self):
        import computer_access
        denied = computer_access.change("grant", "202", name="A", scopes=["computer.files"], actor="202")
        self.assertEqual(denied["code"], "owner_only")
        granted = computer_access.change("grant", "202", name="A", scopes=["computer.files"], actor="101")
        self.assertTrue(granted["ok"])
        self.assertTrue(computer_access.allowed("202", "computer.files"))
        self.assertFalse(computer_access.allowed("202", "computer.process"))
        computer_access.change("revoke", "202", actor="101")
        self.assertFalse(computer_access.allowed("202", "computer.files"))

    @mock.patch("body_client.run_argv")
    @mock.patch("body_client.call")
    def test_inventory_writes_snapshot_and_device_card(self, status, run):
        import computer_inventory
        status.return_value = {"ok": True, "identity": {"kind": "interactive", "integrity": "high"}, "manifest": {}}
        payload = {"captured_at": "2026-07-13T05:00:00.0000000Z", "hostname": "PC", "user": "egor",
                   "os": {"caption": "Windows", "version": "11", "build": "1", "architecture": "64-bit"},
                   "machine": {"manufacturer": "X", "model": "Y", "memory_bytes": 8 * 1024**3},
                   "volumes": [], "tools": [], "apps": [{"name": "Word"}], "known_roots": ["C:\\Users\\egor"], "project_roots": []}
        run.return_value = {"ok": True, "stdout_tail": json.dumps(payload)}
        observed = dt.datetime(2026, 7, 13, 5, 0, tzinfo=dt.timezone.utc)
        with mock.patch("computer_inventory._utc_now", return_value=observed):
            result = computer_inventory.refresh("windows-pc")
        self.assertTrue(result["ok"])
        self.assertTrue((self.base / result["snapshot"]).is_file())
        self.assertIn("Installed applications", (self.base / result["card"]).read_text(encoding="utf-8"))
        self.assertFalse(computer_inventory.due(
            "windows-pc", now=dt.datetime(2026, 7, 13, 5, 30, tzinfo=dt.timezone.utc),
        ))
        self.assertTrue(computer_inventory.due(
            "windows-pc", now=dt.datetime(2026, 7, 14, 6, 0, tzinfo=dt.timezone.utc),
        ))


if __name__ == "__main__":
    unittest.main()
