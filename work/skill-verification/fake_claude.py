#!/usr/bin/env python3
"""Independent simulated CLI for supervisor lifecycle tests; never calls a model."""
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

if "--version" in sys.argv:
    print("2.1.261 (simulated Claude Code)")
    raise SystemExit(0)

def arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None

task = json.loads(sys.stdin.read())
session = arg("--session-id") or arg("--resume")
config = Path(os.environ["CLAUDE_CONFIG_DIR"])
config.mkdir(parents=True, exist_ok=True)
ledger = {"session_id": session, "resume": arg("--resume"), "model": arg("--model"),
          "effort": arg("--effort"), "env_effort": os.environ.get("CLAUDE_CODE_EFFORT_LEVEL"),
          "tag": task.get("tag"), "pid": os.getpid(), "cwd": os.getcwd()}
with (config / "calls.ndjson").open("a") as f:
    f.write(json.dumps(ledger) + "\n")
path = config / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", os.getcwd()) / f"{session}.jsonl"
if arg("--resume") and not path.is_file():
    print(json.dumps({"type": "result", "session_id": session, "subtype": "error_during_execution",
                      "is_error": True, "errors": ["simulated missing session"]}), flush=True)
    raise SystemExit(1)
print(json.dumps({"type": "system", "subtype": "init", "session_id": session,
                  "model": arg("--model")}), flush=True)
time.sleep(task.get("delay", 0.1))
if task.get("scenario") == "synthetic_error":
    model, is_error = "<synthetic>", True
else:
    model, is_error = task.get("actual_model", "claude-opus-5"), False
message = {"role": "assistant", "model": model,
           "content": [{"type": "text", "text": task.get("tag", "fixture result")}]}
record = {"type": "assistant", "uuid": str(uuid.uuid4()), "sessionId": session, "message": message}
if task.get("scenario") != "missing_effort":
    record["effort"] = task.get("actual_effort", "max")
if task.get("scenario") == "nested_effort":
    record.pop("effort", None)
    record["message"]["effort"] = "max"
if task.get("scenario") != "missing_transcript":
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")
print(json.dumps({"type": "assistant", "session_id": session, "message": message}), flush=True)
result = {"type": "result", "session_id": session, "subtype": "success", "is_error": is_error,
          "result": "simulated refusal" if is_error else task.get("tag", "fixture result"),
          "permission_denials": []}
if task.get("scenario") == "missing_result_fields":
    result.pop("session_id")
    result.pop("is_error")
print(json.dumps(result), flush=True)
raise SystemExit(1 if is_error else 0)
