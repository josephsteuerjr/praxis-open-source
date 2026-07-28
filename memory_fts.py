"""Rebuildable SQLite FTS over Praxis's canonical memory sources.

Markdown, append-only JSONL and timestamped computer-inventory JSON remain the source
of truth.  This module owns only a disposable query accelerator under
``memory/.state``.  Generated navigation views (``memory/INDEX.md`` and
``memory/maps``) are deliberately excluded so maps do not become a second,
self-reinforcing memory corpus.
"""

from __future__ import annotations

import bisect
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import memory_provenance

SCHEMA_VERSION = "praxis.memory.fts.v7"
_LOCK = threading.RLock()
_WORD_RE = re.compile(r"[\wа-яё]+", re.I)
_RU_ENDINGS = (
    "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими", "ая", "яя",
    "ое", "ее", "ые", "ие", "ый", "ий", "ой", "ам", "ям", "ах", "ях",
    "ов", "ев", "ом", "ем", "ую", "юю", "а", "я", "ы", "и", "у", "ю", "е", "о",
)
_TEXT_FIELDS = (
    "text", "summary", "subject", "message", "purpose", "why_now", "outcome",
    "lesson", "status", "kind", "action", "capability", "goal", "reason",
    "details", "from_status", "to_status", "name", "path", "error", "code",
    "note", "statement", "why_it_matters", "next_move", "stage", "title",
    "topic_title", "sender_name", "media",
)
_ENTITY_FIELDS = (
    "actor", "person", "peer", "peer_id", "chat_id", "device_id", "task_id",
    "source", "source_id", "desire_id", "run_id", "event_id",
    "topic_id", "message_id", "sender_id", "reply_to_message_id",
)


@dataclass(frozen=True)
class Source:
    path: Path
    rel: str
    kind: str
    visibility: str
    meta: dict[str, Any] | None = None


def _db_path(memory_dir: Path) -> Path:
    return memory_dir / ".state" / "recall.sqlite3"


