from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory_life


class ContinuityProvenanceTests(unittest.TestCase):
    def test_duplicate_compact_id_cannot_enter_continuity_prompt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="praxis_continuity_provenance_") as raw:
            base = Path(raw)
            memory = base / "memory"
            life = memory / "life"
            events = life / "events"
            compacts = life / "compacts"
            state = memory / ".state" / "life"
            event_id = "evt-20260714T120000000000Z-5ec7100d"
            event_file = events / "2026-07-14.jsonl"
            event_file.parent.mkdir(parents=True)
            event_file.write_text(
                json.dumps(
                    {
                        "schema": "praxis.life.event.v1",
                        "id": event_id,
                        "ts": "2026-07-14T12:00:00.000Z",
                        "kind": "conversation_message",
                        "stream": "7",
                        "chat_id": "7",
                        "actor": "Participant",
                        "direction": "in",
                        "text": "primary conversation evidence",
                        "source": "telegram",
                        "source_id": "77",
                        "salience": 2,
                        "refs": [],
                        "meta": {},
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            compact_id = "cmp-20260714T120000000000Z-5ec7100d"
            compact = compacts / "7" / f"{compact_id}.md"
            compact.parent.mkdir(parents=True)
            meta = {
                "schema": "praxis.life.compact.v1",
                "id": compact_id,
                "chat_id": "7",
                "created_at": "2026-07-14T12:00:00.000Z",
                "tier": 1,
                "depth": 1,
                "preservation_priority": 1.0,
                "source_event_ids": [event_id],
                "source_compact_ids": [],
                "event_count": 1,
                "continued": False,
                "legacy": False,
                "degraded": False,
                "first_ts": "2026-07-14T12:00:00.000Z",
                "last_ts": "2026-07-14T12:00:00.000Z",
                "path": compact.relative_to(base).as_posix(),
            }
            compact.write_text(
                f"<!-- praxis-compact: {json.dumps(meta, ensure_ascii=False, separators=(',', ':'))} -->\n"
                f"# Compact {compact_id}\n\n## Суть\nCONTINUITY_CANONICAL_MARKER\n",
                encoding="utf-8",
            )

            with mock.patch.multiple(
                memory_life,
                BASE=base,
                MEM_DIR=memory,
                LIFE_DIR=life,
                EVENTS_DIR=events,
                COMPACTS_DIR=compacts,
                STATE_DIR=state,
            ):
                self.assertIn(
                    "CONTINUITY_CANONICAL_MARKER", memory_life.context_summary("7")
                )

                duplicate = compacts / "shadow" / compact.name
                duplicate.parent.mkdir(parents=True)
                duplicate.write_bytes(compact.read_bytes())
                self.assertNotIn(
                    "CONTINUITY_CANONICAL_MARKER", memory_life.context_summary("7")
                )


if __name__ == "__main__":
    unittest.main()
