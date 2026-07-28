"""One-way compatibility bridge for the retired PASS 12 moral-goals subsystem.

The old subsystem encoded a closed list of personality defects, imposed a
two-goal ceiling and let background evaluation promote those labels into live
prompt state.  PASS 24 deliberately removes that authority.  Praxis's current
intentions live in :mod:`desires`, whose statements are free text and whose
ordering is causal rather than moral.

This module stays small so an older live tree can be upgraded without losing
private history.  ``decommission_legacy`` atomically moves every legacy file
under ``memory/.archive/legacy-goals``.  Archived bytes remain available for
explicit provenance work but are outside automatic recall and generated maps.
The remaining functions are read-only compatibility shims for old callers.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path


BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
STATE_DIR = BASE / "memory" / ".state"
FREQ_FILE = STATE_DIR / "form_freq.json"
REVIEW_FILE = STATE_DIR / "goals_review.json"
GOALS_MD = BASE / "memory" / "goals.md"
RETIRED_MD = BASE / "memory" / "goals_retired.md"
ARCHIVE_DIR = BASE / "memory" / ".archive" / "legacy-goals"

# Public compatibility constants.  An empty vocabulary is intentional: no
# label is allowed to regain prompt or tool authority through an older caller.
FORMS: tuple[str, ...] = ()
FORM_RU: dict[str, str] = {}
FORM_CAP = 0

_FORM_TAG = re.compile(r"\[form:\s*[^\]]+\]", re.IGNORECASE)


def _archive_one(path: Path) -> str:
    if not path.is_file():
        return ""
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    target = ARCHIVE_DIR / f"{path.name}.{digest[:16]}"
    if target.exists():
        if target.read_bytes() != data:
            raise RuntimeError(f"legacy archive digest collision: {target}")
        path.unlink()
    else:
        os.replace(path, target)
    return target.relative_to(BASE).as_posix()


def decommission_legacy() -> dict[str, object]:
    """Archive legacy goal state exactly once and return a migration receipt."""
    moved = []
    for path in (GOALS_MD, RETIRED_MD, FREQ_FILE, REVIEW_FILE):
        archived = _archive_one(path)
        if archived:
            moved.append(archived)
    return {
        "ok": True,
        "migrated": moved,
        "replacement": "memory/desires/events.jsonl",
    }


def parse_form(_text: str) -> str:
    return "unknown"


def strip_form_tag(text: str) -> str:
    return _FORM_TAG.sub("", text or "").strip()


def record(_form: str, day: str | None = None) -> None:
    del day


def rollup(days: int = 7, now=None) -> dict:
    del days, now
    return {}


def top_form(days: int = 7, now=None) -> str:
    del days, now
    return ""


def load() -> list[dict]:
    return []


def active_forms() -> list[str]:
    return []


def set_goal(form: str, source: str = "self", baseline=None, now=None) -> tuple[bool, str]:
    del form, source, baseline, now
    return False, "Контур set_goal удалён; свободные намерения живут в manage_desire."


def maybe_self_raise(threshold: int = 3, now=None) -> str:
    del threshold, now
    return ""


def update_current(now=None) -> list[dict]:
    del now
    return []


def retire(form: str, reason: str = "", now=None) -> bool:
    del form, reason, now
    return False


def state_line() -> str:
    return ""


def weekly_review(now=None) -> list[str]:
    del now
    decommission_legacy()
    return []
