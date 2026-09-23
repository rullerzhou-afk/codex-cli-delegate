"""Codex CLI adapter for the fixed gpt-6-sol / xhigh route.

Each round runs ``codex exec --json`` once; revisions run ``codex exec resume``
on the saved native thread, so corrections keep the same context. The prompt
arrives on stdin behind a per-round marker, the JSON event stream is the round's
evidence, and completion is proven from Codex's own rollout: the marked turn's
model, effort, cwd, sandbox, approval policy and final message.

Delegated runs load neither the user's config.toml nor execpolicy rules, so no
MCP servers, notify programs, hook trust or daily approvals carry over; Codex
auth and AGENTS.md still apply. Only the user's own ``respect_system_proxy``
feature is mirrored, so the worker reaches the network the same way.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from claude_events import Monitor
from kimi_backend import error
from pi_backend import digest_prefix, rows
from tool_catalog import select

MODEL = "gpt-6-sol"
EFFORT = "xhigh"
APPROVAL = "never"
# The desktop app ships the runtime its own threads use, so it offers the same
# models as the app; a standalone CLI can lag (0.154.0 cannot run gpt-6-sol).
APP_BINARY = "/Applications/ChatGPT.app/Contents/Resources/codex"
EXEC_FLAGS = ("json", "ignore-user-config", "ignore-rules", "skip-git-repo-check",
              "output-last-message", "model", "config", "enable", "cd")
RESUME_FLAGS = EXEC_FLAGS[:-1]
TOOL_ITEMS = ("command_execution", "file_change", "mcp_tool_call", "web_search")
THREAD_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# Codex flushes the rollout as the turn ends; allow a short settle after exit.
NATIVE_SETTLE_SECONDS = 5.0


def probe(binary, *args, merge_stderr=True):
    try:
        return subprocess.check_output(
            [str(binary), *args], text=True, stdin=subprocess.DEVNULL, timeout=20,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        error("codex_probe", "Codex " + " ".join(args) + " probe failed (" + type(exc).__name__ + ")")


def help_flags(binary, *command):
    text = ANSI_RE.sub("", probe(binary, *command, "--help"))
    return set(re.findall(r"(?<![\w-])--([a-z][a-z0-9-]*)\b", text))


def supports_profile(binary):
    """True when this CLI's own model catalog offers MODEL at EFFORT."""
    try:
        catalog = json.loads(probe(binary, "debug", "models", merge_stderr=False))
    except ValueError:
        error("codex_probe", "Codex returned an invalid model catalog")
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if not isinstance(models, list):
        error("codex_probe", "Codex returned an invalid model catalog")
    for entry in models:
        if isinstance(entry, dict) and entry.get("slug") == MODEL:
            return any(isinstance(level, dict) and level.get("effort") == EFFORT
                       for level in entry.get("supported_reasoning_levels") or [])
    return False


def codex_home():
    return str(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve())


