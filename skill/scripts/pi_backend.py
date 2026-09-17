"""Pi coding-agent CLI adapter for the fixed OpenRouter Union Alpha route.

Pi runs once per round in JSON mode.  The orchestrator supplies the prompt on
stdin, stores Pi's JSON event stream as round evidence, and resumes an isolated
native Pi session on revisions.  Extensions, skills, prompt templates, themes,
and project context files are disabled for delegated runs; only the explicit
built-in tool allowlist is enabled.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from claude_events import Monitor
from kimi_backend import error
from tool_catalog import PI_TOOLS as TOOLS, select

PROVIDER = "openrouter"
RAW_MODEL = "stealth/union-alpha"
MODEL = PROVIDER + "/" + RAW_MODEL
EFFORT = "off"
REFERENCE_VERSION = "0.85.1"
REQUIRED_FLAGS = (
    "provider", "model", "mode", "print", "session", "session-id",
    "session-dir", "name", "tools", "thinking", "no-extensions",
    "no-skills", "no-prompt-templates", "no-themes", "no-context-files",
    "no-approve", "offline",
)


def probe(binary, *args, timeout=10):
    try:
        return subprocess.check_output(
            [str(binary), *args], text=True, stderr=subprocess.STDOUT,
            timeout=timeout,
        ).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        error("pi_probe", "Pi " + " ".join(args) + " probe failed (" + type(exc).__name__ + ")")


def launcher_runtime(binary):
    """Resolve the executable that the kernel runs for a script launcher."""
    try:
        with Path(binary).open("rb") as source:
            first_line = source.readline(256).decode("utf-8", "replace")
    except OSError:
        return binary
    if not first_line.startswith("#!"):
        return binary
    try:
        words = shlex.split(first_line[2:].strip())
    except ValueError:
        words = []
    if not words:
        error("pi_incompatible", "Pi launcher has an invalid interpreter line")
    interpreter = words[0]
    if os.path.basename(interpreter) == "env":
        args = words[1:]
        if args[:1] == ["-S"]:
            args = args[1:]
        while args and "=" in args[0] and not args[0].startswith("="):
            args = args[1:]
        if not args or args[0].startswith("-"):
            error("pi_incompatible", "Pi launcher uses an unsupported env interpreter line")
        interpreter = args[0]
    resolved = interpreter if os.path.isabs(interpreter) else shutil.which(interpreter)
    if not resolved or not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        error("pi_incompatible", "Pi launcher interpreter could not be resolved")
    return os.path.realpath(resolved)


def prepare(explicit, requested_tools, allow_rules):
    """Validate the installed CLI, OpenRouter auth, exact model, and tools."""
    chosen = select("pi", requested_tools)
    if sys.platform != "darwin":
        error("pi_platform", "Pi process identity is currently verified on macOS only")
    if allow_rules:
        error("pi_permissions", "Claude allow rules are not Pi rules; use --pi-tool")
    binary = explicit or shutil.which("pi")
    if not binary or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        error("pi_missing", "Pi coding-agent CLI not found; provide --pi-bin")
    binary = str(Path(binary).resolve())
    runtime = launcher_runtime(binary)
    version = probe(binary, "--version")
    if not version:
        error("pi_probe", "Pi returned an empty version; could not record the executing CLI")
    help_text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", probe(binary, "--help"))
    flags = set(re.findall(r"(?<![\w-])--([a-z][a-z0-9-]*)\b", help_text))
    missing = sorted(set(REQUIRED_FLAGS) - flags)
    if missing:
        error("pi_incompatible", "Pi is missing required options: " + ", ".join("--" + f for f in missing))
    try:
        auth_text = probe(binary, "auth", "check", "--provider", PROVIDER,
                          "--json", "--no-refresh")
        auth = json.loads(auth_text.splitlines()[-1])
        if not isinstance(auth, dict):
            raise TypeError("auth status is not an object")
    except (ValueError, TypeError, IndexError, AttributeError):
        error("pi_auth", "Pi returned invalid OpenRouter auth status")
    if auth.get("status") != "ready" or auth.get("provider") != PROVIDER:
        error("pi_auth", "Pi OpenRouter authentication is not ready; configure it before delegation")
    models = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "",
                    probe(binary, "--offline", "--list-models", RAW_MODEL))
    exact = any(parts[:2] == [PROVIDER, RAW_MODEL]
                for parts in (line.split() for line in models.splitlines()) if len(parts) >= 2)
    if not exact:
        error("pi_model_missing", "Pi cannot resolve exact model " + MODEL + "; no fallback is allowed")
    return dict(
        pi_bin=binary,
        pi_runtime=runtime,
        pi_tools=chosen,
        pi_version=version,
        pi_compatibility=dict(
            cli_options="checked",
            reference_version=REFERENCE_VERSION,
            matches_reference=version == REFERENCE_VERSION,
            auth_provider=PROVIDER,
            exact_model=MODEL,
            native_evidence="pending_runtime_verification",
        ),
    )


def digest_prefix(path, size):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        remaining = size
        while remaining:
            chunk = source.read(min(remaining, 1024 * 1024))
            if not chunk:
                return None
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def rows(path, offset=0):
    with Path(path).open("rb") as source:
        source.seek(offset)
        data = source.read()
    if data and not data.endswith(b"\n"):
        raise ValueError("native session has an incomplete record")
    result = []
    for line in data.splitlines():
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("native session contains a non-object record")
        result.append(row)
    return result


def session_file(job):
    root = Path(job["pi_session_dir"]).resolve()
    matches = [path for path in root.glob("*_" + job["session_id"] + ".jsonl")
               if path.is_file() and not path.is_symlink() and path.resolve().parent == root]
    if len(matches) != 1:
        raise ValueError("expected exactly one isolated Pi session file")
    return matches[0].resolve()


def baseline(job):
    if not job.get("session_id"):
        error("pi_session_missing", "Cannot resume without the saved Pi session id")
    try:
        path = session_file(job)
        size = path.stat().st_size
    except (OSError, ValueError) as exc:
        error("pi_session_missing", "Pi native session is missing or ambiguous: " + str(exc))
    return [dict(path=str(path), size=size, sha256=digest_prefix(path, size))], "ok"


def require_profile(job):
    if job.get("model") != MODEL or job.get("effort") != EFFORT:
        error("pi_profile", "Saved Pi model/effort differs from the fixed verified profile")


def setup(job, record, resume, prompt_file, directory):
    from claude_task import atomic_write_bytes, ensure_dir

    require_profile(job)
    actual = prepare(job["pi_bin"], job["pi_tools"], [])
    job["pi_runtime"] = actual["pi_runtime"]
    job["pi_version"] = actual["pi_version"]
    job["pi_compatibility"] = actual["pi_compatibility"]
    record["pi_version"] = actual["pi_version"]
    ensure_dir(job["pi_session_dir"])

    directory = Path(directory)
    marker = "[codex-delegate:" + record["run_token"] + "]"
    prompt = directory / "pi-prompt.txt"
    atomic_write_bytes(str(prompt), (marker + "\n" + Path(prompt_file).read_text()).encode())
    args = [
        job["pi_bin"], "--offline", "--mode", "json", "--print",
        "--provider", PROVIDER, "--model", RAW_MODEL, "--thinking", EFFORT,
        "--session-dir", job["pi_session_dir"],
        "--name", "codex-delegate:" + record["run_token"],
        "--tools", ",".join(job["pi_tools"]),
        "--no-extensions", "--no-skills", "--no-prompt-templates",
        "--no-themes", "--no-context-files", "--no-approve",
    ]
    args += ["--session", job["session_id"]] if resume else ["--session-id", job["session_id"]]
    return args, dict(os.environ, PWD=job["cwd"]), str(prompt)


class PiMonitor(Monitor):
    def stream(self, row):
        kind = row.get("type")
        if kind == "message_update":
            event = row.get("assistantMessageEvent") or {}
            if event.get("type") in ("text_delta", "thinking_delta"):
                self.progress()
        elif kind == "message_end":
            message = row.get("message") or {}
            if message.get("role") == "assistant":
                ident = message.get("responseId") or str(message.get("timestamp") or len(self.messages))
                usage = message.get("usage") or {}
                self.messages[ident] = dict(input=self.number(usage.get("input")),
                                            output=self.number(usage.get("output")))
                self.progress()
        elif kind == "tool_execution_start":
            self.tools[row.get("toolCallId") or row.get("toolCall", {}).get("id")] = row.get("toolName", "")
            self.progress()
        elif kind == "tool_execution_end":
            ident = row.get("toolCallId") or row.get("toolCall", {}).get("id")
            tool = self.tools.pop(ident, "") or row.get("toolName", "")
            if row.get("isError") is True:
                self.error("tool_result", tool, "Pi tool failed; inspect private evidence")
            else:
                self.progress()
        elif kind == "agent_end" and row.get("willRetry") is True:
            self.error("pi_retry", message="Pi reported a retry; inspect private evidence")

    def snapshot(self):
        result = super().snapshot()
        result.update(output_source="pi_message_end" if self.messages else "unknown",
                      session_id=self.session_id,
                      notification_mode="pi_json_and_native_verification", quota=None)
        return result


def text_content(message):
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ""
    return "".join(part.get("text", "") for part in content
                   if isinstance(part, dict) and part.get("type") == "text"
                   and isinstance(part.get("text"), str))


def verify(job, record, stdout_path, exit_code):
    from claude_task import clip, write_json

    reasons, report, assistants, effective_effort = [], "", [], None
    model_ok = effort_ok = session_ok = False
    tool_failures = 0
    native_path = None
    try:
        stream = rows(stdout_path)
        sessions = [row for row in stream if row.get("type") == "session"]
        stream_cwd = sessions[0].get("cwd") if len(sessions) == 1 else None
        if (len(sessions) != 1 or sessions[0].get("id") != job["session_id"]
                or not isinstance(stream_cwd, str) or not stream_cwd
                or os.path.realpath(stream_cwd) != job["cwd"]):
            raise ValueError("Pi JSON stream session mismatch")
        stdout_users = [row.get("message") or {} for row in stream
                        if row.get("type") == "message_end"
                        and (row.get("message") or {}).get("role") == "user"]
        stdout_assistants = [row.get("message") or {} for row in stream
                             if row.get("type") == "message_end"
                             and (row.get("message") or {}).get("role") == "assistant"]
        ending_indexes = [i for i, row in enumerate(stream) if row.get("type") == "agent_end"]
        endings = [stream[i] for i in ending_indexes]
        settled_indexes = [i for i, row in enumerate(stream) if row.get("type") == "agent_settled"]
        if any(row.get("willRetry") is True for row in endings):
            reasons.append("native_retry_seen")
        if (not endings or endings[-1].get("willRetry") is not False
                or not settled_indexes or settled_indexes[-1] <= ending_indexes[-1]):
            reasons.append("native_completion_missing")
        if any(row.get("type") == "error" for row in stream):
            reasons.append("session_error")
        tool_failures = sum(row.get("type") == "tool_execution_end" and row.get("isError") is True
                            for row in stream)
        observed_tools = sorted({row.get("toolName") for row in stream
                                 if row.get("type") == "tool_execution_start"
                                 and isinstance(row.get("toolName"), str)})
        if any(name not in (job.get("pi_tools") or []) for name in observed_tools):
            reasons.append("tool_profile_mismatch")

        native_path = session_file(job)
        prior = record.get("prior_assistant_uuids") or []
        offset = 0
        if record.get("kind") == "revision":
            if len(prior) != 1 or record.get("baseline_status") != "ok":
                raise ValueError("revision baseline missing")
            saved = prior[0]
            if (saved.get("path") != str(native_path)
                    or digest_prefix(native_path, saved.get("size", -1)) != saved.get("sha256")):
                raise ValueError("native baseline changed")
            offset = saved["size"]
        full = rows(native_path)
        fresh = rows(native_path, offset)
        if not full or full[0].get("type") != "session" or full[0].get("id") != job["session_id"]:
            raise ValueError("native session header mismatch")
        native_cwd = full[0].get("cwd")
        if not isinstance(native_cwd, str) or not native_cwd or os.path.realpath(native_cwd) != job["cwd"]:
            raise ValueError("native session cwd mismatch")

        name = "codex-delegate:" + record["run_token"]
        markers = [row for row in fresh if row.get("type") == "session_info" and row.get("name") == name]
        if len(markers) != 1:
            raise ValueError("round marker missing or ambiguous")
        marker_id = markers[0].get("id")
        marker_index = (next((i for i, row in enumerate(full) if row.get("id") == marker_id), None)
                        if isinstance(marker_id, str) and marker_id else None)
        if marker_index is None:
            raise ValueError("round marker id is missing from the native session")
        active = full[marker_index:]
        messages = [row for row in active if row.get("type") == "message"]
        users = [(i, row) for i, row in enumerate(active)
                 if row.get("type") == "message" and (row.get("message") or {}).get("role") == "user"]
        if len(users) != 1:
            raise ValueError("current Pi turn is missing or contains a foreign user turn")
        user_index, user = users[0]
        marker = "[codex-delegate:" + record["run_token"] + "]"
        if marker not in text_content(user.get("message") or {}):
            raise ValueError("current Pi prompt marker missing")
        assistants = [row.get("message") or {} for row in messages
                      if (row.get("message") or {}).get("role") == "assistant"]
        if not assistants:
            reasons.append("final_report_missing")

        through_user = marker_index + user_index + 1
        model_changes = [row for row in full[:through_user]
                         if row.get("type") == "model_change"]
        effort_changes = [row for row in full[:through_user]
                          if row.get("type") == "thinking_level_change"]
        round_model_changes = [row for row in active if row.get("type") == "model_change"]
        round_effort_changes = [row for row in active if row.get("type") == "thinking_level_change"]
        if (not model_changes or model_changes[-1].get("provider") != PROVIDER
                or model_changes[-1].get("modelId") != RAW_MODEL
                or any(row.get("provider") != PROVIDER or row.get("modelId") != RAW_MODEL
                       for row in round_model_changes)
                or any(message.get("provider") != PROVIDER or message.get("model") != RAW_MODEL
                       for message in assistants)):
            reasons.append("model_unverified")
        else:
            model_ok = bool(assistants)
        effective_effort = ((round_effort_changes[-1] if round_effort_changes else effort_changes[-1])
                            .get("thinkingLevel") if (round_effort_changes or effort_changes) else None)
        thinking = [part for message in assistants for part in (message.get("content") or [])
                    if isinstance(part, dict) and part.get("type") == "thinking"
                    and any(part.get(key) for key in ("thinking", "text", "content"))]
        if (effective_effort != EFFORT or thinking
                or any(row.get("thinkingLevel") != EFFORT for row in round_effort_changes)):
            reasons.append("effort_unverified")
        else:
            effort_ok = bool(assistants)

        if assistants:
            final = assistants[-1]
            report = text_content(final)
            if final.get("stopReason") != "stop":
                reasons.append("native_completion_missing")
            if not report:
                reasons.append("final_report_missing")
            if not stdout_assistants or text_content(stdout_assistants[-1]) != report:
                reasons.append("final_report_mismatch")
        if len(stdout_users) != 1 or marker not in text_content(stdout_users[0]):
            reasons.append("stream_prompt_mismatch")
        if any(message.get("provider") != PROVIDER or message.get("model") != RAW_MODEL
               for message in stdout_assistants):
            if "model_unverified" not in reasons:
                reasons.append("model_unverified")
            model_ok = False
        session_ok = True
        write_json(str(Path(stdout_path).parent / "pi-native.json"), dict(
            session_id=job["session_id"], cwd=job["cwd"], marker=name,
            assistants=[{key: message.get(key) for key in
                         ("provider", "model", "stopReason", "timestamp", "responseId")}
                        for message in assistants],
            effective_effort=effective_effort, observed_tools=observed_tools, report=report,
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
        native_session_id=job.get("session_id"),
        session_ok=session_ok,
        cli_version=job.get("pi_version"),
        model_verified=model_ok and "model_unverified" not in reasons,
        effort_verified=effort_ok and "effort_unverified" not in reasons,
        assistant_models=sorted({str(message.get("provider")) + "/" + str(message.get("model"))
                                 for message in assistants}),
        efforts=[effective_effort] if effective_effort else [],
        observed_tools=observed_tools if "observed_tools" in locals() else [],
        transcript_lookup=str(Path(stdout_path).parent / "pi-native.json") if native_path else "missing",
        new_assistant_entries=len(assistants),
    )
