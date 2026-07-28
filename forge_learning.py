"""Evidence-backed lessons for completed Praxis Forge tasks.

Lessons are compact operational records, not free-form self-congratulation.  They bind the goal,
changed files, failed/successful probes and a reusable repair heuristic so a later task can recall
the exact evidence that should change its plan.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid
from pathlib import Path
from typing import Iterable


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _tokens(text: str) -> set[str]:
    return {token.casefold() for token in re.findall(r"[A-Za-zА-Яа-яЁё0-9_.-]{3,}", text or "")}


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass


def _read(path: Path, limit: int = 1000) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                out.append(row)
        except ValueError:
            continue
    return out


def lesson_path(state_dir: Path) -> Path:
    return state_dir / "lessons.jsonl"


def recall(state_dir: Path, goal: str, root: Path, *, limit: int = 5) -> list[dict]:
    wanted = _tokens(goal) | _tokens(root.name)
    scored: list[tuple[float, dict]] = []
    for row in _read(lesson_path(state_dir)):
        hay = _tokens(" ".join([
            str(row.get("goal") or ""), str(row.get("lesson") or ""),
            " ".join(row.get("changed") or []), " ".join(row.get("tags") or []),
        ]))
        overlap = len(wanted & hay)
        union = max(1, len(wanted | hay))
        score = overlap / union
        if root.name and row.get("project") == root.name:
            score += .35
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda item: (item[0], item[1].get("at", "")), reverse=True)
    return [{**row, "score": round(score, 3)} for score, row in scored[:max(1, min(limit, 20))]]


def record(state_dir: Path, *, task: dict, root: Path, events: Iterable[dict],
           changed: list[str], verification: dict | None = None, lesson: str = "",
           regression: str = "", outcome: str = "") -> dict:
    event_rows = list(events)
    commands = []
    failures = []
    for event in event_rows:
        if event.get("kind") != "command":
            continue
        item = {"command": event.get("command", ""), "status": event.get("status", ""),
                "exit": event.get("code"), "duration_s": event.get("duration_s"),
                "log": event.get("log", "")}
        commands.append(item)
        if event.get("status") not in {"ok", "passed"}:
            failures.append(item)
    matrix = (verification or {}).get("checks") or []
    failures.extend(row for row in matrix if row.get("status") not in {"ok", "passed"})
    explicit = str(lesson or "").strip()
    if not explicit:
        if failures:
            names = ", ".join(str(row.get("id") or row.get("command") or "probe")[:100]
                              for row in failures[:4])
            explicit = f"Ранний probe должен был поймать: {names}. Сохранять targeted-проверку до полного gate."
        elif changed:
            explicit = "Targeted probes и итоговый gate согласились; повторять тот же evidence-path для сходных изменений."
        else:
            explicit = "Задача завершилась без файлового diff; сначала проверять, нужен ли кодовый change вообще."
    row = {
        "id": "lesson-" + uuid.uuid4().hex[:10], "at": _now(), "task_id": task.get("id"),
        "project": root.name, "root": str(root), "goal": task.get("goal", ""),
        "outcome": outcome or task.get("status", ""), "changed": changed[:200],
        "lesson": explicit, "regression": str(regression or "").strip(),
        "commands": commands[-30:], "verification": matrix[-30:],
        "failures": failures[-20:],
        "tags": sorted(_tokens(" ".join([str(task.get("goal") or ""), *changed])))[:80],
    }
    _append(lesson_path(state_dir), row)
    return row


def format_recall(rows: list[dict]) -> str:
    if not rows:
        return "Для этой задачи похожих проверяемых уроков пока нет."
    parts = []
    for row in rows:
        parts.append(
            f"{row.get('id')} score={row.get('score', 1)} · {row.get('project')} · {row.get('goal')}\n"
            f"урок: {row.get('lesson')}\nрегрессия: {row.get('regression') or 'не записана'}"
        )
    return "\n\n".join(parts)
