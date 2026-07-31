"""Evidence memory for operations performed through a Praxis execution body.

The Windows body remains brainless.  This server-side module records compact,
append-only observations and full private receipts, renders grep-friendly Markdown
cards, and maintains a rebuildable SQLite index.  JSONL/receipts are evidence;
SQLite is only a query accelerator; one completed Forge task may be promoted to one
life event by :func:`finish_task`.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "praxis.computer.observation.v1"
INDEX_SCHEMA = "praxis.computer.index.v2"
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "in_doubt", "error"}
_LOCK = threading.RLock()
log = logging.getLogger("praxis.computer-memory")
_SECRET_KEYS = {
    "authorization", "cookie", "credential", "password", "secret", "token",
    "api_key", "apikey", "private_key",
}
_PAYLOAD_KEYS = {"content", "patch", "old", "new", "text", "body", "stdout_tail", "stderr_tail"}


def _base() -> Path:
    return Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)


def _computer_dir() -> Path:
    return _base() / "memory" / "computer"


def _events_dir() -> Path:
    return _computer_dir() / "events"


def _db_path() -> Path:
    return _base() / "memory" / ".state" / "computer" / "observations.sqlite3"


def _event_files() -> list[Path]:
    return sorted(_events_dir().glob("*.jsonl")) if _events_dir().exists() else []


def _canonical_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in _event_files():
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _forge_task_dir(task_id: str) -> Path:
    # A Windows observation may belong either to the canonical Forge task or to a general
    # PASS 24 run (chat exploration, heartbeat, task window).  Keep non-Forge evidence inside
    # its run instead of manufacturing a second/ghost Forge task store.
    runs = _base() / "memory" / "runs"
    if str(task_id).startswith("run-") and runs.exists():
        matches = list(runs.glob(f"*/{task_id}/manifest.json"))
        if len(matches) == 1:
            return matches[0].parent / "computer"
    return _base() / "memory" / ".forge" / "tasks" / task_id


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _observation_id() -> str:
    now = dt.datetime.now(dt.timezone.utc)
    return f"obs-{now:%Y%m%dT%H%M%S%fZ}-{uuid.uuid4().hex[:8]}"


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-.")[:120] or "unknown"


def _sha_text(value: str) -> dict[str, Any]:
    raw = str(value or "").encode("utf-8", "replace")
    return {"omitted": True, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _rel(path: Path) -> str:
    try:
        return path.relative_to(_base()).as_posix()
    except ValueError:
        return path.as_posix()


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _write_json_atomic(path: Path, value: dict) -> None:
    _write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(path), flags, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _log_payload(task_id: str, observation_id: str, key: str, value: str) -> dict[str, Any]:
    meta = _sha_text(value)
    if not task_id or not value:
        return meta
    path = _forge_task_dir(task_id) / "receipts" / "logs" / f"{observation_id}-{_safe(key)}.log"
    _write_text_atomic(path, str(value))
    meta["log"] = _rel(path)
    return meta


def _scrub(value: Any, *, task_id: str, observation_id: str, key: str = "") -> Any:
    low = key.casefold()
    if any(part in low for part in _SECRET_KEYS):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _scrub(v, task_id=task_id, observation_id=observation_id, key=str(k))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, task_id=task_id, observation_id=observation_id, key=key) for v in value]
    if isinstance(value, str) and low in _PAYLOAD_KEYS:
        if low in {"body", "stdout_tail", "stderr_tail"}:
            return _log_payload(task_id, observation_id, low, value)
        return _sha_text(value)
    if isinstance(value, str) and len(value) > 4000:
        return _sha_text(value)
    return value


def _walk_dicts(value: Any) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _artifacts(result: dict) -> list[dict]:
    found: dict[str, dict] = {}
    for row in _walk_dicts(result):
        sha = str(row.get("sha256") or "").casefold()
        is_artifact = any(row.get(key) is not None for key in ("name", "mime", "source_device"))
        if is_artifact and re.fullmatch(r"[0-9a-f]{64}", sha) and row.get("size") is not None:
            found[sha] = {k: row.get(k) for k in ("sha256", "size", "name", "mime", "source_device")
                          if row.get(k) is not None}
    return list(found.values())


def _hashes(result: dict) -> list[str]:
    out = []
    for row in _walk_dicts(result):
        for key in ("sha256", "before_sha256", "after_sha256", "before", "after"):
            value = str(row.get(key) or "").casefold()
            if re.fullmatch(r"[0-9a-f]{16,64}", value) and value not in out:
                out.append(value)
    return out[:20]


def _status(result: dict) -> str:
    explicit = str(result.get("status") or "").strip().lower()
    if explicit:
        return explicit
    kind = str(result.get("type") or "").strip().lower()
    if kind == "accepted":
        return "accepted"
    if result.get("ok") is False:
        return str(result.get("code") or "failed").lower()
    return "succeeded"


def _subject(context: dict, capability: str, args: dict) -> str:
    for key in ("path", "root", "cwd", "operation_id", "program"):
        if args.get(key):
            return str(args[key])[:260]
    if args.get("command"):
        return str(args["command"]).replace("\n", " ")[:260]
    return str(context.get("root") or capability)[:260]


def _summary(capability: str, status: str, subject: str, hashes: list[str], artifacts: list[dict]) -> str:
    tail = []
    if hashes:
        tail.append("hash=" + hashes[-1][:16])
    if artifacts:
        tail.append(f"artifacts={len(artifacts)}")
    suffix = "; " + ", ".join(tail) if tail else ""
    return f"{capability} {status}: {subject}{suffix}"[:700]


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.execute("PRAGMA busy_timeout=15000")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS observations (
            id TEXT PRIMARY KEY,
            at TEXT NOT NULL,
            task_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            root TEXT NOT NULL,
            goal TEXT NOT NULL,
            execution TEXT NOT NULL,
            capability TEXT NOT NULL,
            status TEXT NOT NULL,
            subject TEXT NOT NULL,
            summary TEXT NOT NULL,
            receipt TEXT NOT NULL,
            hashes_json TEXT NOT NULL,
            artifacts_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS observations_task_at ON observations(task_id, at DESC);
        CREATE INDEX IF NOT EXISTS observations_device_at ON observations(device_id, at DESC);
        CREATE INDEX IF NOT EXISTS observations_capability_at ON observations(capability, at DESC);
        CREATE INDEX IF NOT EXISTS observations_status_at ON observations(status, at DESC);
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    return db


def _index_meta() -> dict[str, str]:
    path = _db_path()
    if not path.exists():
        return {}
    try:
        with contextlib.closing(sqlite3.connect(path, timeout=15)) as db:
            return {str(key): str(value) for key, value in db.execute(
                "SELECT key, value FROM meta"
            )}
    except sqlite3.Error:
        return {}


def _write_index_meta() -> None:
    with contextlib.closing(_connect()) as db:
        db.executemany(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (("schema", INDEX_SCHEMA), ("fingerprint", _canonical_fingerprint())),
        )
        db.commit()


def _ensure_index() -> bool:
    with _LOCK:
        meta = _index_meta()
        if (meta.get("schema") != INDEX_SCHEMA
                or meta.get("fingerprint") != _canonical_fingerprint()):
            rebuild_index()
            return True
    return False


def _index(row: dict) -> None:
    with contextlib.closing(_connect()) as db:
        db.execute(
            """INSERT OR REPLACE INTO observations
               (id, at, task_id, device_id, root, goal, execution, capability, status, subject,
                summary, receipt, hashes_json, artifacts_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["id"], row["at"], row["task_id"], row["device_id"], row["root"],
             row.get("goal") or "", row["execution"], row["capability"], row["status"], row["subject"],
             row["summary"], row["receipt"], json.dumps(row["hashes"]),
             json.dumps(row["artifacts"], ensure_ascii=False)),
        )
        db.commit()


