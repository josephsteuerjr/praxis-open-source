"""Tamper-evident append-only audit for praxis-serverd broker v2.

Existing v1 JSONL is preserved byte-for-byte and committed by a chain-anchor record.  Every new
record hashes the previous record plus canonical JSON.  This does not pretend root cannot rewrite
its own disk; it makes any rewrite detectable and produces a pull-ready export for an off-box
collector that root cannot retroactively change.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
from pathlib import Path


ZERO = "0" * 64


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _canonical(row: dict) -> bytes:
    clean = {key: value for key, value in row.items() if key != "entry_hash"}
    return json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _entry_hash(previous: str, row: dict) -> str:
    return hashlib.sha256(previous.encode("ascii") + b"\n" + _canonical(row)).hexdigest()


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.last_hash = ZERO
        self.sequence = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self._load_or_anchor()
        self.startup_verify = self.verify()

    def _raw_lines(self) -> list[bytes]:
        try:
            return self.path.read_bytes().splitlines(keepends=True)
        except OSError:
            return []

    def _load_or_anchor(self) -> None:
        lines = self._raw_lines()
        chained = []
        for index, raw in enumerate(lines):
            try:
                row = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("entry_hash"):
                chained.append((index, row))
        if chained:
            self.last_hash = str(chained[-1][1].get("entry_hash") or ZERO)
            self.sequence = int(chained[-1][1].get("seq") or len(chained))
            return
        if not lines:
            return
        legacy = b"".join(lines)
        anchor = {
            "at": _now(), "kind": "chain_anchor", "status": "ok",
            "legacy_lines": len(lines), "legacy_sha256": hashlib.sha256(legacy).hexdigest(),
            "note": "v1 audit prefix committed byte-for-byte on broker v2 migration",
        }
        self._append_locked(anchor)

    def _append_locked(self, row: dict) -> dict:
        self.sequence += 1
        final = {**row, "seq": self.sequence, "prev_hash": self.last_hash}
        final["entry_hash"] = _entry_hash(self.last_hash, final)
        line = json.dumps(final, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        self.last_hash = final["entry_hash"]
        return final

    def append(self, row: dict) -> dict:
        with self.lock:
            return self._append_locked(dict(row))

    def tail(self, limit: int = 40) -> list[dict]:
        with self.lock:
            lines = self._raw_lines()[-max(1, min(int(limit or 40), 1000)):]
        out = []
        for raw in lines:
            try:
                row = json.loads(raw.decode("utf-8", "replace"))
                if isinstance(row, dict):
                    out.append(row)
            except ValueError:
                continue
        return out

    def status(self) -> dict:
        """O(1) health for frequent capability snapshots; explicit verify rescans the chain."""
        return {"ok": bool(self.startup_verify.get("ok")), "startup_verify": self.startup_verify,
                "last_hash": self.last_hash, "seq": self.sequence}

    def verify(self) -> dict:
        with self.lock:
            lines = self._raw_lines()
        previous = ZERO
        chain_started = False
        legacy_lines = 0
        chained = 0
        for index, raw in enumerate(lines):
            try:
                row = json.loads(raw.decode("utf-8", "replace"))
            except ValueError as exc:
                if not chain_started:
                    legacy_lines += 1
                    continue
                return {"ok": False, "error": f"line {index + 1}: invalid JSON: {exc}",
                        "legacy_lines": legacy_lines, "chained_entries": chained}
            if not isinstance(row, dict) or not row.get("entry_hash"):
                if chain_started:
                    return {"ok": False, "error": f"line {index + 1}: unchained row after chain start",
                            "legacy_lines": legacy_lines, "chained_entries": chained}
                legacy_lines += 1
                continue
            if not chain_started:
                chain_started = True
                if row.get("kind") == "chain_anchor":
                    legacy = b"".join(lines[:index])
                    if int(row.get("legacy_lines") or -1) != index:
                        return {"ok": False, "error": "chain anchor legacy line count mismatch"}
                    if row.get("legacy_sha256") != hashlib.sha256(legacy).hexdigest():
                        return {"ok": False, "error": "legacy prefix digest mismatch"}
            if row.get("prev_hash") != previous:
                return {"ok": False, "error": f"line {index + 1}: prev_hash mismatch",
                        "legacy_lines": legacy_lines, "chained_entries": chained}
            expected = _entry_hash(previous, row)
            if row.get("entry_hash") != expected:
                return {"ok": False, "error": f"line {index + 1}: entry_hash mismatch",
                        "legacy_lines": legacy_lines, "chained_entries": chained}
            previous = expected
            chained += 1
        return {"ok": True, "legacy_lines": legacy_lines, "chained_entries": chained,
                "last_hash": previous, "lines": len(lines)}

    def export(self, directory: Path) -> dict:
        """Create an immutable-by-name snapshot for a future off-box pull collector."""
        verdict = self.verify()
        if not verdict.get("ok"):
            return {"ok": False, "error": verdict.get("error"), "verify": verdict}
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        raw = self.path.read_bytes() if self.path.exists() else b""
        digest = hashlib.sha256(raw).hexdigest()
        target = directory / f"audit-{verdict.get('last_hash', ZERO)[:16]}.jsonl"
        if not target.exists():
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(raw)
            os.chmod(tmp, 0o444)
            os.replace(tmp, target)
        manifest = {"ok": True, "created": _now(), "path": str(target), "sha256": digest,
                    "bytes": len(raw), "verify": verdict}
        manifest_path = target.with_suffix(".manifest.json")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(manifest_path, 0o444)
        return manifest
