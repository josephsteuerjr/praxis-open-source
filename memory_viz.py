"""Read-only projection of the PASS 19 life memory DAG for the mini-app.

Strictly a *reader*.  This module never writes a byte, never calls an LLM and never
mutates runtime state:

  * ``memory_life`` is imported for constants only (BASE / directory layout / ``_safe``);
    none of its writing helpers (``_load_state``, ``rebuild_state``, ``stream_status``,
    ``compact_if_due`` …) are called, because they persist a rebuilt cursor as a side
    effect.  The state cache is parsed directly from JSON instead.
  * Every parse is wrapped: one corrupt file yields an honest partial result, never a 500.
  * Raw events are *counted*, never loaded — 169k JSONL lines stay on disk.

Work is bounded, not just the payload.  Artifact ids are
``<prefix>-%Y%m%dT%H%M%S%fZ-<hex>`` (``memory_life._id``) and the file name is
``<id>.md``, so the *file name* sorts chronologically without opening anything.  The
census pass therefore reads only the **first line** of each artifact (``_meta_only``:
that is where the whole header lives) and a full ``read_text`` — the 2 MB one, with the
recap regex — is paid only for the ≤ ``MAX_*`` newest survivors that actually need
``summary`` / ``title``.

Public API::

    compact_dag()                -> {"chats": [...], "totals": {...}, "truncated": bool,
                                     "degraded": bool}
    compact_dag("<chat_id>")     -> {"chat_id", "compacts", "episodes", "edges",
                                     "frontier", "totals", "hot", "truncated", "degraded"}

``degraded`` is ``True`` only when the answer could not be *read* — it is never the
same thing as a memory that is genuinely empty, and such a payload is never cached.

Edge direction (both kinds point leaf-ward → root-ward):
    {"from": <source/lower-tier compact id>, "to": <compact that folded it>, "kind": "compact_of"}
    {"from": <episode id>,                   "to": <its compact id>,         "kind": "episode_of"}
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from pathlib import Path

try:  # constants only — no writer of memory_life is ever invoked from here
    import memory_life as _life
except Exception:  # pragma: no cover - reader must survive a broken import
    _life = None

__all__ = ["compact_dag", "invalidate"]

MAX_COMPACTS = 400
MAX_EPISODES = 400
SUMMARY_CHARS = 400
CACHE_TTL_SEC = 30.0
_CACHE_MAX_ENTRIES = 32
_FILE_MAX_BYTES = 2_000_000
# The header is one line.  This bound only ever bites on a pathological file, and it
# is set far above any real header (a fold of ~8k event ids) so nothing readable is
# mistaken for unreadable.
_META_LINE_MAX_BYTES = 1_000_000

_META_RE = re.compile(r"^<!--\s*praxis-(compact|episode):\s*(\{.*\})\s*-->$")
_RECAP_RE = re.compile(r"^## Суть\s*$\n(.*?)(?=^## |\Z)", re.M | re.S)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)
_CHAT_KEY_RE = re.compile(r"^[\w-]{1,120}$")

_LOCK = threading.Lock()
_RESULT_CACHE: dict[tuple, tuple[float, dict]] = {}
_LINE_CACHE: dict[str, tuple[tuple[int, int], int]] = {}

# Two artifact caches, kept apart on purpose (they have very different shapes).
#   _META_CACHE — one small header dict per artifact, from the first line only.  The
#     census touches every file, so this must comfortably outsize the whole corpus:
#     once it evicts, every request re-walks everything.
#   _ROW_CACHE  — the fat rows (recap / title), only ever populated for the ≤ MAX_*
#     survivors of a handful of chats, so a far smaller cap is already generous.
_META_CACHE: dict[str, tuple[tuple[int, int], dict]] = {}
_META_CACHE_MAX = 40000
_ROW_CACHE: dict[str, tuple[tuple[int, int], dict]] = {}
_ROW_CACHE_MAX = 8000

# One computation per cache key: without this a cold key under N concurrent requests
# ran the whole walk N times (the global lock is released before computing).
_KEY_LOCKS: dict[tuple, threading.Lock] = {}
_KEY_LOCK_MAX = 256


# --------------------------------------------------------------------------- #
#  Layout (constants borrowed from memory_life, with a standalone fallback)
# --------------------------------------------------------------------------- #

def _base() -> Path:
    if _life is not None:
        try:
            return Path(_life.BASE)
        except Exception:
            pass
    return Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)


def _dirs() -> dict[str, Path]:
    base = _base()
    if _life is not None:
        try:
            return {
                "events": Path(_life.EVENTS_DIR),
                "compacts": Path(_life.COMPACTS_DIR),
                "episodes": Path(_life.EPISODES_DIR),
                "state": Path(_life.STATE_DIR),
            }
        except Exception:
            pass
    mem = base / "memory"
    return {
        "events": mem / "life" / "events",
        "compacts": mem / "life" / "compacts",
        "episodes": mem / "life" / "episodes",
        "state": mem / ".state" / "life",
    }


def _safe(chat_id) -> str:
    if _life is not None:
        try:
            return _life._safe(chat_id)
        except Exception:
            pass
    return re.sub(r"[^\w-]", "_", str(chat_id)) or "chat"


def _epoch(value) -> float:
    if _life is not None:
        try:
            return float(_life._epoch(value))
        except Exception:
            pass
    try:
        import datetime as _dt

        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- #
#  Primitive readers — each one is total: it returns a default, never raises
# --------------------------------------------------------------------------- #

def _read_text(path: Path) -> str:
    try:
        if path.stat().st_size > _FILE_MAX_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _meta_from_line(line: str, kind: str) -> dict:
    match = _META_RE.match(line or "")
    if not match or match.group(1) != kind:
        return {}
    try:
        data = json.loads(match.group(2))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _recap(text: str) -> str:
    match = _RECAP_RE.search(text or "")
    body = (match.group(1) if match else "").strip()
    if len(body) > SUMMARY_CHARS:
        body = body[: SUMMARY_CHARS - 1].rstrip() + "…"
    return body


def _title(text: str) -> str:
    for match in _H1_RE.finditer(text or ""):
        title = match.group(1).strip()
        if title:
            return title[:200]
    return ""


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _int(value) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except Exception:
            return 0
    return 0


def _bool(value) -> bool:
    return bool(value) if isinstance(value, (bool, int, float)) else False


def _id_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, str) and x]


# --------------------------------------------------------------------------- #
#  Cheap event-spine line counting (cached by path/mtime/size)
# --------------------------------------------------------------------------- #

def _count_lines(path: Path) -> int:
    try:
        st = path.stat()
        stamp = (int(st.st_mtime_ns), int(st.st_size))
    except Exception:
        return 0
    key = str(path)
    with _LOCK:
        cached = _LINE_CACHE.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    total = 0
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                total += chunk.count(b"\n")
    except Exception:
        return cached[1] if cached else 0
    with _LOCK:
        _LINE_CACHE[key] = (stamp, total)
        if len(_LINE_CACHE) > 512:
            for stale in list(_LINE_CACHE)[: len(_LINE_CACHE) - 512]:
                _LINE_CACHE.pop(stale, None)
    return total


def _event_total(events_dir: Path) -> int:
    try:
        paths = sorted(events_dir.glob("*.jsonl")) if events_dir.is_dir() else []
    except Exception:
        return 0
    return sum(_count_lines(p) for p in paths)


# --------------------------------------------------------------------------- #
#  Chat-level scans
# --------------------------------------------------------------------------- #

def _md_files(directory: Path) -> list[Path]:
    """``*.md`` artifacts **sorted by file name — which is chronological order**.

    ``memory_life._id`` (l. 80-82) builds ids as
    ``<prefix>-%Y%m%dT%H%M%S%fZ-<hex>`` with a fixed-width 12-digit time field, and the
    file is written as ``<id>.md``.  A lexicographic sort of the names is therefore a
    sort by creation time, obtained without opening a single file — which is what lets
    the caller slice the newest N *before* parsing anything.
    """
    try:
        if not directory.is_dir():
            return []
        return sorted((p for p in directory.glob("*.md") if p.is_file()),
                      key=lambda p: p.name)
    except Exception:
        return []


def _meta_only(path: Path, kind: str) -> dict:
    """The artifact header, parsed from the **first line alone**.

    ``memory_life`` writes the whole metadata block as a single
    ``<!-- praxis-compact: {...} -->`` / ``<!-- praxis-episode: {...} -->`` comment on
    line 1, so learning ``tier`` (or merely whether a file is parseable) costs one
    ``readline`` — not a ``read_text`` of up to 2 MB plus a recap regex.  Memoised per
    (path, mtime, size); artifacts are immutable once written, so a hit is current.
    """
    try:
        st = path.stat()
        stamp = (int(st.st_mtime_ns), int(st.st_size))
    except Exception:
        return {}
    key = f"{kind}:{path}"
    with _LOCK:
        hit = _META_CACHE.get(key)
    if hit and hit[0] == stamp:
        return dict(hit[1])
    try:
        with path.open("rb") as fh:
            raw = fh.readline(_META_LINE_MAX_BYTES)
        meta = _meta_from_line(raw.decode("utf-8", "ignore").strip(), kind)
    except Exception:
        meta = {}
    with _LOCK:
        _META_CACHE[key] = (stamp, meta)
        if len(_META_CACHE) > _META_CACHE_MAX:
            for stale in list(_META_CACHE)[: len(_META_CACHE) - _META_CACHE_MAX]:
                _META_CACHE.pop(stale, None)
    return dict(meta)


def _scan(directory: Path, kind: str, limit: int) -> dict:
    """Cheap chronological census of one artifact directory.

    Returns every parseable header (``parsed``) plus the newest ``limit`` of them
    (``survivors``) for the caller to read in full.  The overview and the detail view
    both go through here, so the number on the chip and the number of nodes in the
    drawing can no longer disagree: one predicate — a header that parses — decides both.
    """
    parsed: list[tuple[Path, dict]] = []
    unreadable = 0
    for path in _md_files(directory):          # chronological; no I/O beyond the listing
        meta = _meta_only(path, kind)
        if meta:
            parsed.append((path, meta))
        else:
            unreadable += 1
    total = len(parsed)
    return {
        "parsed": parsed,
        "survivors": parsed[-limit:] if 0 <= limit < total else parsed,
        "total": total,
        "unreadable": unreadable,
        "truncated": total > limit >= 0,
    }


def _state_of(state_dir: Path, chat_key: str) -> dict:
    path = state_dir / f"{chat_key}.json"
    try:
        if not path.is_file() or path.stat().st_size > _FILE_MAX_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _hot_count(state: dict) -> int:
    hot = state.get("hot")
    return len(hot) if isinstance(hot, list) else 0


def _frontier_ids(state: dict) -> list[str]:
    rows = state.get("frontier")
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            cid = row.get("id")
            if isinstance(cid, str) and cid:
                out.append(cid)
        elif isinstance(row, str) and row:
            out.append(row)
    return out


def _chat_keys(dirs: dict[str, Path]) -> list[str]:
    keys: set[str] = set()
    for kind in ("compacts", "episodes"):
        try:
            root = dirs[kind]
            if root.is_dir():
                keys.update(p.name for p in root.iterdir() if p.is_dir())
        except Exception:
            continue
    try:
        state_dir = dirs["state"]
        if state_dir.is_dir():
            for path in state_dir.glob("*.json"):
                if path.name in ("formation.json", "formation.request.json"):
                    continue
                keys.add(path.stem)
    except Exception:
        pass
    return sorted(k for k in keys if _CHAT_KEY_RE.match(k or ""))


def _chat_overview(dirs: dict[str, Path], chat_key: str) -> dict:
    """One row per chat.  Header reads only — no artifact body is ever touched here."""
    compacts = _scan(dirs["compacts"] / chat_key, "compact", -1)
    episodes = _scan(dirs["episodes"] / chat_key, "episode", -1)
    tiers: dict[str, int] = {}
    for _path, meta in compacts["parsed"]:
        tier = str(max(1, _int(meta.get("tier")) or 1))
        tiers[tier] = tiers.get(tier, 0) + 1
    state = _state_of(dirs["state"], chat_key)
    return {
        "chat_id": chat_key,
        "compacts": compacts["total"],
        # counted the same way `_chat_dag` counts its nodes: a parseable header
        "episodes": episodes["total"],
        "hot": _hot_count(state),
        "tiers": dict(sorted(tiers.items(), key=lambda kv: int(kv[0]))),
        "unreadable": compacts["unreadable"],
        "frontier_count": len(_frontier_ids(state)),
    }


def _compact_row(path: Path) -> dict:
    text = _read_text(path)
    meta = _meta_from_line(text.split("\n", 1)[0].strip() if text else "", "compact")
    if not meta:
        return {}
    cid = _str(meta.get("id")) or path.stem
    sources = _id_list(meta.get("source_compact_ids"))
    return {
        "id": cid,
        "tier": max(1, _int(meta.get("tier")) or 1),
        "depth": max(1, _int(meta.get("depth")) or 1),
        "created_at": _str(meta.get("created_at")),
        "event_count": _int(meta.get("event_count")),
        "continued": _bool(meta.get("continued")),
        "legacy": _bool(meta.get("legacy")),
        "degraded": _bool(meta.get("degraded")),
        "first_ts": _str(meta.get("first_ts")),
        "last_ts": _str(meta.get("last_ts")),
        "source_compact_ids": sources,
        "source_event_count": len(_id_list(meta.get("source_event_ids"))),
        "summary": _recap(text),
    }


def _episode_row(path: Path) -> dict:
    text = _read_text(path)
    meta = _meta_from_line(text.split("\n", 1)[0].strip() if text else "", "episode")
    if not meta:
        return {}
    return {
        "id": _str(meta.get("id")) or path.stem,
        "compact_id": _str(meta.get("compact_id")),
        "status": _str(meta.get("status")) or "closed",
        "first_ts": _str(meta.get("first_ts")),
        "last_ts": _str(meta.get("last_ts")),
        "title": _title(text),
        "created_at": _str(meta.get("created_at")),
    }


def _cached_row(path: Path, kind: str) -> dict:
    """Parsed artifact row, memoised per (path, mtime, size).

    Compacts and episodes are immutable once written, so a hit is always current;
    a rewritten or replaced file changes mtime/size and is re-read.  Callers get a
    shallow copy, so nothing they do can poison the cache.
    """
    try:
        st = path.stat()
        stamp = (int(st.st_mtime_ns), int(st.st_size))
    except Exception:
        return {}
    key = f"{kind}:{path}"
    with _LOCK:
        hit = _ROW_CACHE.get(key)
    if hit and hit[0] == stamp:
        return dict(hit[1])
    try:
        row = _compact_row(path) if kind == "compact" else _episode_row(path)
    except Exception:
        row = {}
    with _LOCK:
        _ROW_CACHE[key] = (stamp, row)
        if len(_ROW_CACHE) > _ROW_CACHE_MAX:
            for stale in list(_ROW_CACHE)[: len(_ROW_CACHE) - _ROW_CACHE_MAX]:
                _ROW_CACHE.pop(stale, None)
    return dict(row)


def _order_key(row: dict) -> tuple[float, str]:
    stamp = _epoch(row.get("created_at")) or _epoch(row.get("last_ts")) or _epoch(row.get("first_ts"))
    return (stamp, str(row.get("id") or ""))


# --------------------------------------------------------------------------- #
#  Public surface
# --------------------------------------------------------------------------- #

def _overview(dirs: dict[str, Path]) -> dict:
    chats = []
    degraded = False
    for key in _chat_keys(dirs):
        try:
            chats.append(_chat_overview(dirs, key))
        except Exception:
            # A chat we could not read is a hole in the answer, and it must say so.
            degraded = True
            chats.append({"chat_id": key, "compacts": 0, "episodes": 0, "hot": 0,
                          "tiers": {}, "unreadable": 0, "frontier_count": 0})
    chats.sort(key=lambda c: (-int(c.get("compacts") or 0), str(c.get("chat_id"))))
    return {
        "chats": chats,
        "totals": {
            "events": _event_total(dirs["events"]),
            "compacts": sum(int(c.get("compacts") or 0) for c in chats),
            "episodes": sum(int(c.get("episodes") or 0) for c in chats),
        },
        "truncated": False,
        "degraded": degraded,
    }


def _chat_dag(dirs: dict[str, Path], chat_key: str) -> dict:
    # Census first (first lines only), then the cap, then the expensive read: the
    # newest MAX_* survivors are the only artifacts whose body is ever loaded.
    compacts = _scan(dirs["compacts"] / chat_key, "compact", MAX_COMPACTS)
    episodes = _scan(dirs["episodes"] / chat_key, "episode", MAX_EPISODES)

    compact_rows = [row for row in (_cached_row(p, "compact") for p, _m in compacts["survivors"])
                    if row]
    episode_rows = [row for row in (_cached_row(p, "episode") for p, _m in episodes["survivors"])
                    if row]

    total_compacts = compacts["total"]
    total_episodes = episodes["total"]
    compact_rows.sort(key=_order_key, reverse=True)
    episode_rows.sort(key=_order_key, reverse=True)
    truncated = compacts["truncated"] or episodes["truncated"]

    compact_ids = {row["id"] for row in compact_rows}
    edges: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for row in compact_rows:
        for parent in row.get("source_compact_ids") or []:
            # leaf → root: the folded (lower-tier) compact points at the one that ate it
            key = (parent, row["id"], "compact_of")
            if parent in compact_ids and key not in seen:
                seen.add(key)
                edges.append({"from": parent, "to": row["id"], "kind": "compact_of"})
    for row in episode_rows:
        parent = row.get("compact_id") or ""
        key = (row["id"], parent, "episode_of")
        if parent in compact_ids and key not in seen:
            seen.add(key)
            edges.append({"from": row["id"], "to": parent, "kind": "episode_of"})

    state = _state_of(dirs["state"], chat_key)
    return {
        "chat_id": chat_key,
        "compacts": compact_rows,
        "episodes": episode_rows,
        "edges": edges,
        "frontier": _frontier_ids(state),
        "hot": _hot_count(state),
        "totals": {
            "events": _event_total(dirs["events"]),
            "compacts": total_compacts,
            "episodes": total_episodes,
        },
        "truncated": truncated,
        "degraded": False,
    }


def _key_lock(cache_key: tuple) -> threading.Lock:
    """The lock guarding one cache key's computation (created on demand)."""
    with _LOCK:
        lock = _KEY_LOCKS.get(cache_key)
        if lock is None:
            lock = _KEY_LOCKS[cache_key] = threading.Lock()
        if len(_KEY_LOCKS) > _KEY_LOCK_MAX:
            for stale, other in list(_KEY_LOCKS.items()):
                if len(_KEY_LOCKS) <= _KEY_LOCK_MAX:
                    break
                if stale != cache_key and not other.locked():
                    _KEY_LOCKS.pop(stale, None)
        return lock