def system_proxy(home):
    """Mirror only the user's `[features] respect_system_proxy = true` choice."""
    try:
        config = (Path(home) / "config.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    section = re.search(r"^\[features\]\s*\n(.*?)(?=^\[|\Z)", config, re.M | re.S)
    return bool(section and re.search(r"^respect_system_proxy\s*=\s*true\s*(?:#.*)?$", section[1], re.M))


def sandbox_mode(tools):
    return "workspace-write" if "write" in tools else "read-only"


def prepare(explicit, requested_tools, allow_rules):
    """Validate the CLI, its catalog for the exact profile, and the sandbox selection."""
    chosen = select("codex", requested_tools)
    if "network" in chosen and "write" not in chosen:
        error("codex_tools", "Codex network access requires write (the workspace-write sandbox)")
    chosen = sorted(set(chosen) | {"read"})
    if sys.platform != "darwin":
        error("codex_platform", "Codex process identity is currently verified on macOS only")
    if allow_rules:
        error("codex_permissions", "Claude allow rules are not Codex rules; use --codex-tool")
    binary = explicit or (APP_BINARY if os.access(APP_BINARY, os.X_OK) else shutil.which("codex"))
    if not binary or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        error("codex_missing", "Codex CLI not found; provide --codex-bin")
    binary = os.path.realpath(binary)
    version = probe(binary, "--version")
    if not version:
        error("codex_probe", "Codex returned an empty version; could not record the executing CLI")
    for command, required in ((("exec",), EXEC_FLAGS), (("exec", "resume"), RESUME_FLAGS)):
        missing = sorted(set(required) - help_flags(binary, *command))
        if missing:
            error("codex_incompatible", "codex " + " ".join(command) + " is missing required options: "
                  + ", ".join("--" + flag for flag in missing))
    if not supports_profile(binary):
        error("codex_model_missing", version + " does not offer " + MODEL + " at " + EFFORT
              + " effort; update Codex or pass --codex-bin. No fallback is allowed")
    home = codex_home()
    return dict(
        codex_bin=binary,
        codex_home=home,
        codex_tools=chosen,
        codex_version=version,
        codex_system_proxy=system_proxy(home),
        codex_compatibility=dict(cli_options="checked", model_catalog="checked",
                                 native_evidence="pending_runtime_verification"),
    )


def rollout_file(home, thread):
    if not isinstance(thread, str) or not THREAD_RE.match(thread):
        raise ValueError("Codex thread id is missing or malformed")
    root = (Path(home) / "sessions").resolve()
    # A resumed thread keeps appending to the rollout in its original day folder.
    matches = [path for path in root.glob("*/*/*/rollout-*-" + thread + ".jsonl")
               if path.is_file() and not path.is_symlink() and root in path.resolve().parents]
    if len(matches) != 1:
        raise ValueError("expected exactly one native rollout for the Codex thread")
    return matches[0].resolve()


def baseline(job):
    if not job.get("session_id"):
        error("codex_session_missing", "Cannot resume without the saved Codex thread id")
    try:
        path = rollout_file(job["codex_home"], job["session_id"])
        size = path.stat().st_size
    except (OSError, ValueError) as exc:
        error("codex_session_missing", "Codex native rollout is missing or ambiguous: " + str(exc))
    return [dict(path=str(path), size=size, sha256=digest_prefix(path, size))], "ok"


def require_profile(job):
    if job.get("model") != MODEL or job.get("effort") != EFFORT:
        error("codex_profile", "Saved Codex model/effort differs from the fixed verified profile")


def setup(job, record, resume, prompt_file, directory):
    from claude_task import atomic_write_bytes

    require_profile(job)
    actual = prepare(job["codex_bin"], job["codex_tools"], [])
    if actual["codex_home"] != job["codex_home"]:
        error("codex_home_changed", "CODEX_HOME differs from the one holding this job's thread")
    for key in ("codex_version", "codex_compatibility", "codex_system_proxy"):
        job[key] = actual[key]
    record["codex_version"] = actual["codex_version"]

    directory = Path(directory)
    prompt = directory / "codex-prompt.txt"
    marker = "[codex-delegate:" + record["run_token"] + "]\n"
    atomic_write_bytes(str(prompt), marker.encode() + Path(prompt_file).read_bytes())
    options = [
        "--json", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
        "-m", MODEL, "-c", 'model_reasoning_effort="%s"' % EFFORT,
        "-c", 'approval_policy="%s"' % APPROVAL,
        "-c", 'sandbox_mode="%s"' % sandbox_mode(job["codex_tools"]),
        "-o", str(directory / "codex-last-message.txt"),
    ]
    if "network" in job["codex_tools"]:
        options += ["-c", "sandbox_workspace_write.network_access=true"]
    if job["codex_system_proxy"]:
        options += ["--enable", "respect_system_proxy", "-c", "suppress_unstable_features_warning=true"]
    if resume:
        argv = [job["codex_bin"], "exec", "resume", *options, job["session_id"], "-"]
    else:
        argv = [job["codex_bin"], "exec", *options, "-C", job["cwd"], "-"]
    env = dict(os.environ, PWD=job["cwd"])
    # A coordinating Codex task's identity and state DB must not reach the worker.
    for name in ("CODEX_THREAD_ID", "CODEX_SQLITE_HOME"):
        env.pop(name, None)
    return argv, env, str(prompt)


def item_failed(item):
    return (item.get("status") in ("failed", "declined")
            or (item.get("type") == "command_execution" and item.get("exit_code") not in (None, 0)))


class CodexMonitor(Monitor):
    """Progress hints from the JSON stream; `verify` alone decides completion."""

    def stream(self, row):
        kind = row.get("type")
        item = row.get("item") if isinstance(row.get("item"), dict) else {}
        if kind == "thread.started":
            if self.session_id is None and isinstance(row.get("thread_id"), str):
                self.session_id = row["thread_id"]
        elif kind in ("turn.started", "item.updated"):
            self.progress()
        elif kind == "item.started":
            if item.get("type") in TOOL_ITEMS:
                self.tools[item.get("id")] = item.get("type")
            self.progress()
        elif kind == "item.completed":
            tool = self.tools.pop(item.get("id"), "") or (item.get("type") if item.get("type") in TOOL_ITEMS else "")
            if tool and item_failed(item):
                self.error("tool_result", tool, "Codex tool failed; inspect private evidence")
            elif item.get("type") != "error":  # warning items are evidence, not progress
                self.incident_active = False
                self.progress()
        elif kind == "turn.completed":
            usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
            self.messages["turn"] = dict(input=self.number(usage.get("input_tokens")),
                                         output=self.number(usage.get("output_tokens")))
            self.progress()
        elif kind == "turn.failed":
            self.error("codex_turn_failed", message="Codex turn failed; inspect private evidence", fatal=True)
        elif kind == "error":
            self.error("codex_error", message="Codex reported an error; inspect private evidence")

    def snapshot(self):
        result = super().snapshot()
        result.update(output_source="codex_turn_completed" if self.messages else "unknown",
                      session_id=self.session_id,
                      notification_mode="codex_json_and_native_verification", quota=None)
        return result


def message_text(payload):
    parts = payload.get("content")
    if not isinstance(parts, list):
        return ""
    return "".join(part["text"] for part in parts if isinstance(part, dict)
                   and part.get("type") in ("input_text", "output_text", "text")
                   and isinstance(part.get("text"), str))


def native_turns(records, thread):
    """Group rollout records into turns; records tagged with another thread are ignored."""
    turns, order, current = {}, [], None

    def turn(turn_id):
        if turn_id not in turns:
            turns[turn_id] = dict(context=None, users=[], complete=None, aborted=False,
                                  failed=False, rate_limits=None)
            order.append(turn_id)
        return turns[turn_id]

    for index, record in enumerate(records):
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("thread_id") not in (None, thread):
            continue
        kind, sub, turn_id = record.get("type"), payload.get("type"), payload.get("turn_id")
        if kind == "event_msg" and sub == "task_started" and isinstance(turn_id, str):
            current = turn_id
            turn(current)
        elif kind == "turn_context" and isinstance(turn_id, str):
            turn(turn_id)["context"] = payload
        elif kind == "event_msg" and sub == "task_complete":
            turn(turn_id if isinstance(turn_id, str) else current)["complete"] = payload
        elif kind == "event_msg" and sub == "turn_aborted":
            turn(turn_id if isinstance(turn_id, str) else current)["aborted"] = True
        elif current is None:
            continue
        elif kind == "response_item" and sub == "message" and payload.get("role") == "user":
            turns[current]["users"].append((index, message_text(payload)))
        elif kind == "event_msg" and sub in ("error", "task_failed"):
            turns[current]["failed"] = True
        elif kind == "event_msg" and sub == "token_count" and isinstance(payload.get("rate_limits"), dict):
            turns[current]["rate_limits"] = (record.get("timestamp"), payload["rate_limits"])
    return turns, order


def marked_turn_complete(records, thread, marker):
    turns, _ = native_turns(records, thread)
    return any(turn["complete"] is not None and any(marker in text for _, text in turn["users"])
               for turn in turns.values())


def revision_start(record, path, records):
    """Index of the first rollout record written after the revision baseline."""
    if record.get("kind") != "revision":
        return 0
    prior = record.get("prior_assistant_uuids") or []
    if len(prior) != 1 or record.get("baseline_status") != "ok":
        raise ValueError("revision baseline missing")
    saved = prior[0]
    if saved.get("path") != str(path) or digest_prefix(path, saved.get("size", -1)) != saved.get("sha256"):
        raise ValueError("native baseline changed")
    return len(records) - len(rows(path, saved["size"]))


def read_rollout(job, record, thread, marker, expect_complete):
    deadline = time.monotonic() + (NATIVE_SETTLE_SECONDS if expect_complete else 0)
    while True:
        try:
            path = rollout_file(job["codex_home"], thread)
            records = rows(path)
            if (not expect_complete or marked_turn_complete(records, thread, marker)
                    or time.monotonic() >= deadline):
                return path, records, revision_start(record, path, records)
        except (OSError, ValueError):
            if time.monotonic() >= deadline:
                raise
        time.sleep(0.25)


def rate_limit_summary(entry):
    """Reported, not enforced: the round's latest native rate-limit snapshot."""
    if not entry:
        return None
    observed_at, limits = entry
    windows = {}
    for name in ("primary", "secondary"):
        window = limits.get(name)
        if isinstance(window, dict):
            windows[name] = {key: window.get(key) for key in ("used_percent", "window_minutes", "resets_at")}
    used = [w["used_percent"] for w in windows.values()
            if isinstance(w.get("used_percent"), (int, float)) and not isinstance(w.get("used_percent"), bool)]
    return dict(limit_id=limits.get("limit_id"), plan_type=limits.get("plan_type"), windows=windows,
                reached=limits.get("rate_limit_reached_type"), observed_at=observed_at,
                max_used_percent=max(used) if used else None,
                at_or_above_90=bool(used) and max(used) >= 90)


def verify(job, record, stdout_path, exit_code):
    from claude_task import clip, write_json

    reasons, report, thread, observed, usage, limits = [], "", None, [], {}, None
    model = effort = None
    model_ok = effort_ok = session_ok = False
    tool_failures = agent_messages = 0
    native_path = None
    marker = "[codex-delegate:" + record["run_token"] + "]"
    tools = job.get("codex_tools") or ["read"]
    try:
        stream = rows(stdout_path)
        starts = [row for row in stream if row.get("type") == "thread.started"]
        if len(starts) != 1 or not THREAD_RE.match(str(starts[0].get("thread_id"))):
            raise ValueError("Codex JSON stream thread mismatch")
        thread = starts[0]["thread_id"]
        if job.get("session_id") not in (None, thread):
            raise ValueError("Codex resumed a different thread")
        kinds = [row.get("type") for row in stream]
        if kinds.count("turn.started") != 1:
            raise ValueError("Codex JSON stream must contain exactly one turn")
        if "turn.failed" in kinds:
            reasons.append("turn_failed")
        if "error" in kinds:
            reasons.append("session_error")
        if kinds.count("turn.completed") != 1 or kinds[-1] != "turn.completed":
            reasons.append("native_completion_missing")
        items = [row["item"] for row in stream
                 if row.get("type") == "item.completed" and isinstance(row.get("item"), dict)]
        messages = [item["text"] for item in items
                    if item.get("type") == "agent_message" and isinstance(item.get("text"), str)]
        agent_messages = len(messages)
        observed = sorted({item["type"] for item in items if item.get("type") in TOOL_ITEMS})
        tool_failures = sum(1 for item in items if item.get("type") in TOOL_ITEMS and item_failed(item))
        if "write" not in tools and any(item.get("type") == "file_change" and item.get("status") == "completed"
                                        for item in items):
            reasons.append("tool_profile_mismatch")
        streamed = messages[-1] if messages else ""
        finished = [row.get("usage") for row in stream if row.get("type") == "turn.completed"]
        raw = finished[-1] if finished and isinstance(finished[-1], dict) else {}
        usage = {name: raw[key] for name, key in (("input_tokens", "input_tokens"),
                                                  ("cache_read_input_tokens", "cached_input_tokens"),
                                                  ("output_tokens", "output_tokens"),
                                                  ("reasoning_output_tokens", "reasoning_output_tokens"))
                 if type(raw.get(key)) is int}
        try:
            written = (Path(stdout_path).parent / "codex-last-message.txt").read_text(encoding="utf-8")
        except FileNotFoundError:
            written = None

        native_path, records, fresh_from = read_rollout(job, record, thread, marker,
                                                        "turn.completed" in kinds)
        meta = records[0].get("payload") if records and records[0].get("type") == "session_meta" else None
        if (not isinstance(meta, dict) or meta.get("id") != thread
                or meta.get("session_id") not in (None, thread)):
            raise ValueError("native session header mismatch")
        native_cwd = meta.get("cwd")
        if not isinstance(native_cwd, str) or os.path.realpath(native_cwd) != job["cwd"]:
            raise ValueError("native session cwd mismatch")
        if meta.get("originator") != "codex_exec":
            raise ValueError("native thread was not created by codex exec")
        turns, order = native_turns(records, thread)
        marked = [turn_id for turn_id in order
                  if any(index >= fresh_from and marker in text for index, text in turns[turn_id]["users"])]
        occurrences = sum(marker in text for turn in turns.values() for _, text in turn["users"])
        if len(marked) != 1 or occurrences != 1:
            raise ValueError("current Codex turn marker missing or ambiguous")
        if order[-1] != marked[0]:
            raise ValueError("a later foreign turn follows the delegated round")
        turn = turns[marked[0]]
        context = turn["context"] or {}
        model, effort = context.get("model"), context.get("effort")
        context_cwd = context.get("cwd")
        if not isinstance(context_cwd, str) or os.path.realpath(context_cwd) != job["cwd"]:
            raise ValueError("native turn cwd mismatch")
        if model != MODEL:
            reasons.append("model_unverified")
        if effort != EFFORT:
            reasons.append("effort_unverified")
        policy = context.get("sandbox_policy") if isinstance(context.get("sandbox_policy"), dict) else {}
        network = policy.get("network_access")
        if (policy.get("type") != sandbox_mode(tools)
                or ("network" in tools and network is not True)
                or ("network" not in tools and network not in (None, False))):
            reasons.append("sandbox_unverified")
        if context.get("approval_policy") != APPROVAL:
            reasons.append("approval_unverified")
        if turn["aborted"]:
            reasons.append("turn_aborted")
        if turn["failed"]:
            reasons.append("native_error")
        complete = turn["complete"]
        if not isinstance(complete, dict):
            reasons.append("native_completion_missing")
        else:
            final = complete.get("last_agent_message")
            report = final if isinstance(final, str) else ""
            if not report:
                reasons.append("final_report_missing")
            if written is None:
                reasons.append("final_report_file_missing")
            if report != streamed or (written is not None and written.rstrip("\n") != report.rstrip("\n")):
                reasons.append("final_report_mismatch")
        limits = rate_limit_summary(turn["rate_limits"])
        model_ok = model == MODEL and isinstance(complete, dict)
        effort_ok = effort == EFFORT and isinstance(complete, dict)
        session_ok = True
        write_json(str(Path(stdout_path).parent / "codex-native.json"), dict(
            thread_id=thread, rollout=str(native_path), turn_id=marked[0], marker=marker,
            cwd=job["cwd"], model=model, effort=effort, sandbox=policy.get("type"),
            network_access=network, approval=context.get("approval_policy"),
            observed_tools=observed, rate_limits=limits, report=report,
        ))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        reasons.append("native_evidence_invalid:" + str(exc)[:160])
    if exit_code != 0:
        reasons.insert(0, "exit_nonzero")
    reasons = list(dict.fromkeys(reasons))
    return dict(
        ok=not reasons,
        needs_attention=exit_code == 0 and bool(reasons),
        reasons=reasons,
        report=clip(report),
        tool_failures=tool_failures,
        exit_code=exit_code,
        native_session_id=thread,
        session_ok=session_ok,
        cli_version=job.get("codex_version"),
        model_verified=model_ok and "model_unverified" not in reasons,
        effort_verified=effort_ok and "effort_unverified" not in reasons,
        assistant_models=[model] if model else [],
        efforts=[effort] if effort else [],
        observed_tools=observed,
        usage=usage,
        codex_rate_limits=limits,
        transcript_lookup=str(Path(stdout_path).parent / "codex-native.json") if native_path else "missing",
        new_assistant_entries=agent_messages,
    )
