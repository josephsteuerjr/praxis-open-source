#!/usr/bin/env python3
"""One-way, idempotent migration of serverd-v1 host tasks into canonical Praxis Forge.

V1 kept hcode task metadata beside the privileged daemon.  Broker v2 has no task store, so a
rolling deploy imports those records into ``memory/.forge/tasks`` before the old directory becomes
read-only compatibility state.  Existing canonical tasks always win and legacy bytes are retained.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from pathlib import Path


TASK_ID = re.compile(r"^hcode-[A-Za-z0-9_-]{4,64}$")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _load(path: Path) -> dict:
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
        return row if isinstance(row, dict) else {}
    except (OSError, ValueError):
        return {}


def migrate(legacy_tasks: Path, forge_state: Path) -> dict:
    legacy_tasks = Path(legacy_tasks)
    target_tasks = Path(forge_state) / "tasks"
    report = {"ok": True, "migrated": [], "skipped": [], "invalid": []}
    if not legacy_tasks.is_dir():
        return report
    target_tasks.mkdir(parents=True, exist_ok=True)
    for source in sorted(legacy_tasks.glob("hcode-*")):
        old = _load(source / "task.json")
        task_id = str(old.get("id") or source.name)
        root = str(old.get("root") or old.get("target") or "")
        if not TASK_ID.fullmatch(task_id) or not Path(root).is_absolute():
            report["invalid"].append(task_id)
            continue
        target = target_tasks / task_id
        if (target / "task.json").is_file():
            report["skipped"].append(task_id)
            continue
        target.mkdir(parents=True, exist_ok=True)
        created = str(old.get("created") or _now())
        task = {
            "id": task_id,
            "goal": str(old.get("goal") or "migrated host task"),
            "target": str(old.get("target") or root),
            "source_root": root,
            "root": root,
            "scope": "host",
            "backend": "praxis.host.v2",
            "isolation": "host-direct",
            "cleanup": "none",
            "proposal_id": "",
            "branch": "",
            "base_commit": str(old.get("base_commit") or ""),
            "source_git": str(old.get("git_root") or old.get("source_git") or ""),
            "source_branch": "",
            "worktree_root": "",
            "status": str(old.get("status") or "active"),
            "created": created,
            "updated": str(old.get("updated") or created),
            "migrated_from": "praxis.host.v1",
        }
        orientation = source / "orientation.txt"
        if orientation.is_file():
            _write(target / "orientation.txt", orientation.read_text(encoding="utf-8", errors="replace"))
        events = ""
        if (source / "events.jsonl").is_file():
            events = (source / "events.jsonl").read_text(encoding="utf-8", errors="replace")
            if events and not events.endswith("\n"):
                events += "\n"
        event = {"at": _now(), "kind": "migrated_from_serverd_v1",
                 "legacy_path": str(source), "summary": "task ownership moved to canonical Forge"}
        _write(target / "events.jsonl", events + json.dumps(event, ensure_ascii=False) + "\n")
        # task.json is written last: its presence is the migration commit marker.
        _write(target / "task.json", json.dumps(task, ensure_ascii=False, indent=2) + "\n")
        report["migrated"].append(task_id)
    report["ok"] = not report["invalid"]
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy", type=Path, default=Path("/opt/praxis-serverd/state/tasks"))
    parser.add_argument("--forge", type=Path, default=Path("/opt/praxis/memory/.forge"))
    args = parser.parse_args()
    report = migrate(args.legacy, args.forge)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