def _rows(where: str = "1=1", params: tuple = (), limit: int = 80) -> list[dict]:
    with _LOCK:
        _ensure_index()
        with contextlib.closing(_connect()) as db:
            db.row_factory = sqlite3.Row
            selected = db.execute(
                f"SELECT * FROM observations WHERE {where} "  # noqa: S608
                "ORDER BY at DESC, id DESC LIMIT ?",
                (*params, max(1, min(int(limit), 1000))),
            ).fetchall()
    return [dict(row) for row in selected]


def _group_rows(column: str, limit: int) -> list[dict]:
    if column not in {"device_id", "task_id"}:
        raise ValueError("unsupported map column")
    with _LOCK:
        _ensure_index()
        with contextlib.closing(_connect()) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                f"SELECT {column} AS name, MAX(at) AS updated "  # noqa: S608
                f"FROM observations WHERE {column} <> '' GROUP BY {column} "
                "ORDER BY updated DESC, name ASC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
    return [dict(row) for row in rows]


def _latest_rows(column: str, limit: int) -> list[dict]:
    if column not in {"device_id", "task_id"}:
        raise ValueError("unsupported map column")
    with _LOCK:
        _ensure_index()
        with contextlib.closing(_connect()) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                f"SELECT * FROM (SELECT observations.*, "  # noqa: S608
                f"ROW_NUMBER() OVER (PARTITION BY {column} ORDER BY at DESC, id DESC) AS rn "
                f"FROM observations WHERE {column} <> '') "
                "WHERE rn = 1 ORDER BY at DESC, id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
    return [dict(row) for row in rows]