def _rel(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _generated_markdown(path: Path, memory_dir: Path) -> bool:
    rel = path.relative_to(memory_dir).as_posix()
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            if stream.readline().lstrip().startswith("<!-- praxis-generated:"):
                return True
    except OSError:
        pass
    if rel == "INDEX.md" or rel.startswith("maps/"):
        return True
    # Canon is the append-only desire event stream.  CURRENT.md is only its
    # deterministic, grep-friendly projection and must not feed recall twice.
    if rel == "desires/CURRENT.md":
        return True
    # These are deterministic projections of computer JSONL/inventory, not new facts.
    if rel == "computer/MAP.md" or rel.startswith("computer/devices/") \
            or rel.startswith("computer/tasks/"):
        return True
    return rel.startswith("computer/inventory/") and path.name == "CURRENT.md"


def _markdown_visibility(path: Path, memory_dir: Path, skills_dir: Path | None) -> str:
    if skills_dir is not None and _inside(path, skills_dir):
        return "public"
    rel = path.relative_to(memory_dir).as_posix()
    if rel.startswith("people/"):
        return "mixed"
    if rel == "graph.md":
        return "public"
    return "owner"


def _selected_jsonl(path: Path, memory_dir: Path) -> tuple[str, str] | None:
    rel = path.relative_to(memory_dir).as_posix()
    rules = (
        (r"^life/events/[^/]+\.jsonl$", "life_event"),
        (r"^computer/events/[^/]+\.jsonl$", "computer_event"),
        (r"^runs/.+/events\.jsonl$", "run_event"),
        (r"^self/[^/]+\.jsonl$", "self_event"),
        # Её собственные прожитые ходы в комнатах. Раньше они УНИЧТОЖАЛИСЬ при перекате
        # кольца — из опасения, что recall, намеренно слепой к аудитории, унесёт тему из
        # комнаты в комнату. Опасение верное, вывод был неверный: приватность — политика
        # ВЫДАЧИ, а не удаления. Теперь ходы сохраняются с меткой origin, а строка recall
        # называет комнату (`agent._recall_origin`), так что «откуда это» видно и ей, и
        # границе раскрытия. Отдельный kind, а не self_event: перепутать её ход в чужой
        # комнате с записью из своей лички нельзя ни в одном потребителе.
        (r"^self/rooms/[^/]+/turns-[^/]+\.jsonl$", "room_turn"),
        (r"^desires/events\.jsonl$", "desire_event"),
        (r"^social/.+\.jsonl$", "social_event"),
        (r"^access/events/[^/]+\.jsonl$", "access_event"),
        (r"^groups/[^/]+/archive\.jsonl$", "group_message"),
    )
    for pattern, kind in rules:
        if re.match(pattern, rel):
            return kind, "owner"
    return None


def _inventory_snapshot_key(path: Path) -> tuple[int, str, int, str]:
    """Prefer server-observed snapshots; keep legacy history deterministic.

    Old snapshots were named from the Windows clock.  A skewed legacy filename
    must not outrank a newer server-observed record forever.
    """
    try:
        item = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        observed = str(item.get("observed_at") or "") if isinstance(item, dict) else ""
        rank = 2 if observed else 1
    except (OSError, TypeError, ValueError):
        observed, rank = "", 0
    try:
        modified = path.stat().st_mtime_ns
    except OSError:
        modified = 0
    return rank, observed, modified, path.name


def _memory_files(memory_dir: Path, pattern: str, *, include_runs: bool) -> Iterable[Path]:
    """Walk canonical memory while allowing the automatic path to prune runs early."""
    if include_runs:
        yield from memory_dir.rglob(pattern)
        return
    # Path.rglob cannot prune a subtree.  Walking each top-level root except runs
    # avoids enumerating the multi-gigabyte durable execution tree at all.
    #
    # `self/rooms/**` подрезается по той же причине, что и `runs`, и это не вкусовщина:
    # автоматический recall обходит его на КАЖДОМ живом ходе, платя stat+resolve за
    # каждый файл, и получает оттуда РОВНО НОЛЬ строк — `automatic_recall_allowed`
    # отказывает виду `room_turn` явно. Замер адверсарки на Windows: 1500 файлов ≈
    # 1.4 секунды на ход, линейно и без потолка (каталогов там по одному на каждую
    # ветку разговора, где она когда-либо просыпалась). Явный recall ходит другим
    # путём (include_runs=True) и комнаты по-прежнему находит.
    for child in memory_dir.iterdir():
        if child.name == "runs":
            continue
        if child.is_file():
            if child.match(pattern):
                yield child
        elif child.name == "self":
            for path in child.rglob(pattern):
                if "rooms" not in path.relative_to(child).parts:
                    yield path
        elif child.is_dir():
            yield from child.rglob(pattern)


def iter_sources(*, base: Path, memory_dir: Path, skills_dir: Path | None = None,
                 include_runs: bool = True) -> list[Source]:
    """Return a stable, explicit list of canonical sources eligible for recall."""
    base, memory_dir = Path(base), Path(memory_dir)
    skills_dir = Path(skills_dir) if skills_dir is not None else None
    rows: list[Source] = []
    evidence = memory_provenance.claim_evidence_index(memory_dir)
    automatic_life_event_ids = frozenset(
        event_id for event_id, row in (evidence.get("events") or {}).items()
        if memory_provenance.event_automatic_recall_allowed(row)
    )
    if memory_dir.exists():
        for path in _memory_files(memory_dir, "*.md", include_runs=include_runs):
            if (not path.is_file() or not _inside(path, memory_dir)
                    or path.name.startswith("_") or _generated_markdown(path, memory_dir)):
                continue
            rel = path.relative_to(memory_dir).as_posix()
            if rel.startswith("bench/"):
                continue
            if any(part.startswith(".") for part in Path(rel).parts):
                continue
            base_rel = _rel(path, base)
            if re.fullmatch(r"life/claims/[^/]+\.md", rel):
                source_kind, meta = memory_provenance.claim_source(
                    path, evidence_index=evidence,
                )
            else:
                source_kind, meta = memory_provenance.episodic_kind(base_rel) or "markdown", None
            rows.append(Source(path, base_rel, source_kind,
                               _markdown_visibility(path, memory_dir, skills_dir), meta))
        for path in _memory_files(memory_dir, "*.jsonl", include_runs=include_runs):
            if not path.is_file() or not _inside(path, memory_dir):
                continue
            selected = _selected_jsonl(path, memory_dir)
            if selected:
                kind, visibility = selected
                meta = ({"automatic_event_ids": automatic_life_event_ids}
                        if kind == "life_event" else None)
                rows.append(Source(path, _rel(path, base), kind, visibility, meta))
        # Inventory history is canonical JSON rather than JSONL.  Index only the
        # newest timestamped snapshot per device: CURRENT.{json,md} are derived
        # projections, while indexing every daily snapshot would make stale app
        # and tool rows compete with the current machine state.
        inventory = memory_dir / "computer" / "inventory"
        if inventory.exists():
            for device in sorted((p for p in inventory.iterdir() if p.is_dir()),
                                 key=lambda p: p.name.casefold()):
                snapshots = sorted(
                    (p for p in device.glob("*.json") if p.is_file() and p.name != "CURRENT.json"),
                    key=_inventory_snapshot_key,
                )
                if snapshots:
                    path = snapshots[-1]
                    rows.append(Source(path, _rel(path, base), "inventory_snapshot", "owner"))
    if skills_dir is not None and skills_dir.exists():
        for path in skills_dir.glob("*.md"):
            if (path.is_file() and _inside(path, skills_dir)
                    and not path.name.startswith("_")
                    and path.name.casefold() != "index.md"):
                rows.append(Source(path, _rel(path, base), "skill", "public"))
    # Compact self history is immutable evidence outside memory/.  CURRENT is
    # already in the persona prompt; archived revisions remain explicitly
    # recallable without feeding the generated current projection back twice.
    self_history = base / "soul" / "self" / "history"
    if self_history.exists():
        for path in sorted(self_history.glob("*.md"), key=lambda p: p.name.casefold()):
            if path.is_file() and _inside(path, self_history):
                rows.append(Source(path, _rel(path, base), "self_history", "owner"))
    unique = {row.rel: row for row in rows}
    return [unique[key] for key in sorted(unique)]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _markdown_chunks(text: str) -> list[str]:
    chunks: list[str] = []
    block: list[str] = []

    def flush() -> None:
        if block:
            value = " ".join(line.strip() for line in block).strip()
            if len(value) >= 3:
                chunks.append(value)
            block.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("#"):
            flush()
            continue
        if re.match(r"^[-*]\s+", line):
            flush()
            value = re.sub(r"^[-*]\s+", "", line).strip()
            if len(value) >= 3:
                chunks.append(value)
            continue
        block.append(line)
    flush()
    return chunks


def _stem(token: str) -> str:
    value = token.casefold().replace("ё", "е")
    if len(value) <= 4:
        return value
    for ending in _RU_ENDINGS:
        if value.endswith(ending) and len(value) - len(ending) >= 4:
            return value[:-len(ending)]
    return value


def _terms(text: str) -> str:
    return " ".join(_stem(token) for token in _WORD_RE.findall(str(text or "")) if len(token) > 1)


def _json_values(value: Any) -> Iterable[str]:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        if text:
            yield text
    elif isinstance(value, list):
        for item in value:
            yield from _json_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _json_values(item)


def _json_text(row: dict) -> str:
    values: list[str] = []
    for key in _TEXT_FIELDS:
        for value in _json_values(row.get(key)):
            if value not in values:
                values.append(value)
    meta = row.get("meta")
    if isinstance(meta, dict):
        for key in _TEXT_FIELDS:
            for value in _json_values(meta.get(key)):
                if value not in values:
                    values.append(value)
    # Desire events carry their current, source-of-truth snapshot under state.
    state = row.get("state")
    if isinstance(state, dict):
        for key in _TEXT_FIELDS:
            for value in _json_values(state.get(key)):
                if value not in values:
                    values.append(value)
    return " · ".join(values)[:16_000]


def _json_entities(row: dict) -> list[str]:
    values: list[str] = []
    for key in _ENTITY_FIELDS:
        for value in _json_values(row.get(key)):
            if value not in values:
                values.append(value)
    for value in _json_values(row.get("entities")):
        if value not in values:
            values.append(value)
    return values[:100]


def _list_strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if value is None or value == "":
        return []
    return [str(value)]


def _chunk_visibility(text: str, source_visibility: str, row: dict | None = None) -> str:
    item = row or {}
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    explicit = str(item.get("visibility") or meta.get("visibility") or "").strip().lower()
    if explicit == "public":
        return "public"
    if explicit in {"owner", "private"}:
        return "owner"
    if source_visibility == "mixed":
        return "owner" if re.search(r"\[private\]", text, re.I) else "public"
    return source_visibility


def _source_chunks(source: Source) -> tuple[list[dict], int]:
    rows: list[dict] = []
    corrupt = 0
    if source.kind in memory_provenance.CLAIM_KINDS:
        # The receipt body remains explicitly searchable, but metadata/status/revision
        # prose never inherits the statement's trust class.  Automatic recall gets one
        # exact, content-bound projection only.
        raw_kind = (source.kind if source.kind in {
            memory_provenance.CLAIM_CONTESTED_KIND,
            memory_provenance.CLAIM_UNSUPPORTED_KIND,
            memory_provenance.CLAIM_INVALID_KIND,
        } else memory_provenance.CLAIM_RAW_KIND)
        for index, text in enumerate(_markdown_chunks(_read(source.path))):
            rows.append({
                "chunk_key": f"raw:{index}", "text": text,
                "source_type": raw_kind,
                "automatic_eligible": False,
                "visibility": _chunk_visibility(text, source.visibility),
                "at": "", "event_id": "", "run_id": "", "refs": [],
                "supersedes": [], "entities": [],
            })
        meta = source.meta or {}
        if meta.get("_statement"):
            text, projection_kind, eligible = memory_provenance.claim_projection(meta)
            rows.append({
                "chunk_key": "claim:statement", "text": text,
                "source_type": projection_kind,
                "automatic_eligible": bool(eligible),
                "visibility": "owner" if meta.get("visibility") == "private" else "public",
                "at": str(meta.get("updated_at") or ""),
                "event_id": str(meta.get("id") or ""), "run_id": str(meta.get("last_run") or ""),
                "refs": list(meta.get("evidence_ids") or []), "supersedes": [],
                "entities": [str(meta.get("subject") or "")],
            })
        return rows, corrupt

    if source.kind in {
        "markdown", "skill", "self_history",
        memory_provenance.UNTRUSTED_EPISODIC_KIND,
        memory_provenance.UNTRUSTED_REFLECTION_KIND,
    }:
        for index, text in enumerate(_markdown_chunks(_read(source.path))):
            rows.append({
                "chunk_key": f"md:{index}", "text": text,
                "source_type": source.kind,
                "automatic_eligible": source.kind in {"skill", "markdown"},
                "visibility": _chunk_visibility(text, source.visibility),
                "at": "", "event_id": "", "run_id": "", "refs": [],
                "supersedes": [], "entities": [],
            })
        return rows, corrupt

    if source.kind == "inventory_snapshot":
        try:
            item = json.loads(source.path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, TypeError, ValueError):
            return rows, 1
        if not isinstance(item, dict):
            return rows, 1
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        device = str(item.get("device_id") or payload.get("hostname") or source.path.parent.name)
        captured = str(item.get("captured_at") or payload.get("captured_at") or "")
        observed = str(item.get("observed_at") or captured)
        event_id = f"inventory:{device}:{observed or source.path.stem}"
        base_entities = [value for value in (device, payload.get("hostname"), payload.get("user"))
                         if str(value or "")]

        def add(key: str, text: str, entities=()) -> None:
            value = str(text or "").strip()
            if len(value) < 3:
                return
            rows.append({
                "chunk_key": f"inventory:{key}", "text": value, "source": device,
                "source_type": source.kind, "automatic_eligible": True,
                "visibility": "owner", "at": observed, "event_id": event_id,
                "run_id": "", "refs": [source.rel], "supersedes": [],
                "entities": list(dict.fromkeys([*base_entities, *[str(x) for x in entities if str(x)]])),
            })

        os_row = payload.get("os") if isinstance(payload.get("os"), dict) else {}
        machine = payload.get("machine") if isinstance(payload.get("machine"), dict) else {}
        identity = item.get("identity") if isinstance(item.get("identity"), dict) else {}
        add("overview", " · ".join(str(value) for value in (
            f"computer {device}", payload.get("hostname"), payload.get("user"),
            os_row.get("caption"), os_row.get("version"), os_row.get("build"),
            os_row.get("architecture"), machine.get("manufacturer"), machine.get("model"),
            identity.get("kind"), identity.get("integrity"),
        ) if str(value or "").strip()))
        for group, label in (("volumes", "volume"), ("tools", "tool"), ("apps", "application")):
            values = payload.get(group) if isinstance(payload.get(group), list) else []
            for index, value in enumerate(values):
                if not isinstance(value, dict):
                    continue
                fields = [str(part) for part in value.values() if part not in (None, "")]
                add(f"{group}:{index}", f"{label} " + " · ".join(fields), fields[:3])
        for group, label in (("known_roots", "known root"), ("project_roots", "project root")):
            values = payload.get(group) if isinstance(payload.get(group), list) else []
            for index, value in enumerate(values):
                add(f"{group}:{index}", f"{label} {value}", (value,))
        return rows, corrupt

    try:
        stream = source.path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return rows, corrupt
    seen_keys: set[str] = set()
    with stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                corrupt += 1
                continue
            if not isinstance(item, dict):
                corrupt += 1
                continue
            text = _json_text(item)
            automatic_text = text
            if source.kind == "computer_event" and item.get("goal"):
                automatic_item = dict(item)
                automatic_item.pop("goal", None)
                automatic_text = _json_text(automatic_item)
            if len(text) < 3:
                continue
            event_id = str(item.get("id") or item.get("event_id") or "")
            digest = hashlib.sha256(line.encode("utf-8", "replace")).hexdigest()[:16]
            key = f"event:{event_id}" if event_id else f"line:{line_no}:{digest}"
            if key in seen_keys:
                key = f"{key}:duplicate:{line_no}:{digest}"
            seen_keys.add(key)
            refs = _list_strings(item.get("refs") or item.get("evidence_refs"))
            supersedes = _list_strings(item.get("supersedes"))
            run_id = str(item.get("run_id") or (item.get("meta") or {}).get("run_id") or "") \
                if isinstance(item.get("meta") or {}, dict) else str(item.get("run_id") or "")
            row = {
                "chunk_key": key, "text": automatic_text,
                "source_type": source.kind,
                "automatic_eligible": (
                    event_id in set((source.meta or {}).get("automatic_event_ids") or ())
                    if source.kind == "life_event"
                    else memory_provenance.self_event_automatic_recall_allowed(item)
                    if source.kind == "self_event"
                    else True
                ),
                "source": str(item.get("actor") or item.get("device_id")
                              or item.get("task_id") or source.kind),
                "visibility": _chunk_visibility(automatic_text, source.visibility, item),
                "at": str(item.get("ts") or item.get("at") or item.get("created_at") or ""),
                "event_id": event_id, "run_id": run_id, "refs": refs,
                "supersedes": supersedes, "entities": _json_entities(item),
                "desire_id": str(item.get("desire_id") or ""),
            }
            if source.kind == "computer_event" and automatic_text != text:
                rows.append({
                    **row, "chunk_key": f"{key}:explicit", "text": text,
                    "automatic_eligible": False,
                    "visibility": _chunk_visibility(text, source.visibility, item),
                })
            rows.append(row)
    return rows, corrupt


def _source_snapshot(source: Source) -> str:
    """Per-source stat key; '' when unreadable (excluded from the fingerprint)."""
    try:
        stat = source.path.stat()
    except OSError:
        return ""
    return f"{source.kind}\x00{stat.st_size}\x00{stat.st_mtime_ns}"


def _snapshots(sources: list[Source]) -> dict[str, str]:
    """One stat pass reused by both the corpus fingerprint and the v7 diff."""
    return {source.rel: _source_snapshot(source) for source in sources}


def _fingerprint_from(snapshots: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(snapshots):
        snapshot = snapshots[rel]
        if not snapshot:
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(snapshot.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _fingerprint(sources: list[Source]) -> str:
    return _fingerprint_from(_snapshots(sources))


def _create_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            chunk_key TEXT NOT NULL,
            source TEXT NOT NULL,
            source_type TEXT NOT NULL,
            visibility TEXT NOT NULL,
            automatic_eligible INTEGER NOT NULL,
            at TEXT NOT NULL,
            event_id TEXT NOT NULL,
            desire_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            refs_json TEXT NOT NULL,
            supersedes_json TEXT NOT NULL,
            entities TEXT NOT NULL,
            terms TEXT NOT NULL,
            text TEXT NOT NULL,
            UNIQUE(path, chunk_key)
        );
        CREATE TABLE source_state (
            path TEXT PRIMARY KEY,
            corrupt_lines INTEGER NOT NULL DEFAULT 0,
            snapshot TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX chunks_path ON chunks(path);
        CREATE INDEX chunks_visibility ON chunks(visibility);
        CREATE INDEX chunks_automatic ON chunks(automatic_eligible);
        CREATE INDEX chunks_event ON chunks(event_id);
        CREATE INDEX chunks_desire ON chunks(desire_id);
        CREATE INDEX chunks_run ON chunks(run_id);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            text, terms, entities,
            content='chunks', content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
          INSERT INTO chunks_fts(rowid, text, terms, entities)
          VALUES (new.id, new.text, new.terms, new.entities);
        END;
        CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
          INSERT INTO chunks_fts(chunks_fts, rowid, text, terms, entities)
          VALUES ('delete', old.id, old.text, old.terms, old.entities);
        END;
        CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
          INSERT INTO chunks_fts(chunks_fts, rowid, text, terms, entities)
          VALUES ('delete', old.id, old.text, old.terms, old.entities);
          INSERT INTO chunks_fts(rowid, text, terms, entities)
          VALUES (new.id, new.text, new.terms, new.entities);
        END;
        """
    )


def _insert_source(db: sqlite3.Connection, source: Source, memory_dir: Path,
                   snapshot: str | None = None) -> tuple[int, int]:
    chunks, corrupt = _source_chunks(source)
    for chunk in chunks:
        db.execute(
            """INSERT INTO chunks
               (path, chunk_key, source, source_type, visibility, automatic_eligible,
                at, event_id, desire_id, run_id,
                refs_json, supersedes_json, entities, terms, text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source.rel, chunk["chunk_key"], chunk.get("source") or source.path.stem,
                chunk.get("source_type") or source.kind,
                chunk["visibility"], int(bool(chunk.get("automatic_eligible"))
                    and memory_provenance.automatic_recall_allowed(
                    source_type=chunk.get("source_type") or source.kind,
                    path=source.rel, text=chunk["text"],
                    memory_dir=memory_dir,
                )), chunk["at"], chunk["event_id"],
                chunk.get("desire_id") or "", chunk["run_id"],
                json.dumps(chunk["refs"], ensure_ascii=False, separators=(",", ":")),
                json.dumps(chunk["supersedes"], ensure_ascii=False, separators=(",", ":")),
                " ".join(chunk["entities"]), _terms(chunk["text"]), chunk["text"],
            ),
        )
    db.execute(
        "INSERT OR REPLACE INTO source_state(path, corrupt_lines, snapshot) VALUES (?, ?, ?)",
        (source.rel, corrupt,
         _source_snapshot(source) if snapshot is None else snapshot),
    )
    return len(chunks), corrupt


def rebuild(*, base: Path, memory_dir: Path, skills_dir: Path | None = None,
            db_path: Path | None = None) -> dict:
    """Atomically rebuild the disposable database from canonical sources."""
    base, memory_dir = Path(base), Path(memory_dir)
    path = Path(db_path) if db_path is not None else _db_path(memory_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    sources = iter_sources(base=base, memory_dir=memory_dir, skills_dir=skills_dir)
    snapshots = _snapshots(sources)
    fingerprint = _fingerprint_from(snapshots)
    tmp = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    chunks = corrupt = 0
    with _LOCK:
        try:
            with contextlib.closing(sqlite3.connect(tmp)) as db:
                _create_schema(db)
                for source in sources:
                    count, broken = _insert_source(db, source, memory_dir,
                                                   snapshot=snapshots.get(source.rel, ""))
                    chunks += count
                    corrupt += broken
                db.executemany(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    (("schema", SCHEMA_VERSION), ("fingerprint", fingerprint),
                     ("sources", str(len(sources))), ("chunks", str(chunks)),
                     ("corrupt_lines", str(corrupt))),
                )
                db.commit()
                check = db.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok":
                    raise sqlite3.DatabaseError(f"integrity_check: {check}")
            os.replace(tmp, path)
            for suffix in ("-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError):
                    Path(str(path) + suffix).unlink()
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
    return {
        "ok": True, "schema": SCHEMA_VERSION, "database": _rel(path, base),
        "sources": len(sources), "chunks": chunks, "corrupt_lines": corrupt,
        "fingerprint": fingerprint,
    }


def _meta(path: Path) -> dict[str, str]:
    try:
        with contextlib.closing(sqlite3.connect(path)) as db:
            tables = {
                str(row[0]) for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            if not {"meta", "chunks", "chunks_fts", "source_state"}.issubset(tables):
                return {}
            return {str(key): str(value) for key, value in db.execute("SELECT key, value FROM meta")}
    except (OSError, sqlite3.Error):
        return {}


def ensure(*, base: Path, memory_dir: Path, skills_dir: Path | None = None,
           db_path: Path | None = None) -> dict:
    path = Path(db_path) if db_path is not None else _db_path(Path(memory_dir))
    sources = iter_sources(base=Path(base), memory_dir=Path(memory_dir), skills_dir=skills_dir)
    current = _meta(path)
    snapshots = _snapshots(sources)
    fingerprint = _fingerprint_from(snapshots)
    if current.get("schema") != SCHEMA_VERSION:
        return rebuild(base=Path(base), memory_dir=Path(memory_dir), skills_dir=skills_dir,
                       db_path=path)
    if current.get("fingerprint") != fingerprint:
        # The durable run tree appends events on every live turn, so the corpus
        # fingerprint moves constantly.  Re-parsing the whole multi-gigabyte canon
        # for that (minutes, while the mind lock is held) is what made explicit
        # recall crawl; reconcile only the sources whose stat snapshot moved.
        return _refresh_changed(base=Path(base), memory_dir=Path(memory_dir),
                                db_path=path, sources=sources,
                                snapshots=snapshots, fingerprint=fingerprint)
    return {
        "ok": True, "schema": SCHEMA_VERSION, "database": _rel(path, Path(base)),
        "sources": int(current.get("sources") or 0), "chunks": int(current.get("chunks") or 0),
        "corrupt_lines": int(current.get("corrupt_lines") or 0), "fingerprint": fingerprint,
        "rebuilt": False,
    }


def _refresh_changed(*, base: Path, memory_dir: Path, db_path: Path,
                     sources: list[Source], snapshots: dict[str, str],
                     fingerprint: str) -> dict:
    """Incrementally reconcile the disposable index with canon (v7).

    Claim kind/eligibility and life-event automatic ids are derived from the
    provenance evidence index rather than from each file's own bytes alone, so
    any change under memory/life/ also re-derives every dependent source; the
    FTS delete/insert triggers keep chunks_fts consistent row by row."""
    by_rel = {source.rel: source for source in sources}
    with _LOCK, contextlib.closing(sqlite3.connect(db_path, timeout=15)) as db:
        db.execute("PRAGMA busy_timeout=15000")
        stored = {str(row[0]): str(row[1]) for row in db.execute(
            "SELECT path, snapshot FROM source_state")}
        changed = {rel for rel, snapshot in snapshots.items()
                   if stored.get(rel) != snapshot}
        removed = [rel for rel in stored if rel not in by_rel]
        if any(rel.startswith("memory/life/") for rel in (*changed, *removed)):
            changed.update(
                rel for rel, source in by_rel.items()
                if rel.startswith("memory/life/claims/") or source.kind == "life_event"
            )
        for rel in removed:
            db.execute("DELETE FROM chunks WHERE path = ?", (rel,))
            db.execute("DELETE FROM source_state WHERE path = ?", (rel,))
        for rel in sorted(changed):
            db.execute("DELETE FROM chunks WHERE path = ?", (rel,))
            _insert_source(db, by_rel[rel], memory_dir, snapshot=snapshots.get(rel, ""))
        chunks = int(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] or 0)
        corrupt = int(db.execute(
            "SELECT COALESCE(SUM(corrupt_lines), 0) FROM source_state").fetchone()[0] or 0)
        for key, value in (("fingerprint", fingerprint), ("sources", str(len(sources))),
                           ("chunks", str(chunks)), ("corrupt_lines", str(corrupt))):
            db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
        db.commit()
    return {
        "ok": True, "schema": SCHEMA_VERSION, "database": _rel(db_path, Path(base)),
        "sources": len(sources), "chunks": chunks, "corrupt_lines": corrupt,
        "fingerprint": fingerprint, "rebuilt": False, "refreshed": len(changed) + len(removed),
    }


def _match_query(query: str) -> str:
    stems = []
    for token in _WORD_RE.findall(str(query or "")):
        stem = _stem(token)
        if len(stem) > 1 and stem not in stems:
            stems.append(stem)
    # Unqualified phrases intentionally search original text, normalized terms and entity
    # identifiers.  Quoting every token keeps FTS operators in user input inert.
    return " OR ".join(f'"{stem.replace(chr(34), chr(34) * 2)}"*' for stem in stems)


def _canonical_candidates(*, base: Path, memory_dir: Path, skills_dir: Path | None,
                          rows: list[dict]) -> tuple[list[dict], bool]:
    """Rebind cache locators to current canonical chunks.

    The SQLite file is disposable and may be stale or tampered with.  A returned row
    therefore has authority only when every prompt-relevant field exactly matches the
    chunk re-derived from the current source file.
    """
    wanted = {str(row.get("path") or "") for row in rows}
    sources = {
        source.rel: source for source in iter_sources(
            base=base, memory_dir=memory_dir, skills_dir=skills_dir,
        ) if source.rel in wanted
    }
    chunks: dict[tuple[str, str], tuple[Source, dict]] = {}
    for rel, source in sources.items():
        for chunk in _source_chunks(source)[0]:
            chunks[(rel, str(chunk.get("chunk_key") or ""))] = (source, chunk)
    valid: list[dict] = []
    mismatch = False
    for row in rows:
        key = (str(row.get("path") or ""), str(row.get("chunk_key") or ""))
        canonical = chunks.get(key)
        if canonical is None:
            mismatch = True
            continue
        source, chunk = canonical
        expected = {
            "source": chunk.get("source") or source.path.stem,
            "source_type": chunk.get("source_type") or source.kind,
            "visibility": chunk.get("visibility") or source.visibility,
            "automatic_eligible": int(bool(chunk.get("automatic_eligible"))
                and memory_provenance.automatic_recall_allowed(
                    source_type=chunk.get("source_type") or source.kind,
                    path=source.rel, text=chunk.get("text") or "", memory_dir=memory_dir,
                )),
            "at": chunk.get("at") or "", "event_id": chunk.get("event_id") or "",
            "desire_id": chunk.get("desire_id") or "", "run_id": chunk.get("run_id") or "",
            "refs_json": json.dumps(chunk.get("refs") or [], ensure_ascii=False,
                                    separators=(",", ":")),
            "supersedes_json": json.dumps(chunk.get("supersedes") or [], ensure_ascii=False,
                                           separators=(",", ":")),
            "entities": " ".join(chunk.get("entities") or []),
            "terms": _terms(chunk.get("text") or ""),
            "text": chunk.get("text") or "",
        }
        if any(str(row.get(field) if row.get(field) is not None else "") != str(value)
               for field, value in expected.items()):
            mismatch = True
            continue
        valid.append(row)
    return valid, mismatch


_CANON_CACHE: dict[str, tuple[str, list[dict]]] = {}
_CANON_GEN = 0
_INDEX_CACHE: dict = {}


def _build_source_rows(source: "Source", memory_dir: Path) -> list[dict]:
    """Parse one source into automatic-eligible rows with precomputed match tokens.

    Pure function of the source bytes; memoised by stat snapshot in
    ``_automatic_canonical_chunks`` so an unchanged source is not re-parsed or
    re-stemmed on every live turn.  That per-turn rescan of the whole canon was
    the dominant pre-model latency."""
    out: list[dict] = []
    for chunk in _source_chunks(source)[0]:
        text = str(chunk.get("text") or "")
        source_type = str(chunk.get("source_type") or source.kind)
        visibility = str(chunk.get("visibility") or source.visibility)
        eligible = bool(chunk.get("automatic_eligible")) and memory_provenance.automatic_recall_allowed(
            source_type=source_type, path=source.rel, text=text, memory_dir=memory_dir,
        )
        if not eligible:
            continue
        refs = list(chunk.get("refs") or [])
        supersedes = list(chunk.get("supersedes") or [])
        entities = [str(value) for value in chunk.get("entities") or []]
        entities_text = " ".join(entities)
        source_name = str(chunk.get("source") or source.path.stem)
        terms = _terms(text)
        match_tokens = terms.split()
        match_tokens.extend(_terms(f"{entities_text} {source_name} {source.rel}").split())
        out.append({
            "path": source.rel,
            "chunk_key": str(chunk.get("chunk_key") or ""),
            "source": source_name,
            "source_type": source_type,
            "visibility": visibility,
            "automatic_eligible": 1,
            "at": str(chunk.get("at") or ""),
            "event_id": str(chunk.get("event_id") or ""),
            "desire_id": str(chunk.get("desire_id") or ""),
            "run_id": str(chunk.get("run_id") or ""),
            "refs_json": json.dumps(refs, ensure_ascii=False, separators=(",", ":")),
            "supersedes_json": json.dumps(
                supersedes, ensure_ascii=False, separators=(",", ":"),
            ),
            "entities": entities_text,
            "terms": terms,
            "text": text,
            "_mtokens": match_tokens,
        })
    return out


def _automatic_canonical_chunks(*, base: Path, memory_dir: Path,
                                skills_dir: Path | None, scope: str) -> list[dict]:
    """Derive the complete automatic corpus from canon, never from an index selection.

    Per-source parsing/stemming is memoised by stat snapshot: only sources whose
    bytes moved are re-parsed on a live turn.  ``_CANON_GEN`` is bumped whenever the
    row set changes so the inverted index (``_automatic_index``) knows to rebuild.
    Run artifacts stay excluded because the durable run tree grows on every turn and
    yields no eligible automatic chunk."""
    global _CANON_GEN
    rows: list[dict] = []
    seen: set[str] = set()
    for source in iter_sources(base=base, memory_dir=memory_dir, skills_dir=skills_dir,
                               include_runs=False):
        if source.rel.startswith("memory/runs/"):
            continue
        seen.add(source.rel)
        snapshot = _source_snapshot(source)
        cached = _CANON_CACHE.get(source.rel)
        if cached is not None and snapshot and cached[0] == snapshot:
            source_rows = cached[1]
        else:
            source_rows = _build_source_rows(source, memory_dir)
            if snapshot:
                _CANON_CACHE[source.rel] = (snapshot, source_rows)
            else:
                _CANON_CACHE.pop(source.rel, None)
            _CANON_GEN += 1
        if scope == "owner":
            rows.extend(source_rows)
        else:
            rows.extend(row for row in source_rows if row["visibility"] == "public")
    stale = [rel for rel in _CANON_CACHE if rel not in seen]
    for rel in stale:
        _CANON_CACHE.pop(rel, None)
    if stale:
        _CANON_GEN += 1
    return rows

_AUTOMATIC_MANIFEST_FIELDS = (
    "path", "chunk_key", "source", "source_type", "visibility", "automatic_eligible",
    "at", "event_id", "desire_id", "run_id", "refs_json", "supersedes_json",
    "entities", "terms", "text",
)


def _manifest(rows: Iterable[dict]) -> list[tuple[str, ...]]:
    return sorted(
        tuple(str(row.get(field) if row.get(field) is not None else "")
              for field in _AUTOMATIC_MANIFEST_FIELDS)
        for row in rows
    )


def _automatic_cache_matches(path: Path, canonical: list[dict]) -> bool:
    """Detect logical cache edits/omissions; automatic retrieval does not depend on it."""
    try:
        with contextlib.closing(sqlite3.connect(path, timeout=15)) as db:
            db.row_factory = sqlite3.Row
            fields = ", ".join(_AUTOMATIC_MANIFEST_FIELDS)
            cached = [dict(row) for row in db.execute(
                f"SELECT {fields} FROM chunks WHERE automatic_eligible = 1"
            ).fetchall()]
    except (OSError, sqlite3.Error):
        return False
    return _manifest(cached) == _manifest(canonical)


def _automatic_index(*, base: Path, memory_dir: Path,
                     skills_dir: Path | None, scope: str):
    """(retrieval_rows, sorted_tokens, postings) for automatic recall.

    ``postings`` maps each match token to {row_index: occurrence_count}.  Built once
    per (corpus generation, scope) and cached, so a live turn ranks only the rows that
    actually contain a query stem (O(matches)) instead of scanning every chunk against
    every stem (O(chunks x stems x tokens) — that grew to minutes once the recall query
    was the whole last_n conversation, hundreds of unique stems)."""
    canonical = _automatic_canonical_chunks(
        base=base, memory_dir=memory_dir, skills_dir=skills_dir, scope=scope,
    )
    key = (_CANON_GEN, scope)
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    latest_desire_rows: dict[str, dict] = {}
    retrieval_rows: list[dict] = []
    for row in canonical:
        desire_id = str(row.get("desire_id") or "")
        if row.get("source_type") != "desire_event" or not desire_id:
            retrieval_rows.append(row)
            continue
        previous = latest_desire_rows.get(desire_id)
        row_order = (str(row.get("at") or ""), str(row.get("chunk_key") or ""))
        previous_order = (
            str(previous.get("at") or ""), str(previous.get("chunk_key") or "")
        ) if previous else ("", "")
        if previous is None or row_order > previous_order:
            latest_desire_rows[desire_id] = row
    retrieval_rows.extend(latest_desire_rows.values())
    postings: dict[str, dict[int, int]] = {}
    for idx, row in enumerate(retrieval_rows):
        tokens = row.get("_mtokens")
        if tokens is None:
            tokens = str(row["terms"]).split()
            tokens.extend(_terms(
                f"{row['entities']} {row['source']} {row['path']}"
            ).split())
        for token in tokens:
            bucket = postings.get(token)
            if bucket is None:
                postings[token] = {idx: 1}
            else:
                bucket[idx] = bucket.get(idx, 0) + 1
    result = (retrieval_rows, sorted(postings), postings)
    for stale_key in [k for k in _INDEX_CACHE if k[0] != _CANON_GEN]:
        del _INDEX_CACHE[stale_key]
    _INDEX_CACHE[key] = result
    return result


def _automatic_search(query: str, *, base: Path, memory_dir: Path,
                      skills_dir: Path | None, limit: int, scope: str,
                      db_path: Path | None) -> list[dict]:
    """Rank a small strict corpus directly; SQLite/FTS cannot inject or hide a cue."""
    # Automatic prompt recall is derived and ranked from canonical sources below; it
    # never reads candidates from the disposable explicit-search database.  Do not
    # synchronously ensure that database here.  A live turn creates its durable run
    # before prompt recall, so run context/events change the full-corpus fingerprint on
    # every turn.  Ensuring here consequently rebuilt the complete index on every DM
    # (minutes of pre-model latency) despite the result being unused.  Explicit search
    # and explicit rebuild hooks remain the owners of cache materialization and repair.
    database = Path(db_path) if db_path is not None else _db_path(memory_dir)
    state = {"schema": SCHEMA_VERSION, "database": _rel(database, base)}

    stems = list(dict.fromkeys(
        _stem(token) for token in _WORD_RE.findall(str(query or ""))
        if len(_stem(token)) > 1
    ))
    if not stems:
        return []
    retrieval_rows, sorted_tokens, postings = _automatic_index(
        base=base, memory_dir=memory_dir, skills_dir=skills_dir, scope=scope,
    )
    n_stems = len(stems)
    n_tokens = len(sorted_tokens)
    row_hits: dict[int, list[int]] = {}
    for si, stem in enumerate(stems):
        j = bisect.bisect_left(sorted_tokens, stem)
        while j < n_tokens:
            token = sorted_tokens[j]
            if not token.startswith(stem):
                break
            for row_idx, cnt in postings[token].items():
                counts = row_hits.get(row_idx)
                if counts is None:
                    counts = [0] * n_stems
                    row_hits[row_idx] = counts
                counts[si] += cnt
            j += 1
    ranked: list[tuple[float, dict]] = []
    for row_idx, hits in row_hits.items():
        matched = sum(1 for count in hits if count)
        if not matched:
            continue
        row = retrieval_rows[row_idx]
        tokens_len = len(row.get("_mtokens") or ())
        score = matched / n_stems + min(0.25, sum(hits) / max(8.0, tokens_len))
        ranked.append((score, row))
    ranked.sort(key=lambda item: (-item[0], item[1]["path"], item[1]["chunk_key"]))
    selected = ranked[:max(1, min(int(limit or 30), 500))]
    strongest = max((score for score, _ in selected), default=1.0)
    out: list[dict] = []
    for score, row in selected:
        refs = json.loads(row["refs_json"] or "[]")
        supersedes = json.loads(row["supersedes_json"] or "[]")
        out.append({
            "id": f"{row['path']}#{row['chunk_key']}", "text": row["text"],
            "source": row["source"], "path": row["path"],
            "lexical": round(score / strongest, 6), "source_type": row["source_type"],
            "visibility": row["visibility"], "automatic_eligible": True,
            "automatic_canonical": True, "at": row["at"],
            "event_id": row["event_id"], "run_id": row["run_id"],
            "desire_id": row["desire_id"], "refs": refs,
            "supersedes": supersedes,
            "provenance": list(dict.fromkeys([*refs, *supersedes])),
            "index": f"{state.get('schema') or SCHEMA_VERSION}:canonical",
        })
    return out


def search(query: str, *, base: Path, memory_dir: Path, skills_dir: Path | None = None,
           limit: int = 30, scope: str = "owner", purpose: str = "explicit",
           db_path: Path | None = None) -> list[dict]:
    """Rank FTS candidates and expose provenance/visibility metadata."""
    query = str(query or "").strip()
    if not query:
        return []
    base, memory_dir = Path(base), Path(memory_dir)
    skills_dir = Path(skills_dir) if skills_dir is not None else None
    purpose = "automatic" if purpose == "automatic" else "explicit"
    if purpose == "automatic":
        return _automatic_search(
            query, base=base, memory_dir=memory_dir, skills_dir=skills_dir,
            limit=limit, scope=scope, db_path=db_path,
        )
    state = ensure(base=base, memory_dir=memory_dir, skills_dir=skills_dir,
                   db_path=db_path)
    path = Path(db_path) if db_path is not None else _db_path(Path(memory_dir))
    match = _match_query(query)
    if not match:
        return []
    cap = max(1, min(int(limit or 30), 500))
    visibility_sql = "" if scope == "owner" else " AND c.visibility = 'public'"
    automatic_sql = " AND c.automatic_eligible = 1" if purpose == "automatic" else ""
    sql = f"""
        SELECT c.*, bm25(chunks_fts, 1.0, 0.45, 0.7) AS rank
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        WHERE chunks_fts MATCH ? {visibility_sql} {automatic_sql}
        ORDER BY rank ASC, c.path ASC, c.chunk_key ASC
        LIMIT ?
    """
    candidates: list[dict] = []
    for attempt in range(2):
        with contextlib.closing(sqlite3.connect(path, timeout=15)) as db:
            db.row_factory = sqlite3.Row
            cached = [dict(row) for row in db.execute(
                sql, (match, min(2000, max(80, cap * 8))),
            ).fetchall()]
        candidates, mismatch = _canonical_candidates(
            base=Path(base), memory_dir=Path(memory_dir), skills_dir=skills_dir, rows=cached,
        )
        if not mismatch:
            break
        if attempt == 0:
            state = rebuild(base=Path(base), memory_dir=Path(memory_dir),
                            skills_dir=skills_dir, db_path=path)
            continue
        # Canon changed twice during one read or the cache cannot be reconciled.
        # Fail closed instead of letting a disposable row become prompt authority.
        candidates = []
    selected = []
    seen_desires: set[str] = set()
    for row in candidates:
        desire_id = str(row.get("desire_id") or "")
        if row.get("source_type") == "desire_event" and desire_id:
            if desire_id in seen_desires:
                continue
            seen_desires.add(desire_id)
        selected.append(row)
        if len(selected) >= cap:
            break
    strengths = [max(0.0, -float(row.get("rank") or 0.0)) for row in selected]
    strongest = max(strengths, default=0.0)
    out = []
    for index, (row, strength) in enumerate(zip(selected, strengths)):
        lexical = strength / strongest if strongest > 0 else 1.0 - index / max(1, len(selected))
        refs = json.loads(row.get("refs_json") or "[]")
        supersedes = json.loads(row.get("supersedes_json") or "[]")
        provenance = list(dict.fromkeys([*refs, *supersedes]))
        out.append({
            "id": f"{row['path']}#{row['chunk_key']}", "text": row["text"],
            "source": row["source"], "path": row["path"],
            "lexical": round(float(lexical), 6), "source_type": row["source_type"],
            "visibility": row["visibility"],
            "automatic_eligible": bool(row["automatic_eligible"]), "at": row["at"],
            "automatic_canonical": purpose == "automatic",
            "event_id": row["event_id"], "run_id": row["run_id"],
            "desire_id": row.get("desire_id") or "",
            "refs": refs, "supersedes": supersedes, "provenance": provenance,
            "index": state.get("schema"),
        })
    return out


def upsert(path: str | Path, *, base: Path, memory_dir: Path,
           skills_dir: Path | None = None, db_path: Path | None = None) -> dict:
    """Refresh one eligible source; rebuild if the database is absent or incompatible."""
    base, memory_dir = Path(base), Path(memory_dir)
    target = Path(path).resolve()
    database = Path(db_path) if db_path is not None else _db_path(memory_dir)
    current = _meta(database)
    if current.get("schema") != SCHEMA_VERSION:
        return rebuild(base=base, memory_dir=memory_dir, skills_dir=skills_dir, db_path=database)
    sources = iter_sources(base=base, memory_dir=memory_dir, skills_dir=skills_dir)
    source = next((item for item in sources if item.path.resolve() == target), None)
    rel = _rel(target, base)
    with _LOCK, contextlib.closing(sqlite3.connect(database, timeout=15)) as db:
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("DELETE FROM chunks WHERE path = ?", (rel,))
        db.execute("DELETE FROM source_state WHERE path = ?", (rel,))
        count = corrupt = 0
        if source is not None:
            count, corrupt = _insert_source(db, source, memory_dir)
        fingerprint = _fingerprint(sources)
        totals = db.execute("SELECT COUNT(*), COUNT(DISTINCT path) FROM chunks").fetchone()
        corrupt_total = db.execute(
            "SELECT COALESCE(SUM(corrupt_lines), 0) FROM source_state"
        ).fetchone()[0]
        values = {
            "fingerprint": fingerprint, "chunks": str(int(totals[0] or 0)),
            "sources": str(len(sources)),
            "corrupt_lines": str(int(corrupt_total or 0)),
        }
        for key, value in values.items():
            db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
        db.commit()
    return {"ok": True, "path": rel, "chunks": count, "indexed": source is not None}


if __name__ == "__main__":
    import sys

    root = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
    mem = root / "memory"
    skills = root / "soul" / "skills"
    if len(sys.argv) > 1 and sys.argv[1] == "search":
        print(json.dumps(search(" ".join(sys.argv[2:]), base=root, memory_dir=mem,
                                skills_dir=skills), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(rebuild(base=root, memory_dir=mem, skills_dir=skills),
                         ensure_ascii=False, indent=2))
