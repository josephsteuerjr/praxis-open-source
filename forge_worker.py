"""Independent model process used by Praxis Forge coding subagents.

Each worker gets a fresh context and task-bound tools.  A scout/reviewer is
read-oriented by construction; a worker can edit, apply patches, run commands,
start background processes, checkpoint and delegate further fresh-context
agents.  Results are files, so the parent Praxis process can poll them after a
restart and several workers can run concurrently.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import traceback
from pathlib import Path

import forge
import llm


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _obj(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


INSPECT_TOOL = {
    "name": "inspect",
    "description": (
        "See the exact task place. Besides read/search/diff, model/symbols/references/diagnostics/"
        "impact/checks expose normalized semantic and test facts; mailbox shows worker signals."
    ),
    "input_schema": _obj({
        "action": {"type": "string", "enum": ["status", "orientation", "overview", "review", "model", "symbols",
                    "references", "diagnostics", "impact", "checks", "lessons", "mailbox",
                    "read", "list", "search", "diff", "history"]},
        "path": {"type": "string"}, "query": {"type": "string"},
        "glob": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"},
    }, ["action"]),
}

VERIFY_TOOL = {
    "name": "verify",
    "description": (
        "Plan or supervise a durable verification matrix. plan derives targeted checks from the "
        "actual diff/impact map; start runs checks outside this model turn; poll gathers exact logs."
    ),
    "input_schema": _obj({
        "action": {"type": "string", "enum": ["plan", "start", "poll", "stop", "list"]},
        "verification_id": {"type": "string"}, "commands": {"type": "string"},
        "full": {"type": "boolean"}, "max_parallel": {"type": "integer"},
        "timeout": {"type": "integer"}, "tail": {"type": "integer"},
    }, ["action"]),
}

SIGNAL_TOOL = {
    "name": "signal",
    "description": (
        "Send a structured finding/question/blocker/contract/result to the shared swarm mailbox, "
        "or claim/release files as advisory ownership. Claims surface conflicts; they never veto edits."
    ),
    "input_schema": _obj({
        "kind": {"type": "string", "enum": ["finding", "question", "blocker", "contract",
                                                    "result", "claim", "release"]},
        "message": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}},
    }, ["kind"]),
}

EDIT_TOOL = {
    "name": "edit",
    "description": (
        "Edit inside this task root. replace is an exact unique replacement; write creates or "
        "rewrites a file; patch applies a unified diff only after git apply --check. Pass the "
        "sha256 returned by inspect(read) as expected_sha256 when another worker may race you."
    ),
    "input_schema": _obj({
        "action": {"type": "string", "enum": ["replace", "write", "patch"]},
        "path": {"type": "string"}, "content": {"type": "string"},
        "old": {"type": "string"}, "new": {"type": "string"},
        "patch": {"type": "string"}, "expected_sha256": {"type": "string"},
    }, ["action"]),
}

RUN_TOOL = {
    "name": "run",
    "description": (
        "Run an arbitrary foreground shell command in the task. Full output is kept as evidence; "
        "the reply is capped. timeout=0 means no timeout. Use this to test, build, lint and probe."
    ),
    "input_schema": _obj({"command": {"type": "string"}, "cwd": {"type": "string"},
                           "timeout": {"type": "integer"}}, ["command"]),
}

PROCESS_TOOL = {
    "name": "process",
    "description": (
        "Start/poll/stop/list a long-running command without blocking this agent. start returns an "
        "id immediately and keeps the complete log. timeout=0 is unbounded."
    ),
    "input_schema": _obj({
        "action": {"type": "string", "enum": ["start", "poll", "stop", "list"]},
        "process_id": {"type": "string"}, "command": {"type": "string"},
        "cwd": {"type": "string"}, "name": {"type": "string"},
        "timeout": {"type": "integer"}, "tail": {"type": "integer"},
    }, ["action"]),
}

DELEGATE_TOOL = {
    "name": "delegate",
    "description": (
        "Spawn another independent fresh-context coding agent on this same task. Use several scouts "
        "for parallel reconnaissance, a worker for a separable implementation, or a reviewer for a "
        "hostile second look. Returns immediately; poll it through inspect(status) or ask the parent."
    ),
    "input_schema": _obj({
        "brief": {"type": "string"},
        "role": {"type": "string", "enum": ["scout", "worker", "reviewer"]},
        "max_iters": {"type": "integer"},
    }, ["brief", "role"]),
}

CHECKPOINT_TOOL = {
    "name": "checkpoint",
    "description": "Commit the current task working tree as a recoverable checkpoint.",
    "input_schema": _obj({"message": {"type": "string"}}, ["message"]),
}


def _system(request: dict) -> str:
    role = request["role"]
    stance = {
        "scout": "You are a reconnaissance agent. Inspect broadly, run non-mutating probes when useful, and return concrete evidence and file/line references.",
        "reviewer": "You are an adversarial code reviewer. Read the actual diff and relevant code, run focused checks, and identify real defects or explicitly clear it.",
        "worker": "You are an implementation agent. Orient, edit the real isolated working tree, run checks, and leave it materially closer to done. Do not stop at recommendations when you can act.",
    }[role]
    orientation = forge.inspect(request["task_id"], "orientation")
    return f"""You are a fresh-context coding subagent created by Praxis, not a conversational assistant.