def search(query: str = "", *, task_id: str = "", device_id: str = "", limit: int = 80) -> list[dict]:
    clauses, params = [], []
    if task_id:
        clauses.append("task_id = ?"); params.append(task_id)
    if device_id:
        clauses.append("device_id = ?"); params.append(device_id)
    if query:
        clauses.append("(summary LIKE ? OR subject LIKE ? OR capability LIKE ?)")
        needle = f"%{query}%"; params.extend([needle, needle, needle])
    return _rows(" AND ".join(clauses) or "1=1", tuple(params), limit)


def recent_artifact_evidence(limit: int = 3) -> list[dict]:
    """Return bounded durable artifact facts, independent of delivery state."""
    rows = _rows("artifacts_json <> '[]'", (), max(1, min(int(limit), 10)))
    evidence = []
    for row in rows:
        try:
            artifacts = json.loads(row.get("artifacts_json") or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(artifacts, list) or not artifacts:
            continue
        evidence.append({
            "at": row.get("at"),
            "task_id": row.get("task_id"),
            "device_id": row.get("device_id"),
            "capability": row.get("capability"),
            "status": row.get("status"),
            "receipt": row.get("receipt"),
            "artifacts": artifacts,
        })
    return evidence


def _md_line(row: dict) -> str:
    at = str(row.get("at") or "").replace("T", " ")[:19]
    subject = str(row.get("subject") or "").replace("`", "'").replace("\n", " ")[:180]
    return (f"- {at} **{row.get('status')}** `{row.get('capability')}` — {subject}; "
            f"receipt `{row.get('receipt')}`")


def _render_task(context: dict) -> Path:
    task_id = str(context.get("task_id") or "")
    rows = search(task_id=task_id, limit=80)
    path = _computer_dir() / "tasks" / f"{_safe(task_id)}.md"
    updated = str((rows[0] if rows else {}).get("at") or "unknown")
    goal = str(context.get("goal") or (rows[0] if rows else {}).get("goal") or "").strip()
    text = "\n".join([
        "<!-- praxis-generated: computer-task-map-v1 -->",
        f"# Computer evidence — {task_id}", "",
        f"- Device: `{context.get('device_id') or 'unknown'}`",
        f"- Root: `{context.get('root') or ''}`",
        f"- Goal: {goal or 'not recorded'}",
        f"- Updated: {updated}", "", "## Recent observations", "",
        *([_md_line(row) for row in rows] or ["- No observations yet."]), "",
    ])
    _write_text_atomic(path, text)
    return path


def _render_device(context: dict) -> Path:
    device_id = str(context.get("device_id") or "unknown")
    rows = search(device_id=device_id, limit=80)
    roots = []
    for row in rows:
        root = str(row.get("root") or "")
        if root and root not in roots:
            roots.append(root)
    path = _computer_dir() / "devices" / f"{_safe(device_id)}.md"
    updated = str((rows[0] if rows else {}).get("at") or "unknown")
    text = "\n".join([
        "<!-- praxis-generated: computer-device-map-v1 -->",
        f"# Computer — {device_id}", "", f"- Updated: {updated}", "",
        f"- Current inventory: [inventory/{_safe(device_id)}/CURRENT.md](../inventory/{_safe(device_id)}/CURRENT.md)", "",
        "## Observed roots", "", *([f"- `{root}`" for root in roots[:40]] or ["- None yet."]),
        "", "## Recent evidence", "", *([_md_line(row) for row in rows] or ["- None yet."]), "",
    ])
    _write_text_atomic(path, text)
    return path


def _render_map() -> Path:
    devices = _group_rows("device_id", 200)
    tasks = _group_rows("task_id", 500)
    path = _computer_dir() / "MAP.md"
    inventory = []
    updated_candidates = [
        str(row.get("updated") or "") for row in [*devices, *tasks]
        if str(row.get("updated") or "")
    ]
    inventory_dir = _computer_dir() / "inventory"
    if inventory_dir.exists():
        for directory in sorted(
            (p for p in inventory_dir.iterdir() if p.is_dir()),
            key=lambda item: item.name.casefold(),
        ):
            current = directory / "CURRENT.json"
            updated = "unknown"
            try:
                snapshot = json.loads(current.read_text(encoding="utf-8"))
                updated = str(snapshot.get("observed_at") or snapshot.get("captured_at") or "unknown")
            except (OSError, ValueError):
                pass
            if updated != "unknown":
                updated_candidates.append(updated)
            inventory.append(f"- [{directory.name} inventory](inventory/{directory.name}/CURRENT.md) — {updated}")
    map_updated = max(updated_candidates, default="unknown")
    text = "\n".join([
        "<!-- praxis-generated: computer-map-v1 -->",
        "# Computer map", "",
        "> Generated from append-only computer observations. SQLite is a rebuildable index, not memory truth.",
        "", f"- Updated: {map_updated}", "", "## Devices", "",
        *([f"- [{row['name']}](devices/{_safe(row['name'])}.md) — {row['updated']}"
           for row in devices]
          or ["- None yet."]),
        "", "## Inventory", "", *(inventory if inventory else ["- No inventory snapshot yet."]),
        "", "## Forge tasks", "",
        *([f"- [{row['name']}](tasks/{_safe(row['name'])}.md) — {row['updated']}"
           for row in tasks]
          or ["- None yet."]), "",
    ])
    _write_text_atomic(path, text)
    return path


def render_map() -> Path:
    """Public deterministic map refresh for inventory/access hooks."""
    with _LOCK:
        return _render_map()


def record(context: dict, capability: str, args: dict, execution: str, result: dict) -> dict | None:
    """Persist one useful server-side observation for a body call.

    Running process polls are deliberately suppressed.  Accepted and terminal process states,
    filesystem operations, artifact transfers and failures remain visible.
    """
    status = _status(result)
    if capability == "process.status" and status not in TERMINAL:
        return None
    task_id = str(context.get("task_id") or context.get("id") or "")
    if not task_id:
        return None
    context = {**context, "task_id": task_id}
    observation_id = _observation_id()
    device_id = str(context.get("device_id") or "unknown")
    root = str(context.get("root") or "")
    hashes = _hashes(result)
    artifacts = _artifacts(result)
    subject = _subject(context, capability, args)
    receipt_path = _forge_task_dir(task_id) / "receipts" / f"{observation_id}.json"
    row = {
        "schema": SCHEMA, "id": observation_id, "at": _utc_now(),
        "task_id": task_id, "device_id": device_id, "root": root,
        "goal": str(context.get("goal") or "")[:1_000],
        "execution": str(execution or "interactive"), "capability": str(capability),
        "status": status, "subject": subject,
        "summary": _summary(capability, status, subject, hashes, artifacts),
        "receipt": _rel(receipt_path), "hashes": hashes, "artifacts": artifacts,
        "request_id": str(result.get("request_id") or ""),
        "operation_id": str(result.get("operation_id") or args.get("operation_id") or ""),
    }
    receipt = {
        "schema": "praxis.computer.receipt.v1", "observation": row,
        "request": {"capability": capability, "execution": execution,
                    "args": _scrub(args, task_id=task_id, observation_id=observation_id)},
        "result": _scrub(result, task_id=task_id, observation_id=observation_id),
    }
    with _LOCK:
        # Bring a missing/stale disposable index to the canonical prefix before
        # appending the new event.  Failure is non-fatal: the post-append branch
        # below can still rebuild from JSONL, and canonical recording comes first.
        index_ready = False
        try:
            _ensure_index()
            index_ready = True
        except Exception:
            log.warning("computer memory index preflight deferred", exc_info=True)
        _write_json_atomic(receipt_path, receipt)
        day = row["at"][:10]
        _append_jsonl(_events_dir() / f"{day}.jsonl", row)
        _append_jsonl(_forge_task_dir(task_id) / "observations.jsonl", row)
        # Everything below is a disposable SQL/navigation projection.  Failure
        # here must not turn a successfully recorded body action into a failed
        # action or invite a duplicate retry; rebuild_index() restores it from
        # the JSONL above.
        try:
            if index_ready:
                _index(row)
                _write_index_meta()
            else:
                _ensure_index()
            _render_task(context)
            _render_device(context)
            _render_map()
        except Exception:
            log.warning("computer memory projection deferred; canonical event is durable",
                        exc_info=True)
    return row


def task_report(task_id: str, limit: int = 80) -> str:
    rows = search(task_id=task_id, limit=limit)
    if not rows:
        return f"Для {task_id} computer observations пока нет."
    card = _computer_dir() / "tasks" / f"{_safe(task_id)}.md"
    return "\n".join([f"evidence map: {_rel(card)}", *[_md_line(row) for row in rows]])


def task_summary(task_id: str) -> dict:
    rows = search(task_id=task_id, limit=1000)
    statuses: dict[str, int] = {}
    capabilities: dict[str, int] = {}
    artifacts: dict[str, dict] = {}
    for row in rows:
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        capabilities[row["capability"]] = capabilities.get(row["capability"], 0) + 1
        for artifact in json.loads(row.get("artifacts_json") or "[]"):
            artifacts[str(artifact.get("sha256") or "")] = artifact
    return {
        "task_id": task_id, "observations": len(rows), "statuses": statuses,
        "capabilities": capabilities, "artifacts": list(artifacts.values()),
        "map": _rel(_computer_dir() / "tasks" / f"{_safe(task_id)}.md"),
        "observation_ids": [row["id"] for row in rows],
    }


def finish_task(task: dict, *, outcome: str, checked: str = "", review: str = "") -> dict:
    """Promote one completed Windows task to one global life event, idempotently."""
    task_id = str(task.get("id") or "")
    marker = _forge_task_dir(task_id) / "computer-episode.json"
    try:
        existing = json.loads(marker.read_text(encoding="utf-8"))
        if existing.get("life_event_id"):
            return existing
    except (OSError, ValueError):
        pass
    summary = task_summary(task_id)
    text = (f"Windows-задача {task_id} завершена ({outcome}): {task.get('goal')}; "
            f"observations={summary['observations']}, artifacts={len(summary['artifacts'])}; "
            f"evidence={summary['map']}")
    try:
        import memory_life
        event = memory_life.append_event(
            "computer_episode", actor="Praxis", direction="internal", text=text,
            source="forge", source_id=task_id, salience=2,
            refs=summary["observation_ids"][:200],
            meta={"device_id": task.get("device_id"), "root": task.get("root"),
                  "outcome": outcome, "checked": checked, "review": review,
                  "evidence_map": summary["map"], "artifacts": summary["artifacts"]},
        )
    except Exception as exc:  # evidence remains valid even if life promotion is unavailable
        event = {"id": "", "error": f"{type(exc).__name__}: {exc}"}
    promoted = {**summary, "text": text, "life_event_id": event.get("id") or "",
                "life_error": event.get("error") or ""}
    _write_json_atomic(marker, promoted)
    try:
        import memory_catalog
        memory_catalog.rebuild()
    except Exception:
        pass
    return promoted


def rebuild_index() -> dict:
    """Rebuild the disposable SQLite index and Markdown map from canonical JSONL."""
    path = _db_path()
    with _LOCK:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                Path(str(path) + suffix).unlink()
        for event_file in _event_files():
            for line in event_file.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                    if row.get("schema") != SCHEMA:
                        continue
                    _index(row)
                except (KeyError, TypeError, ValueError, sqlite3.Error):
                    continue
        _write_index_meta()
        with contextlib.closing(_connect()) as db:
            count = int(db.execute("SELECT COUNT(*) FROM observations").fetchone()[0])
        for row in _latest_rows("task_id", 500):
            _render_task({
                "task_id": row["task_id"], "device_id": row["device_id"],
                "root": row["root"], "goal": row.get("goal") or "",
            })
        for row in _latest_rows("device_id", 200):
            _render_device({
                "device_id": row["device_id"], "root": row["root"],
            })
        _render_map()
    return {"ok": True, "observations": count, "database": _rel(path)}


if __name__ == "__main__":
    import sys
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "rebuild":
        print(json.dumps(rebuild_index(), ensure_ascii=False, indent=2))
    elif command == "search":
        print(json.dumps(search(" ".join(sys.argv[2:])), ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"database": _rel(_db_path()), "rows": len(_rows(limit=1000))},
                         ensure_ascii=False, indent=2))
