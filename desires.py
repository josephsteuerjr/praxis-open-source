"""Durable conation ledger for Praxis (PASS 24).

The ledger makes operational intention inspectable without pretending to prove
subjective experience.  Its canonical source is append-only JSONL.  A
grep-friendly Markdown projection is rebuilt from that source and may be
deleted/recreated at any time.

The causal path is explicit and ordered:

    noticed -> wanted -> chosen -> acted -> observed -> changed

An active or blocked desire may start another cycle at ``wanted`` after
``changed``.  Satisfied/released desires require an explicit ``reopen``.  Run
and evidence references travel with each event so a RECAP can update a desire
from facts rather than an imagined result.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any, Iterable

import memory_provenance
from self_model import FileLock, atomic_write, clean_refs, utc_now


SCHEMA = "praxis.desire.event.v1"
PROJECTION_SCHEMA = "praxis.desires.projection.v1"
STAGES = ("noticed", "wanted", "chosen", "acted", "observed", "changed")
STATUSES = ("latent", "active", "satisfied", "released", "blocked")
TERMINAL_STATUSES = frozenset({"satisfied", "released"})
MAX_STATE_REFS = 500
MAX_STATE_RUNS = 200
MAX_PROJECTION_DESIRES = 240
MAX_PROJECTION_TIMELINE = 120
MAX_PROJECTION_EVENT_REFS = 20
MAX_PROJECTION_NOTE_CHARS = 500
_NEXT_STAGE = {
    "noticed": "wanted",
    "wanted": "chosen",
    "chosen": "acted",
    "acted": "observed",
    "observed": "changed",
    "changed": "wanted",
}
_LOCAL_LOCK = threading.RLock()
_DESIRE_ID_RE = re.compile(r"^desire-[A-Za-z0-9][A-Za-z0-9._:-]{0,91}$")


def _new_id(prefix: str) -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    return f"{prefix}-{now:%Y%m%dT%H%M%S}-{secrets.token_hex(5)}"


def _clean_text(value: Any, *, limit: int, required: str = "") -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{required} is required")
    return text[:limit]


def _desire_id(value: Any, *, generated: bool = False) -> str:
    text = _clean_text(value, limit=100, required="desire_id")
    if not _DESIRE_ID_RE.fullmatch(text):
        hint = " (generated ids already satisfy this)" if generated else ""
        raise ValueError("desire_id must match desire-[A-Za-z0-9._:-]+" + hint)
    return text


def _yaml_scalar(value: Any) -> str:
    # JSON strings are valid YAML scalars and remain grep-friendly/unambiguous.
    return json.dumps(str(value or ""), ensure_ascii=False)


class DesireLedger:
    def __init__(self, base: str | Path | None = None):
        self.base = Path(base or os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
        self.events_path = self.base / "memory" / "desires" / "events.jsonl"
        self.projection_path = self.base / "memory" / "desires" / "CURRENT.md"
        self.lock_path = self.base / "memory" / ".state" / "desires.lock"

    def events(self, desire_id: str = "") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            lines = self.events_path.read_text(encoding="utf-8", errors="strict").splitlines()
        except OSError:
            return out
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                # A torn final append cannot invalidate the earlier durable log.
                continue
            if not isinstance(event, dict) or event.get("schema") != SCHEMA:
                continue
            if desire_id and event.get("desire_id") != desire_id:
                continue
            out.append(event)
        return out

    def _states_from(self, events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        states: dict[str, dict[str, Any]] = {}
        for event in events:
            did = str(event.get("desire_id") or "")
            snapshot = event.get("state")
            if not _DESIRE_ID_RE.fullmatch(did) or not isinstance(snapshot, dict):
                continue
            state = dict(snapshot)
            state["id"] = did
            state["timeline"] = list(states.get(did, {}).get("timeline") or []) + [{
                "event_id": event.get("event_id"),
                "ts": event.get("ts"),
                "event_type": event.get("event_type"),
                "stage": event.get("stage"),
                "note": event.get("note") or "",
                "run_id": event.get("run_id") or "",
                "evidence_refs": list(event.get("evidence_refs") or []),
            }]
            states[did] = state
        return states

    def states(self) -> dict[str, dict[str, Any]]:
        return self._states_from(self.events())

    def get(self, desire_id: str) -> dict[str, Any] | None:
        return self.states().get(_desire_id(desire_id))

    def list(self, *, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        selected = set(statuses or ())
        if selected - set(STATUSES):
            raise ValueError("unknown desire status: " + ", ".join(sorted(selected - set(STATUSES))))
        values = list(self.states().values())
        if selected:
            values = [state for state in values if state.get("status") in selected]
        return sorted(values, key=lambda state: (str(state.get("last_changed") or ""), state["id"]), reverse=True)

    @staticmethod
    def _snapshot(
        previous: dict[str, Any] | None,
        *,
        desire_id: str,
        stage: str,
        statement: str | None = None,
        source: str | None = None,
        why_it_matters: str | None = None,
        status: str | None = None,
        next_move: str | None = None,
        evidence_refs: Iterable[Any] = (),
        run_id: str = "",
        now: str,
        cycle: int | None = None,
    ) -> dict[str, Any]:
        old = dict(previous or {})
        refs = clean_refs(
            list(old.get("evidence_refs") or []) + list(evidence_refs or ())
        )[-MAX_STATE_REFS:]
        runs = clean_refs(
            list(old.get("run_ids") or []) + ([run_id] if run_id else [])
        )[-MAX_STATE_RUNS:]
        result = {
            "id": desire_id,
            "statement": _clean_text(statement if statement is not None else old.get("statement"),
                                     limit=2_000, required="statement"),
            "source": _clean_text(source if source is not None else old.get("source"),
                                  limit=2_000, required="source"),
            "why_it_matters": _clean_text(
                why_it_matters if why_it_matters is not None else old.get("why_it_matters"),
                limit=2_000,
                required="why_it_matters",
            ),
            "status": status or old.get("status") or "latent",
            "stage": stage,
            "next_move": _clean_text(next_move if next_move is not None else old.get("next_move"), limit=2_000),
            "evidence_refs": refs,
            "run_ids": runs,
            "created_at": old.get("created_at") or now,
            "last_changed": now,
            "cycle": int(cycle if cycle is not None else old.get("cycle") or 1),
        }
        if result["status"] not in STATUSES:
            raise ValueError(f"unknown desire status: {result['status']}")
        return result

    def _append_locked(self, event: dict[str, Any]) -> dict[str, Any]:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self._repair_torn_tail_locked()
        fd = os.open(str(self.events_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return event

    def _repair_torn_tail_locked(self) -> None:
        """A crashed partial JSON row must not swallow the next valid event."""
        try:
            data = self.events_path.read_bytes()
        except OSError:
            return
        if not data or data.endswith(b"\n"):
            return
        boundary = data.rfind(b"\n") + 1
        with self.events_path.open("r+b") as fh:
            fh.truncate(boundary)
            fh.flush()
            os.fsync(fh.fileno())

    def _event(
        self,
        *,
        desire_id: str,
        event_type: str,
        stage: str,
        state: dict[str, Any],
        note: str,
        actor: str,
        evidence_refs: Iterable[Any],
        run_id: str,
        dedupe_key: str,
        now: str,
    ) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "event_id": _new_id("devent"),
            "desire_id": desire_id,
            "ts": now,
            "event_type": event_type,
            "stage": stage,
            "actor": _clean_text(actor or "praxis", limit=100),
            "note": _clean_text(note, limit=4_000),
            "evidence_refs": clean_refs(evidence_refs)[-MAX_STATE_REFS:],
            "run_id": _clean_text(re.sub(r"[\r\n\t]+", " ", str(run_id or "")), limit=200),
            "dedupe_key": _clean_text(dedupe_key, limit=300),
            "state": state,
        }

    def _find_dedupe(self, dedupe_key: str) -> dict[str, Any] | None:
        if not dedupe_key:
            return None
        for event in reversed(self.events()):
            if event.get("dedupe_key") == dedupe_key:
                return event
        return None

    def _write_projection_locked(self) -> Path:
        events = self.events()
        states = self._states_from(events)
        # Active directions come first; within each class the newest state wins.
        # Stable passes keep ties deterministic without encoding an inverted ISO
        # timestamp trick into the projection contract.
        ordered = sorted(states.values(), key=lambda item: item["id"])
        ordered.sort(key=lambda item: str(item.get("last_changed") or ""), reverse=True)
        ordered.sort(key=lambda item: (
            0 if item.get("status") == "active" else
            1 if item.get("status") == "latent" else
            2 if item.get("status") == "blocked" else 3
        ))
        total_desires = len(ordered)
        ordered = ordered[:MAX_PROJECTION_DESIRES]
        # Derived output stays byte-stable across rebuilds of the same canon.
        generated = max((str(event.get("ts") or "") for event in events), default="")
        lines = [
            "<!-- praxis-generated: " + json.dumps(
                {"schema": PROJECTION_SCHEMA, "source": "memory/desires/events.jsonl", "generated_at": generated},
                ensure_ascii=False,
                separators=(",", ":"),
            ) + " -->",
            "# Желания Praxis",
            "",
            "_Проекция append-only ledger. Канон: `memory/desires/events.jsonl`; этот файл можно пересобрать._",
            "",
            f"_Показано желаний: {len(ordered)} из {total_desires}; "
            f"до {MAX_PROJECTION_TIMELINE} последних событий на желание._",
        ]
        if not ordered:
            lines += ["", "Активных или архивных желаний пока нет."]
        for state in ordered:
            lines += [
                "",
                f"## {state['id']} — {state['status']}",
                f"statement: {_yaml_scalar(state.get('statement'))}",
                f"source: {_yaml_scalar(state.get('source'))}",
                f"why_it_matters: {_yaml_scalar(state.get('why_it_matters'))}",
                f"status: {state.get('status')}",
                f"stage: {state.get('stage')}",
                f"cycle: {state.get('cycle')}",
                f"next_move: {_yaml_scalar(state.get('next_move'))}",
                f"created_at: {state.get('created_at')}",
                f"last_changed: {state.get('last_changed')}",
                "evidence_refs: " + json.dumps(state.get("evidence_refs") or [], ensure_ascii=False),
                "run_ids: " + json.dumps(state.get("run_ids") or [], ensure_ascii=False),
                "",
                "### Причинная цепочка",
            ]
            timeline = list(state.get("timeline") or [])
            omitted_timeline = max(0, len(timeline) - MAX_PROJECTION_TIMELINE)
            if omitted_timeline:
                lines.append(
                    f"- … ещё {omitted_timeline} ранних событий; канон остаётся в events.jsonl"
                )
            for item in timeline[-MAX_PROJECTION_TIMELINE:]:
                refs = ", ".join(
                    list(item.get("evidence_refs") or [])[-MAX_PROJECTION_EVENT_REFS:]
                )
                suffix = ""
                if item.get("run_id"):
                    suffix += f"; run={item['run_id']}"
                if refs:
                    suffix += f"; evidence={refs}"
                note = str(item.get("note") or "").replace("\n", " ")[:MAX_PROJECTION_NOTE_CHARS]
                event_label = item.get("stage") if item.get("event_type") == "transition" else item.get("event_type")
                lines.append(f"- {item.get('ts')} `{event_label}` — {note or '—'}{suffix}")
        atomic_write(self.projection_path, ("\n".join(lines).rstrip() + "\n").encode("utf-8"))
        return self.projection_path

    def rebuild_projection(self) -> dict[str, Any]:
        with _LOCAL_LOCK, FileLock(self.lock_path):
            path = self._write_projection_locked()
            return {
                "ok": True,
                "path": path.relative_to(self.base).as_posix(),
                "desires": len(self.states()),
                "events": len(self.events()),
            }

    def _append_and_project(self, event: dict[str, Any]) -> dict[str, Any]:
        self._append_locked(event)
        try:
            self._write_projection_locked()
            event["projection_ok"] = True
        except Exception as exc:  # canonical event is already durable; report degraded view honestly
            event["projection_ok"] = False
            event["projection_error"] = type(exc).__name__
        return event

    def notice(
        self,
        statement: str,
        *,
        source: str,
        why_it_matters: str,
        next_move: str = "",
        evidence_refs: Iterable[Any] = (),
        run_id: str = "",
        actor: str = "praxis",
        desire_id: str = "",
        dedupe_key: str = "",
    ) -> dict[str, Any]:
        """Create a latent candidate at the ``noticed`` stage."""
        with _LOCAL_LOCK, FileLock(self.lock_path):
            duplicate = self._find_dedupe(dedupe_key)
            if duplicate:
                return {**duplicate, "deduplicated": True}
            did = _desire_id(desire_id) if str(desire_id or "").strip() else _new_id("desire")
            if self.get(did):
                raise ValueError(f"desire already exists: {did}")
            now = utc_now()
            refs = clean_refs(evidence_refs)
            memory_provenance.require_normative_provenance(source=source, refs=refs)
            state = self._snapshot(
                None,
                desire_id=did,
                stage="noticed",
                statement=statement,
                source=source,
                why_it_matters=why_it_matters,
                status="latent",
                next_move=next_move,
                evidence_refs=refs,
                run_id=run_id,
                now=now,
                cycle=1,
            )
            event = self._event(
                desire_id=did,
                event_type="transition",
                stage="noticed",
                state=state,
                note="candidate noticed",
                actor=actor,
                evidence_refs=refs,
                run_id=run_id,
                dedupe_key=dedupe_key,
                now=now,
            )
            return self._append_and_project(event)

    def advance(
        self,
        desire_id: str,
        stage: str,
        *,
        note: str,
        status: str = "",
        next_move: str | None = None,
        statement: str | None = None,
        why_it_matters: str | None = None,
        evidence_refs: Iterable[Any] = (),
        run_id: str = "",
        actor: str = "praxis",
        dedupe_key: str = "",
    ) -> dict[str, Any]:
        """Move one causal step, rejecting skipped or out-of-order evidence."""
        desired_stage = _clean_text(stage, limit=30, required="stage")
        if desired_stage not in STAGES:
            raise ValueError(f"unknown desire stage: {desired_stage}")
        explanation = _clean_text(note, limit=4_000, required="transition note")
        desire_id = _desire_id(desire_id)
        with _LOCAL_LOCK, FileLock(self.lock_path):
            duplicate = self._find_dedupe(dedupe_key)
            if duplicate:
                return {**duplicate, "deduplicated": True}
            current = self.get(desire_id)
            if not current:
                raise KeyError(f"unknown desire: {desire_id}")
            if not memory_provenance.desire_state_normative_eligible(current):
                raise ValueError(
                    "historical desire is rooted in untrusted journal evidence; "
                    "create a new independently verified desire instead"
                )
            expected = _NEXT_STAGE[current["stage"]]
            if desired_stage != expected:
                raise ValueError(
                    f"causal stage must follow {current['stage']} -> {expected}; got {desired_stage}"
                )
            if current.get("status") in TERMINAL_STATUSES and current["stage"] == "changed":
                raise ValueError(f"{current['status']} desire requires explicit reopen")
            if desired_stage == "changed" and not status:
                raise ValueError("changed stage requires an explicit status")
            if status and status not in STATUSES:
                raise ValueError(f"unknown desire status: {status}")
            if desired_stage != "changed" and status in TERMINAL_STATUSES:
                raise ValueError("satisfied/released status requires observed -> changed evidence")
            resolved_status = status or ("active" if desired_stage in {"wanted", "chosen", "acted", "observed"}
                                         else current.get("status") or "latent")
            cycle = int(current.get("cycle") or 1) + (1 if current["stage"] == "changed" else 0)
            now = utc_now()
            refs = clean_refs(evidence_refs)
            memory_provenance.require_normative_provenance(refs=refs)
            state = self._snapshot(
                current,
                desire_id=desire_id,
                stage=desired_stage,
                statement=statement,
                why_it_matters=why_it_matters,
                status=resolved_status,
                next_move=next_move,
                evidence_refs=refs,
                run_id=run_id,
                now=now,
                cycle=cycle,
            )
            event = self._event(
                desire_id=desire_id,
                event_type="transition",
                stage=desired_stage,
                state=state,
                note=explanation,
                actor=actor,
                evidence_refs=refs,
                run_id=run_id,
                dedupe_key=dedupe_key,
                now=now,
            )
            return self._append_and_project(event)

    def want(self, desire_id: str, *, note: str, **kwargs: Any) -> dict[str, Any]:
        return self.advance(desire_id, "wanted", note=note, **kwargs)

    def choose(self, desire_id: str, *, note: str, **kwargs: Any) -> dict[str, Any]:
        return self.advance(desire_id, "chosen", note=note, **kwargs)

    def act(self, desire_id: str, *, note: str, **kwargs: Any) -> dict[str, Any]:
        return self.advance(desire_id, "acted", note=note, **kwargs)

    def observe(self, desire_id: str, *, note: str, **kwargs: Any) -> dict[str, Any]:
        return self.advance(desire_id, "observed", note=note, **kwargs)

    def change(self, desire_id: str, *, note: str, status: str, **kwargs: Any) -> dict[str, Any]:
        return self.advance(desire_id, "changed", note=note, status=status, **kwargs)

    def link_run(
        self,
        desire_id: str,
        run_id: str,
        *,
        note: str,
        evidence_refs: Iterable[Any] = (),
        actor: str = "praxis",
        dedupe_key: str = "",
    ) -> dict[str, Any]:
        """Link a spawned/adopted run without faking a causal-stage transition."""
        rid = _clean_text(re.sub(r"[\r\n\t]+", " ", str(run_id or "")),
                          limit=200, required="run_id")
        explanation = _clean_text(note, limit=4_000, required="run link note")
        desire_id = _desire_id(desire_id)
        with _LOCAL_LOCK, FileLock(self.lock_path):
            duplicate = self._find_dedupe(dedupe_key)
            if duplicate:
                return {**duplicate, "deduplicated": True}
            current = self.get(desire_id)
            if not current:
                raise KeyError(f"unknown desire: {desire_id}")
            if not memory_provenance.desire_state_normative_eligible(current):
                raise ValueError(
                    "historical desire is rooted in untrusted journal evidence; "
                    "create a new independently verified desire instead"
                )
            now = utc_now()
            refs = clean_refs(evidence_refs)
            memory_provenance.require_normative_provenance(refs=refs)
            state = self._snapshot(
                current,
                desire_id=desire_id,
                stage=current["stage"],
                evidence_refs=refs,
                run_id=rid,
                now=now,
            )
            event = self._event(
                desire_id=desire_id,
                event_type="run_link",
                stage=current["stage"],
                state=state,
                note=explanation,
                actor=actor,
                evidence_refs=refs,
                run_id=rid,
                dedupe_key=dedupe_key,
                now=now,
            )
            return self._append_and_project(event)

    def update_from_run(
        self,
        desire_id: str,
        run_id: str,
        *,
        observation: str,
        change_note: str,
        status: str,
        next_move: str = "",
        evidence_refs: Iterable[Any] = (),
        actor: str = "praxis",
        dedupe_key: str = "",
    ) -> dict[str, Any]:
        """Apply a factual run outcome as ``observed`` then ``changed``.

        The method intentionally requires the desire to be at ``acted``.  It
        writes two separately auditable events; a retry can use ``dedupe_key``.
        """
        if status not in STATUSES:
            raise ValueError(f"unknown desire status: {status}")
        if dedupe_key:
            duplicate = self._find_dedupe(f"{dedupe_key}:changed")
            if duplicate:
                return {"ok": True, "deduplicated": True, "changed": duplicate}
        observed_key = f"{dedupe_key}:observed" if dedupe_key else ""
        observed = self._find_dedupe(observed_key) if observed_key else None
        if observed is not None:
            # The process may have died after the first durable event and
            # before `changed`.  Resume from observed instead of trying the
            # acted->observed transition twice.
            current = self.get(desire_id)
            if not current or current.get("stage") != "observed":
                raise ValueError(
                    "observed receipt exists but desire is no longer at observed; "
                    "manual reconciliation is required"
                )
            observed = {**observed, "deduplicated": True}
        else:
            observed = self.observe(
                desire_id,
                note=observation,
                evidence_refs=evidence_refs,
                run_id=run_id,
                actor=actor,
                dedupe_key=observed_key,
            )
        changed = self.change(
            desire_id,
            note=change_note,
            status=status,
            next_move=next_move,
            evidence_refs=evidence_refs,
            run_id=run_id,
            actor=actor,
            dedupe_key=f"{dedupe_key}:changed" if dedupe_key else "",
        )
        return {"ok": True, "observed": observed, "changed": changed}

    def reopen(
        self,
        desire_id: str,
        *,
        note: str,
        next_move: str,
        evidence_refs: Iterable[Any] = (),
        run_id: str = "",
        actor: str = "praxis",
        dedupe_key: str = "",
    ) -> dict[str, Any]:
        """Explicitly reactivate a satisfied/released desire at a new wanted cycle."""
        desire_id = _desire_id(desire_id)
        with _LOCAL_LOCK, FileLock(self.lock_path):
            duplicate = self._find_dedupe(dedupe_key)
            if duplicate:
                return {**duplicate, "deduplicated": True}
            current = self.get(desire_id)
            if not current:
                raise KeyError(f"unknown desire: {desire_id}")
            if not memory_provenance.desire_state_normative_eligible(current):
                raise ValueError(
                    "historical desire is rooted in untrusted journal evidence; "
                    "create a new independently verified desire instead"
                )
            if current.get("stage") != "changed" or current.get("status") not in TERMINAL_STATUSES:
                raise ValueError("reopen is only for satisfied/released desires at changed stage")
            now = utc_now()
            refs = clean_refs(evidence_refs)
            memory_provenance.require_normative_provenance(refs=refs)
            state = self._snapshot(
                current,
                desire_id=desire_id,
                stage="wanted",
                status="active",
                next_move=next_move,
                evidence_refs=refs,
                run_id=run_id,
                now=now,
                cycle=int(current.get("cycle") or 1) + 1,
            )
            event = self._event(
                desire_id=desire_id,
                event_type="transition",
                stage="wanted",
                state=state,
                note=_clean_text(note, limit=4_000, required="reopen note"),
                actor=actor,
                evidence_refs=refs,
                run_id=run_id,
                dedupe_key=dedupe_key,
                now=now,
            )
            return self._append_and_project(event)


def _ledger(base: str | Path | None = None) -> DesireLedger:
    return DesireLedger(base)


def notice(statement: str, *, base: str | Path | None = None, **kwargs: Any) -> dict[str, Any]:
    return _ledger(base).notice(statement, **kwargs)


def get(desire_id: str, *, base: str | Path | None = None) -> dict[str, Any] | None:
    return _ledger(base).get(desire_id)


def active(*, base: str | Path | None = None) -> list[dict[str, Any]]:
    return _ledger(base).list(statuses={"latent", "active", "blocked"})


def advance(desire_id: str, stage: str, *, base: str | Path | None = None,
            **kwargs: Any) -> dict[str, Any]:
    return _ledger(base).advance(desire_id, stage, **kwargs)


def want(desire_id: str, *, base: str | Path | None = None, **kwargs: Any) -> dict[str, Any]:
    return _ledger(base).want(desire_id, **kwargs)


def choose(desire_id: str, *, base: str | Path | None = None, **kwargs: Any) -> dict[str, Any]:
    return _ledger(base).choose(desire_id, **kwargs)


def act(desire_id: str, *, base: str | Path | None = None, **kwargs: Any) -> dict[str, Any]:
    return _ledger(base).act(desire_id, **kwargs)


def observe(desire_id: str, *, base: str | Path | None = None, **kwargs: Any) -> dict[str, Any]:
    return _ledger(base).observe(desire_id, **kwargs)


def change(desire_id: str, *, base: str | Path | None = None, **kwargs: Any) -> dict[str, Any]:
    return _ledger(base).change(desire_id, **kwargs)


def link_run(desire_id: str, run_id: str, *, base: str | Path | None = None,
             **kwargs: Any) -> dict[str, Any]:
    return _ledger(base).link_run(desire_id, run_id, **kwargs)


def update_from_run(desire_id: str, run_id: str, *, base: str | Path | None = None,
                    **kwargs: Any) -> dict[str, Any]:
    return _ledger(base).update_from_run(desire_id, run_id, **kwargs)


def reopen(desire_id: str, *, base: str | Path | None = None, **kwargs: Any) -> dict[str, Any]:
    return _ledger(base).reopen(desire_id, **kwargs)


def rebuild_projection(*, base: str | Path | None = None) -> dict[str, Any]:
    return _ledger(base).rebuild_projection()