Overall goal: {request.get('goal')}
Your brief: {request.get('brief')}
Your role: {role}
{stance}

FACTUAL ORIENTATION
{orientation}

Operating contract:
- The task root is your exact address. Inspect current files and git state; never invent project facts.
- Work autonomously and use as many tool turns as the job needs. The parent can run other agents concurrently.
- For concurrency-sensitive edits, read the file hash and pass expected_sha256; a mismatch is a signal to rebase your reasoning, not to overwrite blindly.
- `run` is arbitrary shell for build/test/probes. `process` is for servers, watchers and other long-lived commands. `delegate` can create further fresh-context specialists.
- Use semantic `inspect` actions before broad text guessing. Use `verify` for a persisted test matrix, and `signal` to leave findings/contracts/blockers in the shared DAG mailbox.
- Never put credentials or secret contents in your prose. Code facts, commands, diffs and test output are welcome.
- End with a compact but complete result: what you learned/changed, exact verification, remaining risks, and the next useful integration step. Do not merely narrate intentions.
"""


def _dispatch(request: dict, name: str, args: dict) -> str:
    task_id = request["task_id"]
    clean = {k: v for k, v in (args or {}).items() if v is not None}
    if name == "inspect":
        return forge.inspect(task_id, **clean)
    if name == "edit":
        return forge.edit(task_id, **clean)
    if name == "run":
        return forge.run(task_id, **clean)
    if name == "process":
        return forge.process(task_id, **clean)
    if name == "verify":
        return forge.verify(task_id, **clean)
    if name == "signal":
        return forge.swarm(task_id, "signal", node_id=str(request.get("node_id") or request.get("id") or ""),
                           kind=str(clean.get("kind") or "finding"),
                           message=str(clean.get("message") or ""), files=clean.get("files") or [])
    if name == "delegate":
        # расписка манометра: бриф сочинил ЭТОТ воркер, не она напрямую
        clean.setdefault("spawned_by", str(request.get("id") or ""))
        return forge.agent(task_id, "spawn", **clean)
    if name == "checkpoint":
        return forge.checkpoint(task_id, **clean)
    return f"Unknown tool {name}"


def run(request_path: Path) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    result_path = request_path.parent / "result.json"
    trace: list[dict] = []
    try:
        role = request.get("role") or "worker"
        tools = [INSPECT_TOOL, RUN_TOOL, PROCESS_TOOL, VERIFY_TOOL, SIGNAL_TOOL, DELEGATE_TOOL]
        if role == "worker":
            tools += [EDIT_TOOL, CHECKPOINT_TOOL]
        messages: list[dict] = [{"role": "user", "content": str(request.get("brief") or "Execute the brief.")}]
        max_iters = int(request.get("max_iters") or 0) or max(20, llm.limits().max_tool_iters * 2)
        reply = ""
        final_model = ""
        for _ in range(max_iters):
            response = llm.chat("voice", system=_system(request), messages=messages, tools=tools)
            final_model = response.model
            if response.stop_reason != "tool_use":
                reply = response.text.strip()
                break
            assistant_blocks, tool_results = [], []
            for block in response.blocks:
                if block.get("type") == "text":
                    assistant_blocks.append(block)
                elif block.get("type") == "tool_use":
                    assistant_blocks.append(block)
                    args = {k: v for k, v in (block.get("input") or {}).items() if v is not None}
                    try:
                        output = _dispatch(request, block.get("name", ""), args)
                    except Exception as exc:
                        output = f"Tool error {type(exc).__name__}: {exc}"
                    trace.append({"tool": block.get("name"), "input": args,
                                  "output": str(output)[:2000], "at": _now()})
                    tool_results.append({"type": "tool_result", "tool_use_id": block.get("id", ""),
                                         "content": str(output)[:12000]})
            messages.append({"role": "assistant", "content": assistant_blocks})
            messages.append({"role": "user", "content": tool_results})
        if not reply:
            reply = llm.chat("voice", system=_system(request), messages=messages,
                             tools=None).text.strip()
        diff = forge.inspect(request["task_id"], "diff")
        data = {
            "status": "done", "finished": _now(), "role": role, "model": final_model,
            "result": reply, "tool_calls": len(trace), "trace": trace[-30:],
            "diff_tail": diff[-8000:],
        }
        _write(result_path, data)
        forge._event(request["task_id"], "agent_finished", agent_id=request["id"], role=role,
                     summary=f"{request['id']} done; tools={len(trace)}")
        # PASS 30 Этап 1: завершение — входящее событие родительской петли, не повод
        # ждать таймера. Пишем из СВОЕГО процесса, durable; best-effort (не роняет выход).
        forge.emit_unit_event(request["task_id"], request["id"], data, request=request)
        print(f"{request['id']} done; tools={len(trace)}", flush=True)
        return 0
    except Exception as exc:
        data = {"status": "error", "finished": _now(),
                "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-8000:],
                "trace": trace[-20:]}
        _write(result_path, data)
        try:
            forge._event(request["task_id"], "agent_error", agent_id=request.get("id"),
                         summary=data["error"])
        except Exception:
            pass
        forge.emit_unit_event(request.get("task_id") or "", request.get("id") or "",
                              data, request=request)
        print(data["traceback"], flush=True)
        return 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    raise SystemExit(run(Path(args.request)))
