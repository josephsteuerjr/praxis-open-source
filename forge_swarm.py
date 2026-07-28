"""Persistent DAG, mailbox and advisory ownership for Praxis Forge workers."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Callable


KINDS = {"finding", "question", "blocker", "contract", "result", "claim", "release"}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _load(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _plan_path(task_dir: Path) -> Path:
    return task_dir / "swarm.json"


def load(task_dir: Path) -> dict:
    row = _load(_plan_path(task_dir), {})
    return row if isinstance(row, dict) else {}


def _node_id(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-.")
    return clean[:64]


def create(task_dir: Path, raw_plan: str | list | dict, *, max_parallel: int = 3) -> dict:
    if isinstance(raw_plan, str):
        try:
            raw_plan = json.loads(raw_plan)
        except ValueError as exc:
            raise ValueError(f"plan is not valid JSON: {exc}") from exc
    if isinstance(raw_plan, dict):
        raw_nodes = raw_plan.get("nodes") or []
    else:
        raw_nodes = raw_plan or []
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("plan needs a non-empty nodes list")
    nodes = []
    ids: set[str] = set()
    for index, item in enumerate(raw_nodes):
        if not isinstance(item, dict):
            raise ValueError(f"node {index + 1} is not an object")
        nid = _node_id(item.get("id") or f"step-{index + 1}")
        if not nid or nid in ids:
            raise ValueError(f"duplicate/empty node id: {nid!r}")
        ids.add(nid)
        role = str(item.get("role") or "worker").strip().lower()
        if role not in {"scout", "worker", "reviewer"}:
            raise ValueError(f"node {nid}: role must be scout|worker|reviewer")
        brief = str(item.get("brief") or "").strip()
        if not brief:
            raise ValueError(f"node {nid}: brief is required")
        deps = [_node_id(dep) for dep in (item.get("deps") or [])]
        owns = [str(path).replace("\\", "/") for path in (item.get("owns") or []) if str(path).strip()]
        nodes.append({"id": nid, "role": role, "brief": brief, "deps": deps,
                      "owns": owns, "status": "pending", "agent_id": ""})
    for node in nodes:
        missing = [dep for dep in node["deps"] if dep not in ids]
        if missing:
            raise ValueError(f"node {node['id']}: unknown deps {missing}")
        if node["id"] in node["deps"]:
            raise ValueError(f"node {node['id']}: self dependency")
    graph = {node["id"]: node["deps"] for node in nodes}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(nid: str) -> None:
        if nid in visiting:
            raise ValueError(f"cycle at {nid}")
        if nid in visited:
            return
        visiting.add(nid)
        for dep in graph[nid]:
            visit(dep)
        visiting.remove(nid)
        visited.add(nid)

    for nid in graph:
        visit(nid)
    plan = {"version": 1, "created": _now(), "updated": _now(),
            "max_parallel": max(1, min(int(max_parallel or 3), 8)), "nodes": nodes}
    _atomic(_plan_path(task_dir), plan)
    return plan


def refresh(task_dir: Path, agent_state: Callable[[str], dict]) -> dict:
    plan = load(task_dir)
    if not plan:
        return {}
    changed = False
    for node in plan.get("nodes") or []:
        aid = str(node.get("agent_id") or "")
        if not aid or node.get("status") not in {"starting", "running"}:
            continue
        state = agent_state(aid)
        status = str(state.get("status") or "lost")
        mapped = {"done": "done", "error": "failed", "failed": "failed",
                  "stopped": "stopped", "lost": "lost"}.get(status, "running")
        if mapped != node.get("status"):
            node["status"] = mapped
            node["finished"] = state.get("finished", "") if mapped != "running" else ""
            changed = True
    if changed:
        plan["updated"] = _now()
        _atomic(_plan_path(task_dir), plan)
    return plan


def ready_nodes(plan: dict) -> list[dict]:
    nodes = plan.get("nodes") or []
    status = {node.get("id"): node.get("status") for node in nodes}
    running = sum(1 for node in nodes if node.get("status") in {"starting", "running"})
    slots = max(0, int(plan.get("max_parallel") or 1) - running)
    ready = []
    for node in nodes:
        if node.get("status") != "pending":
            continue
        deps = node.get("deps") or []
        if all(status.get(dep) == "done" for dep in deps):
            ready.append(node)
        elif any(status.get(dep) in {"failed", "stopped", "lost", "blocked"} for dep in deps):
            node["status"] = "blocked"
            node["finished"] = _now()
    return ready[:slots]


def save(task_dir: Path, plan: dict) -> None:
    plan["updated"] = _now()
    _atomic(_plan_path(task_dir), plan)


def signal(task_dir: Path, *, node_id: str = "", agent_id: str = "", kind: str,
           message: str, files: list[str] | None = None) -> dict:
    kind = str(kind or "finding").strip().lower()
    if kind not in KINDS:
        raise ValueError("kind: " + " | ".join(sorted(KINDS)))
    row = {"at": _now(), "node_id": _node_id(node_id), "agent_id": str(agent_id or ""),
           "kind": kind, "message": str(message or "").strip()[:12000],
           "files": [str(path).replace("\\", "/") for path in (files or [])][:100]}
    if not row["message"] and kind not in {"claim", "release"}:
        raise ValueError("message is required")
    mailbox = task_dir / "mailbox.jsonl"
    mailbox.parent.mkdir(parents=True, exist_ok=True)
    with mailbox.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    if kind in {"claim", "release"}:
        claims_path = task_dir / "claims.json"
        claims = _load(claims_path, {})
        claims = claims if isinstance(claims, dict) else {}
        owner = row["node_id"] or row["agent_id"] or "unknown"
        conflicts = []
        for file in row["files"]:
            previous = claims.get(file)
            if kind == "claim":
                if previous and previous != owner:
                    conflicts.append({"file": file, "owner": previous})
                claims[file] = owner
            elif previous == owner:
                claims.pop(file, None)
        _atomic(claims_path, claims)
        row["conflicts"] = conflicts
    return row


def mailbox(task_dir: Path, limit: int = 100) -> list[dict]:
    try:
        lines = (task_dir / "mailbox.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-max(1, min(limit, 500)):]:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                out.append(row)
        except ValueError:
            continue
    return out


def summary(plan: dict, messages: list[dict] | None = None) -> str:
    if not plan:
        return "У задачи нет swarm-плана."
    lines = [f"swarm max_parallel={plan.get('max_parallel')} updated={plan.get('updated')}"]
    for node in plan.get("nodes") or []:
        deps = ",".join(node.get("deps") or []) or "-"
        owns = ",".join(node.get("owns") or []) or "-"
        lines.append(f"- {node.get('id')} [{node.get('status')}] {node.get('role')} "
                     f"agent={node.get('agent_id') or '-'} deps={deps} owns={owns}: {node.get('brief')}")
    if messages:
        lines.append("mailbox:")
        for row in messages[-20:]:
            lines.append(f"- {row.get('at')} {row.get('kind')} {row.get('node_id') or row.get('agent_id')}: "
                         f"{row.get('message') or ','.join(row.get('files') or [])}")
    return "\n".join(lines)
