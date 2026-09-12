#!/usr/bin/env python3
"""Deterministic stdio control-protocol peer; never contacts a model."""
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

if "--version" in sys.argv:
    print("2.1.261 (simulated Claude Code)")
    raise SystemExit()


def arg(name):
    for value in sys.argv:
        if value.startswith(name + "="):
            return value[len(name) + 1:]
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def emit(obj):
    print(json.dumps(obj), flush=True)


session = arg("--session-id") or arg("--resume")
root = Path(os.environ["CLAUDE_CONFIG_DIR"])
root.mkdir(parents=True, exist_ok=True)
transcript = root / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()) / (session + ".jsonl")
callbacks = {}
pending = None

for line in sys.stdin:
    message = json.loads(line)
    if message["type"] == "control_request":
        request = message["request"]
        if request["subtype"] == "initialize":
            callbacks = request.get("hooks") or {}
        emit({"type": "control_response", "response": {"subtype": "success",
              "request_id": message["request_id"], "response": {}}})
        continue
    if message["type"] == "control_response":
        if pending:
            emit(pending)
            pending = None
        continue
    if message["type"] != "user":
        continue
    task = json.loads(message["message"]["content"])
    with (root / "calls.ndjson").open("a") as f:
        f.write(json.dumps({"pid": os.getpid(), "session": session, "tag": task.get("tag"),
                            "resume": arg("--resume")}) + "\n")
    if task.get("delay"):
        time.sleep(task["delay"])
    emit({"type": "system", "subtype": "init", "session_id": session, "model": arg("--model")})
    model = task.get("model", "claude-opus-5")
    assistant = {"id": str(uuid.uuid4()), "role": "assistant", "model": model,
                 "content": [{"type": "text", "text": task.get("tag", "fixture")}]}
    transcript.parent.mkdir(parents=True, exist_ok=True)
    with transcript.open("a") as f:
        f.write(json.dumps({"type": "assistant", "uuid": str(uuid.uuid4()), "sessionId": session,
                            "message": assistant, "effort": "max", "version": "2.1.261"}) + "\n")
    emit({"type": "assistant", "session_id": session, "message": assistant})
    if task.get("quota"):
        emit({"type": "rate_limit_event", "session_id": session, "uuid": str(uuid.uuid4()),
              "rate_limit_info": {"status": "allowed", "unifiedWindows": {"seven_day": {"utilization": task["quota"],
                                                                  "resetsAt": time.time() + 3600}}}})
    pending = {"type": "result", "subtype": "success", "session_id": session, "is_error": False,
               "num_turns": 1, "duration_ms": 1, "duration_api_ms": 1, "usage": {},
               "total_cost_usd": 0, "result": task.get("tag", "fixture"), "permission_denials": []}
    hook_ids = [c for matcher in callbacks.get("Stop", []) for c in matcher.get("hookCallbackIds", [])]
    if hook_ids:
        emit({"type": "control_request", "request_id": str(uuid.uuid4()),
              "request": {"subtype": "hook_callback", "callback_id": hook_ids[0],
                          "input": {"hook_event_name": "Stop", "session_id": session, "cwd": os.getcwd()},
                          "tool_use_id": None}})
    else:
        emit(pending)
        pending = None