def _degraded(chat_key: str | None) -> dict:
    """The payload for *could not read*, which is not the payload for *nothing there*."""
    totals = {"events": 0, "compacts": 0, "episodes": 0}
    if chat_key is None:
        return {"chats": [], "totals": totals, "truncated": False, "degraded": True}
    return {"chat_id": chat_key, "compacts": [], "episodes": [], "edges": [],
            "frontier": [], "hot": 0, "totals": totals, "truncated": False,
            "degraded": True}


def compact_dag(chat_id: str | None = None) -> dict:
    """Read-only snapshot of the compression DAG.

    Without ``chat_id`` — one row per chat stream.  With one — the capped
    compact/episode node lists plus their edges, the current frontier and totals.
    The whole result is memoised for ``CACHE_TTL_SEC`` so a hot Telegram poller
    loop is never blocked by a filesystem walk, and a cold key is computed once
    however many callers ask for it at the same moment.  A payload that failed to
    read carries ``degraded: True`` and is deliberately **not** cached.
    """
    if chat_id is None or str(chat_id).strip() == "":
        chat_key = None
    else:
        chat_key = _safe(str(chat_id).strip())
        if not _CHAT_KEY_RE.match(chat_key):
            raise ValueError("bad chat id")

    dirs = _dirs()
    cache_key = (str(_base()), chat_key)
    now = time.monotonic()
    with _LOCK:
        hit = _RESULT_CACHE.get(cache_key)
        if hit and now - hit[0] < CACHE_TTL_SEC:
            return copy.deepcopy(hit[1])

    with _key_lock(cache_key):
        # Re-check: the thread we queued behind has very likely just filled the cache.
        now = time.monotonic()
        with _LOCK:
            hit = _RESULT_CACHE.get(cache_key)
            if hit and now - hit[0] < CACHE_TTL_SEC:
                return copy.deepcopy(hit[1])

        try:
            data = _overview(dirs) if chat_key is None else _chat_dag(dirs, chat_key)
        except Exception:
            # A read that failed is not a memory that is empty.  Say which one it is,
            # and never let the failure sit in the cache asserting it for 30 s.
            return _degraded(chat_key)
        if data.get("degraded"):
            return data

        with _LOCK:
            _RESULT_CACHE[cache_key] = (now, data)
            if len(_RESULT_CACHE) > _CACHE_MAX_ENTRIES:
                for stale, _payload in sorted(_RESULT_CACHE.items(), key=lambda kv: kv[1][0])[:8]:
                    _RESULT_CACHE.pop(stale, None)
        return copy.deepcopy(data)


def invalidate() -> None:
    """Drop the memoised results (tests / manual refresh). Touches no file."""
    with _LOCK:
        _RESULT_CACHE.clear()
        _LINE_CACHE.clear()
        _META_CACHE.clear()
        _ROW_CACHE.clear()


if __name__ == "__main__":  # read-only CLI probe
    import sys

    print(json.dumps(compact_dag(sys.argv[1] if len(sys.argv) > 1 else None),
                     ensure_ascii=False, indent=1))
