#!/usr/bin/env python3
"""Delegate implementation work from a Codex task to a local Claude Code CLI.

Codex plans and reviews. CLI transport starts one detached worker per round;
SDK transport keeps one worker and native client connected across reviewed
rounds. Both use the private ``_worker`` entrypoint and shared job state.

Invariants:

* The launch transaction (reserve round -> spawn worker -> record identity) runs
  under one lock hold; the worker blocks on that same lock before doing
  anything, so no observer ever sees a half-registered launch.  A worker never
  holds the lock across the Claude run itself.
* Each round carries a single-use ``launch_claim``; a second private worker for
  the same round cannot invoke Claude again.  Every worker write is guarded by
  current_round + run token + claim.
* Nothing passes on Claude's say-so. A round reaches ``awaiting_review`` only
  with exit 0 (CLI) or an SDK result boundary, exactly one result event whose ``is_error is False`` and
  ``subtype == "success"`` and whose ``session_id`` equals the saved session,
  assistant events reporting ``claude-opus-5``, and a local transcript whose new
  assistant entries all record ``effort=max``.  Acceptance stays a Codex act.
* Signals go only to a PID whose recorded start time and unique run token still
  match.  ``ps`` failure is never read as proof a process is gone.

CLI: Python 3.9+, standard library. SDK/MCP: Python 3.12+, pinned dependencies. macOS first.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import delegate_completion
import delegate_job_store
import delegate_process
import delegate_recovery
import delegate_transport
from delegate_core import CliError
from delegate_recovery import live_blockers, process_view, reconcile
from delegate_job_store import (JOB_STATE_LEGACY_NO_NAMESPACE_VERSION, JOB_STATE_MIGRATIONS,
                                JOB_STATE_SCHEMA_NAMESPACE, JOB_STATE_SCHEMA_VERSION,
                                JOB_STATE_SUPPORTED_VERSIONS, Context, current_round,
                                default_state_dir, migrate_job_state, prompt_path, round_dir,
                                validate_job_namespace, validate_job_schema)
from delegate_ownership import find_conflict, keys_conflict, reservation_key, validate_owner
from delegate_process import (STATE_GONE, capture_identity, identity_state, pid_liveness,
                              ps_probe, terminate_recorded)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# Job-state schema/migration constants live in delegate_job_store (with the
# reader that enforces them) and are imported above. See docs/CONTRACTS.md.
# Legacy alias: callers and fixtures that referenced the old constant name.
SCHEMA_VERSION = JOB_STATE_SCHEMA_VERSION

MODEL = "claude-opus-5"
EFFORT = "max"

BASE_ALLOWED_TOOLS = ("Read", "Glob", "Grep")
ENABLED_TOOLS = "Read,Write,Edit,NotebookEdit,Bash,Glob,Grep"
from tool_catalog import (CLAUDE_OPTIONAL, CODEX_TOOLS, KIMI_TOOLS, OPENCODE_TOOLS,
                          OPENCODE_ALIASES, PI_TOOLS)
ALLOW_RULE_TOOLS = frozenset(("Read", "Write", "Edit", "Glob", "Grep", "Bash") + CLAUDE_OPTIONAL)

# Only these three variables are injected, and only into the child environment.
CHILD_ENV_OVERRIDES = {
    "CLAUDE_CODE_EFFORT_LEVEL": "max",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}

DEFAULT_TIMEOUT = None  # No wall-clock kill unless a duration is explicitly chosen.
MIN_TIMEOUT = 1  # tiny values are only useful for tests; otherwise user error
MAX_TIMEOUT = 86400
DEFAULT_MAX_REVISIONS = None  # None is unlimited; 0 still means no corrections.

WAIT_CAP_SECONDS = 55
WAIT_POLL_SECONDS = 0.4

LOCK_TIMEOUT_SECONDS = 30.0
# A freshly spawned worker needs a moment to record its own identity; until the
# grace expires, reconciliation must not call a launching round dead.
LAUNCH_GRACE_SECONDS = 60.0
IDENTITY_SETTLE_SECONDS = 2.0

PROMPT_MAX_BYTES = 1 << 20
NOTES_MAX_BYTES = 1 << 20
REPORT_MAX_CHARS = 4000
STDOUT_LINE_MAX_BYTES = 4 << 20

DIR_MODE = 0o700
FILE_MODE = 0o600

PHASE_STARTING = "starting"
PHASE_RUNNING = "running"
PHASE_AWAITING_REVIEW = "awaiting_review"
PHASE_ACCEPTED = "accepted"
PHASE_FAILED = "failed"
PHASE_STOPPED = "stopped"
PHASE_INTERRUPTED = "interrupted"
PHASE_NEEDS_ATTENTION = "needs_attention"

# Phases that still hold the checkout reservation.  Only an explicit Codex
# decision (accept) or an explicit teardown (stop) releases a checkout.
RESERVING_PHASES = frozenset(
    (
        PHASE_STARTING,
        PHASE_RUNNING,
        PHASE_AWAITING_REVIEW,
        PHASE_FAILED,
        PHASE_INTERRUPTED,
        PHASE_NEEDS_ATTENTION,
    )
)
BUSY_PHASES = frozenset((PHASE_STARTING, PHASE_RUNNING))
# A worker must never downgrade a phase an operator decision already wrote.
TERMINAL_DECISION_PHASES = frozenset((PHASE_ACCEPTED, PHASE_STOPPED))
# Phases whose revision needs the explicit --recover acknowledgement, because
# Codex must supply a fresh prompt written after inspecting the evidence.
RECOVER_PHASES = frozenset((PHASE_FAILED, PHASE_INTERRUPTED, PHASE_NEEDS_ATTENTION, PHASE_STOPPED))

UUID_RE = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
OWNER_RE = re.compile(r"\A[A-Za-z0-9._:@+\-]{1,200}\Z")
ALLOW_RULE_RE = re.compile(r"\A([A-Za-z][A-Za-z0-9_]{0,31})(?:\((.+)\))?\Z", re.S)

# Claude Code marks a locally generated stand-in turn (a request that never
# reached the model) with a synthetic model name.  That metadata is the only
# authoritative signal; report text is never pattern-matched for "errors".
SYNTHETIC_MODEL_NAMES = frozenset(("<synthetic>", "synthetic", "<none>"))

# The only transcript effort location verified on this machine (Claude Code
# v2.1.261) is a top-level "effort" string on the assistant entry.  Nothing
# speculative is accepted: an effort buried elsewhere does not count as proof.
EFFORT_FIELD = "effort"

SECRET_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}"
    r"|ey[A-Za-z0-9_\-]{18,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})"
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def now():
    return time.time()


def iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else now()))


def emit(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def redact(text):
    return SECRET_RE.sub("[redacted]", text)


def clip(text, limit=REPORT_MAX_CHARS):
    if not text:
        return ""
    text = redact(str(text))
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def valid_uuid(value):
    return isinstance(value, str) and bool(UUID_RE.match(value))


def require_uuid(value, what):
    if not valid_uuid(value):
        raise CliError("bad_id", "%s is not a lowercase UUID" % what)
    return value


def ensure_dir(path):
    os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    try:
        os.chmod(path, DIR_MODE)
    except OSError:
        pass
    return path


def atomic_write_bytes(path, data):
    parent = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".tmp-")
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path, obj):
    atomic_write_bytes(path, (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))


def read_json(path):
    with open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def copy_private(src, dst, max_bytes):
    with open(src, "rb") as handle:
        data = handle.read(max_bytes + 1)
    atomic_write_bytes(dst, data)


# Job-state schema/migration and persisted job access live in
# delegate_job_store and are re-exported here for the existing public surface.


# --------------------------------------------------------------------------- #
# Locking
# --------------------------------------------------------------------------- #


class StateLock:
    """Exclusive advisory lock over the whole state root.

    Held only for short read-modify-write bursts: reservations, phase changes,
    process-identity records.  Never around a Claude run.
    """

    def __init__(self, state_dir, timeout=LOCK_TIMEOUT_SECONDS):
        self.path = os.path.join(state_dir, "lock")
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, FILE_MODE)
        deadline = now() + self.timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fd = fd
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    os.close(fd)
                    raise
                if now() >= deadline:
                    os.close(fd)
                    raise CliError("lock_timeout", "state lock busy for %ss" % self.timeout)
                time.sleep(0.05)

    def __exit__(self, *exc_info):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None
        return False


# --------------------------------------------------------------------------- #
# Process identity
# --------------------------------------------------------------------------- #
# Process identity, liveness, and termination live in delegate_process and are
# re-exported here so existing callers and patch points keep working.


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def validate_cwd(path):
    if not path or not os.path.isabs(path):
        raise CliError("bad_cwd", "--cwd must be an absolute path")
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise CliError("bad_cwd", "--cwd is not an existing directory")
    return real


def validate_input_file(path, what, max_bytes):
    if not path:
        raise CliError("bad_file", "%s is required" % what)
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        raise CliError("bad_file", "%s does not exist: %s" % (what, path))
    size = os.path.getsize(real)
    if size == 0:
        raise CliError("bad_file", "%s is empty" % what)
    if size > max_bytes:
        raise CliError("bad_file", "%s exceeds %d bytes" % (what, max_bytes))
    return real


def validate_timeout(value):
    if value is None or value == "unlimited":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise CliError("bad_timeout", "--timeout must be unlimited or an integer number of seconds")
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise CliError("bad_timeout", "--timeout must be an integer")
    if not MIN_TIMEOUT <= value <= MAX_TIMEOUT:
        raise CliError("bad_timeout", "--timeout must be %d..%d seconds" % (MIN_TIMEOUT, MAX_TIMEOUT))
    return value


def validate_max_revisions(value):
    if value is None or str(value).lower() == "unlimited":
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise CliError("bad_revisions", "--max-revisions must be unlimited or a nonnegative integer")
    if value < 0:
        raise CliError("bad_revisions", "--max-revisions must be unlimited or a nonnegative integer")
    return value


def read_access(cwd, directories, files):
    """Validate declared inputs before spending a model call; resolve symlinks."""
    roots = []
    for value in directories or []:
        path = validate_cwd(value)
        if path != cwd and path not in roots:
            roots.append(path)
    required = []
    for value in files or []:
        path = os.path.realpath(os.path.abspath(os.path.expanduser(value)))
        if not os.path.isfile(path) or not os.access(path, os.R_OK):
            raise CliError("required_file_unreadable", "required file is missing or unreadable", path=path)
        if not any(path.startswith(root.rstrip(os.sep) + os.sep) for root in [cwd] + roots):
            raise CliError("required_file_outside_scope", "declare the authorized parent with --read-dir before launch", path=path)
        required.append(dict(path=path, bytes=os.path.getsize(path), sha256=sha256_file(path)))
    return roots, required


def quota_gate(ctx, home=None, refresh=False):
    from claude_quota import refresh_from_jobs, status
    quota = refresh_from_jobs(ctx.state_dir, home) if refresh else status(ctx.state_dir, home)
    if quota["state"] == "paused":
        raise CliError("quota_paused", "Claude quota reached 90%; finish the current round and pause new calls", quota=quota)
    return quota


def validate_allow_rules(rules):
    """Accept only narrow, explicitly scoped permission rules.

    Bash is an enabled tool but deliberately absent from the base allowlist, so
    any shell use must arrive as a specific rule from the calling Codex task.
    Blanket ``Bash(*)``, MCP and plugin rules are refused outright.
    """
    out = []
    for raw in rules or []:
        rule = str(raw).strip()
        if not rule:
            continue
        if any(ch in rule for ch in "\n\r\t\x00"):
            raise CliError("bad_allow_tool", "invalid --allow-tool rule: %r" % raw)
        match = ALLOW_RULE_RE.match(rule)
        if not match:
            raise CliError("bad_allow_tool", "invalid --allow-tool rule: %r" % raw)
        tool, spec = match.group(1), match.group(2)
        if tool not in ALLOW_RULE_TOOLS:
            raise CliError(
                "bad_allow_tool",
                "tool %r is not one of %s" % (tool, ",".join(sorted(ALLOW_RULE_TOOLS))),
            )
        if spec is not None and not spec.strip():
            raise CliError("bad_allow_tool", "empty rule specifier: %r" % raw)
        if tool == "Bash":
            if not spec:
                raise CliError("bad_allow_tool", "Bash rules must be scoped, e.g. Bash(npm test:*)")
            stripped = spec.strip()
            if stripped in ("*", "*:*", ":*") or stripped.startswith("*"):
                raise CliError("bad_allow_tool", "refusing blanket Bash rule: %r" % raw)
        if rule not in out:
            out.append(rule)
    return out


def resolve_claude_bin(explicit):
    path = explicit or shutil.which("claude")
    if not path:
        raise CliError("no_claude", "claude executable not found on PATH; pass --claude-bin")
    real = os.path.realpath(path)
    if not (os.path.isfile(real) and os.access(real, os.X_OK)):
        raise CliError("no_claude", "not an executable file: %s" % path)
    return real


# --------------------------------------------------------------------------- #
# State root and job paths
# --------------------------------------------------------------------------- #


# Persisted job access and round path helpers live in delegate_job_store and
# are re-exported here; checkout reservation/owner validation live in
# delegate_ownership.


# --------------------------------------------------------------------------- #
# Claude invocation
# --------------------------------------------------------------------------- #


def enabled_tools(job):
    optional = {rule.split("(", 1)[0] for rule in job.get("allow_tools") or []}
    return ENABLED_TOOLS.split(",") + [t for t in CLAUDE_OPTIONAL if t in optional]


def build_claude_argv(job, run_token, resume):
    """Identical model/effort surface for the first call and every revision."""
    argv = [
        job["claude_bin"],
        "-p",
        "--name",
        run_token,
        "--restricted",
        "--include-partial-messages",
        "--model",
        MODEL,
        "--effort",
        EFFORT,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "dontAsk",
        "--tools",
        ",".join(enabled_tools(job)),
        "--allowed-tools",
        ",".join(BASE_ALLOWED_TOOLS),
    ]
    for directory in job.get("read_dirs") or []:
        argv += ["--add-dir", directory]
    if job.get("required_files") or job.get("allow_tools"):
        guidance = ("Required task inputs were checked before launch. Read their actual contents; "
                    "the manifest is not a substitute for reading. Use Read with explicit offset/limit "
                    "for large files and continue through needed ranges; a truncated excerpt is not the full file. "
                    "Use file tools for reading, not unapproved shell commands. Additional --add-dir directories "
                    "are task reference inputs; write only within the primary working directory unless the user authorized more.\n"
                    + json.dumps(dict(required_files=job.get("required_files", []),
                                      allow_tools=job.get("allow_tools", [])), ensure_ascii=False)
                    + "\nUse the authorized command forms above instead of guessing alternative spellings.")
        argv += ["--append-system-prompt", guidance]
    if resume:
        argv += ["--resume", job["session_id"]]
    else:
        argv += ["--session-id", job["session_id"]]
    return argv


def child_env():
    env = dict(os.environ)
    env.update(CHILD_ENV_OVERRIDES)
    return env


# --------------------------------------------------------------------------- #
# Evidence parsing: stdout NDJSON
# --------------------------------------------------------------------------- #


def parse_stream(path):
    """Summarise a stream-json stdout file without retaining transcript text.

    Raw values are preserved rather than coerced: a missing ``is_error`` must
    stay distinguishable from ``false`` so verification can demand the latter.
    """
    summary = {
        "missing": False,
        "lines": 0,
        "init_session_id": None,
        "assistant_models": [],
        "assistant_count": 0,
        "assistant_facts": [],
        "missing_assistant_models": 0,
        "result": None,
        "result_count": 0,
        "result_session_id": None,
        "result_text": "",
        "synthetic_markers": [],
        "permission_denials": 0,
        "parse_errors": 0,
    }
    if not os.path.isfile(path):
        summary["missing"] = True
        return summary
    with open(path, "rb") as handle:
        for raw in handle:
            if len(raw) > STDOUT_LINE_MAX_BYTES:
                summary["parse_errors"] += 1
                continue
            line = raw.strip()
            if not line:
                continue
            summary["lines"] += 1
            try:
                event = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                summary["parse_errors"] += 1
                continue
            if not isinstance(event, dict):
                summary["parse_errors"] += 1
                continue
            etype = event.get("type")
            if etype == "system" and event.get("subtype") == "init":
                summary["init_session_id"] = event.get("session_id")
            elif etype == "assistant":
                summary["assistant_count"] += 1
                summary["assistant_facts"].append(message_fact(event))
                model = (event.get("message") or {}).get("model")
                if not model:
                    summary["missing_assistant_models"] += 1
                if model and model not in summary["assistant_models"]:
                    summary["assistant_models"].append(model)
                if model in SYNTHETIC_MODEL_NAMES:
                    summary["synthetic_markers"].append("synthetic_model")
            elif etype == "result":
                summary["result_count"] += 1
                summary["result"] = {
                    # Kept raw on purpose: None (absent) is not False.
                    "subtype": event.get("subtype"),
                    "is_error": event.get("is_error"),
                    "num_turns": event.get("num_turns"),
                }
                summary["result_session_id"] = event.get("session_id")
                usage = event.get("usage")
                if isinstance(usage, dict):
                    summary["usage"] = {k: usage[k] for k in ("input_tokens", "cache_creation_input_tokens",
                        "cache_read_input_tokens", "output_tokens") if k in usage}
                text = event.get("result")
                if isinstance(text, str):
                    summary["result_text"] = text
                denials = event.get("permission_denials")
                if isinstance(denials, list):
                    summary["permission_denials"] = len(denials)
                elif isinstance(denials, int):
                    summary["permission_denials"] = denials
    summary["synthetic_markers"] = sorted(set(summary["synthetic_markers"]))
    return summary


# --------------------------------------------------------------------------- #
# Evidence parsing: local Claude transcript (effort ground truth)
# --------------------------------------------------------------------------- #


def claude_config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def encode_project_path(path):
    """Claude Code v2.1.261 encodes a project cwd by replacing non-alphanumerics."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def find_transcript(session_id, cwd, config_dir=None):
    """Locate exactly one transcript file for this session id.

    Preferred: the ``projects/<encoded cwd>/<session>.jsonl`` layout.  The
    fallback searches *filenames only* for the known UUID and demands a unique
    match, so no unrelated session is ever opened.
    """
    require_uuid(session_id, "session id")
    projects = os.path.join(config_dir or claude_config_dir(), "projects")
    candidates = []
    for base in (cwd, os.path.realpath(cwd)):
        candidate = os.path.join(projects, encode_project_path(base), session_id + ".jsonl")
        if candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate, "encoded_path"
    matches = sorted(set(glob.glob(os.path.join(projects, "*", session_id + ".jsonl"))))
    if len(matches) == 1:
        return matches[0], "filename_search"
    if len(matches) > 1:
        raise CliError("ambiguous_transcript", "%d transcripts match session %s" % (len(matches), session_id))
    return None, "missing"


def entry_effort(entry):
    """Only the verified top-level ``effort`` string counts as a record."""
    value = entry.get(EFFORT_FIELD)
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return None


# This policy recognizes one observed SDK continuation record, never generic
# synthetic responses. The unmodified stream still rejects every synthetic turn.
TRANSCRIPT_POLICY = "claude-cli-no-response-v1"


def message_fact(entry):
    message = entry.get("message") or {}
    content = message.get("content")
    return {
        "message_id": message.get("id"),
        "session_id": entry.get("sessionId") or entry.get("session_id"),
        "model": message.get("model"),
        "content_sha256": hashlib.sha256(json.dumps(
            content, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest(),
    }


def is_cli_no_response_placeholder(entry):
    """Positive, closed recognition of a non-model SDK continuation marker."""
    message = entry.get("message") or {}
    usage = message.get("usage")

    def zero_usage(value):
        if isinstance(value, dict):
            return all(zero_usage(item) for item in value.values())
        return value is None or (type(value) is int and value == 0)

    return (
        entry.get("type") == "assistant"
        and entry.get("isApiErrorMessage") is False
        and all(entry.get(key) is None for key in ("error", "apiError", "api_error"))
        and entry.get("isSidechain") is False
        and entry.get("entrypoint") == "sdk-cli"
        and entry_effort(entry) is None
        and valid_uuid(entry.get("uuid"))
        and valid_uuid(message.get("id"))
        and message.get("model") == "<synthetic>"
        and message.get("role") == "assistant"
        and message.get("type") == "message"
        and message.get("stop_reason") == "stop_sequence"
        and message.get("stop_sequence") == ""
        and message.get("content") == [{"type": "text", "text": "No response requested."}]
        and isinstance(usage, dict)
        and all(type(usage.get(key)) is int and usage[key] == 0 for key in (
            "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        and zero_usage(usage)
    )


def read_transcript_assistants(path):
    """Yield compact per-entry facts.  Message bodies are never retained."""
    entries = []
    version = None
    if not path or not os.path.isfile(path):
        return entries, version
    with open(path, "rb") as handle:
        for line_number, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("version"), str):
                version = entry["version"]
            if entry.get("type") != "assistant":
                continue
            entries.append(
                {
                    "uuid": entry.get("uuid"),
                    "session_id": entry.get("sessionId") or entry.get("session_id"),
                    "model": (entry.get("message") or {}).get("model"),
                    "effort": entry_effort(entry),
                    "message_fact": message_fact(entry),
                    "cli_placeholder": is_cli_no_response_placeholder(entry),
                    "line": line_number,
                    "record_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
    return entries, version


def snapshot_assistant_baseline(session_id, cwd, config_dir=None):
    """Pre-existing assistant uuids, plus how trustworthy that baseline is.

    An ambiguous or unreadable lookup must not collapse into an empty baseline:
    a resume would then count old entries as new.  The status travels with the
    round and fails verification later.
    """
    try:
        path, how = find_transcript(session_id, cwd, config_dir)
    except CliError:
        return [], "ambiguous"
    if not path:
        return [], "missing"
    try:
        entries, _ = read_transcript_assistants(path)
    except OSError:
        return [], "unreadable"
    if any(not entry.get("uuid") for entry in entries):
        return [], "unidentifiable"
    return [entry["uuid"] for entry in entries], "ok"


def verify_transcript(session_id, cwd, prior_uuids, baseline_status, stream_assistants=None, config_dir=None):
    out = {
        "transcript_found": False,
        "lookup": None,
        "baseline": baseline_status,
        "new_assistant_entries": 0,
        "new_model_entries": 0,
        "ignored_cli_placeholders": [],
        "efforts": [],
        "models": [],
        "cli_version": None,
        "effort_ok": False,
        "reasons": [],
    }
    if baseline_status not in ("ok", "missing"):
        # We cannot say which entries predate this round.
        out["reasons"].append("transcript_baseline_%s" % baseline_status)
        return out
    try:
        path, how = find_transcript(session_id, cwd, config_dir)
    except CliError as exc:
        out["lookup"] = "ambiguous"
        out["reasons"].append(exc.code)
        return out
    out["lookup"] = how
    if not path:
        out["reasons"].append("transcript_missing")
        return out
    out["transcript_found"] = True
    try:
        entries, version = read_transcript_assistants(path)
    except OSError as exc:
        out["reasons"].append("transcript_unreadable_errno_%s" % exc.errno)
        return out
    out["cli_version"] = version

    # An entry without a uuid cannot be placed before or after the baseline.
    if any(not entry.get("uuid") for entry in entries):
        out["reasons"].append("transcript_entry_unidentifiable")
        return out

    prior = set(prior_uuids or [])
    new = [entry for entry in entries if entry["uuid"] not in prior]
    out["new_assistant_entries"] = len(new)
    if not new:
        out["reasons"].append("no_new_assistant_entries")
        return out

    placeholders = [entry for entry in new if entry["cli_placeholder"]]
    if placeholders:
        real = [entry for entry in new if not entry["cli_placeholder"]]
        stream_facts = stream_assistants or []
        real_facts = [entry["message_fact"] for entry in real]
        stream_ids = {fact.get("message_id") for fact in stream_facts}
        # Every actual stream event must match new transcript content, session,
        # model and max effort; every new model record must belong to this stream.
        # A marker in stdout, an error, or missing identity never gets excluded.
        bound = (
            bool(stream_facts) and bool(real)
            and all(fact.get("message_id") and fact.get("session_id") == session_id
                    and fact.get("model") == MODEL and fact in real_facts for fact in stream_facts)
            and all(entry["message_fact"] in stream_facts and entry["effort"] == EFFORT for entry in real)
            and all(entry["session_id"] == session_id
                    and entry["message_fact"]["message_id"] not in stream_ids for entry in placeholders)
        )
        if bound:
            out["ignored_cli_placeholders"] = [
                {"uuid": entry["uuid"], "message_id": entry["message_fact"]["message_id"],
                 "line": entry["line"], "record_sha256": entry["record_sha256"],
                 "reason": TRANSCRIPT_POLICY} for entry in placeholders
            ]
            new = real
        else:
            out["reasons"].append("cli_placeholder_stream_binding_failed")
    out["new_model_entries"] = len(new)

    efforts, models, missing, wrong_session = [], [], 0, 0
    for entry in new:
        effort = entry.get("effort")
        if effort is None:
            missing += 1
        elif effort not in efforts:
            efforts.append(effort)
        model = entry.get("model")
        if not model:
            out["reasons"].append("transcript_model_missing")
        elif model not in models:
            models.append(model)
        # Every new entry must positively belong to this exact session.
        if entry.get("session_id") != session_id:
            wrong_session += 1
    out["efforts"] = efforts
    out["models"] = models
    if missing:
        out["reasons"].append("effort_missing_on_%d_entries" % missing)
    if wrong_session:
        out["reasons"].append("transcript_session_mismatch_on_%d_entries" % wrong_session)
    bad = sorted(effort for effort in efforts if effort != EFFORT)
    if bad:
        out["reasons"].append("effort_not_max:%s" % ",".join(bad))
    if models and any(model != MODEL for model in models):
        out["reasons"].append("transcript_model_mismatch:%s" % ",".join(sorted(models)))
    out["effort_ok"] = not out["reasons"]
    return out


# --------------------------------------------------------------------------- #
# Round verification
# --------------------------------------------------------------------------- #

# Reasons that mean "we could not tell", as opposed to "it demonstrably failed".
# These land in needs_attention so Codex inspects rather than assuming failure.
SOFT_REASONS = frozenset(
    ("transcript_missing", "no_new_assistant_entries", "ambiguous_transcript", "evidence_missing")
)
SOFT_REASON_PREFIXES = (
    "effort_missing_on_",
    "transcript_unreadable_errno_",
    "transcript_baseline_",
    "transcript_entry_unidentifiable",
)


def verify_round(job, stdout_path, exit_code, prior_uuids, baseline_status, sdk_boundary=False):
    """A round passes only when every independent signal agrees.

    Claude's final answer is never sufficient.  Required evidence, all of it
    positive (absent evidence fails): exit 0, exactly one result event with
    ``is_error is False`` and ``subtype == "success"`` and a ``session_id``
    equal to the saved session, assistant events reporting the real model, and
    a transcript whose new entries all record effort=max.
    """
    stream = parse_stream(stdout_path)
    session_id = job["session_id"]
    reasons = []

    if stream.get("missing"):
        reasons.append("evidence_missing")
    if stream.get("parse_errors"):
        reasons.append("stream_parse_errors")
    if stream.get("missing_assistant_models"):
        reasons.append("assistant_model_missing")
    if exit_code != 0 and not sdk_boundary:
        reasons.append("exit_code:%s" % exit_code)

    result = stream.get("result")
    count = stream.get("result_count", 0)
    if count != 1:
        reasons.append("result_events:%d" % count)
    if result:
        # `is not False` deliberately rejects a missing or non-boolean field.
        if result.get("is_error") is not False:
            reasons.append("result_is_error:%r" % (result.get("is_error"),))
        if result.get("subtype") != "success":
            reasons.append("result_subtype:%s" % result.get("subtype"))
    # Exact equality, so an absent session_id fails rather than being skipped.
    if stream.get("result_session_id") != session_id:
        reasons.append("result_session_mismatch")
    if stream.get("init_session_id") not in (None, session_id):
        reasons.append("init_session_mismatch")

    models = stream.get("assistant_models") or []
    if stream.get("assistant_count", 0) == 0:
        reasons.append("no_assistant_events")
    elif not models:
        reasons.append("no_assistant_model")
    elif any(model != MODEL for model in models):
        reasons.append("assistant_model:%s" % ",".join(sorted(models)))

    # A synthetic stand-in turn fails even when the result says success.
    reasons.extend(stream.get("synthetic_markers") or [])

    transcript = verify_transcript(session_id, job["cwd"], prior_uuids, baseline_status,
                                   stream.get("assistant_facts"), job.get("claude_config_dir"))
    reasons.extend(transcript["reasons"])

    return {
        "ok": not reasons,
        "reasons": reasons,
        "needs_attention": bool(reasons) and all(
            reason in SOFT_REASONS or reason.startswith(SOFT_REASON_PREFIXES) for reason in reasons
        ),
        "exit_code": exit_code,
        "completion_basis": "sdk_result" if sdk_boundary else "process_exit",
        "result_event": result,
        "usage": stream.get("usage") or {},
        "session_ok": stream.get("result_session_id") == session_id,
        "model_verified": bool(models) and all(model == MODEL for model in models),
        "assistant_models": models,
        "effort_verified": transcript["effort_ok"],
        "efforts": transcript["efforts"],
        "new_assistant_entries": transcript["new_assistant_entries"],
        "new_model_entries": transcript["new_model_entries"],
        "ignored_cli_placeholders": transcript["ignored_cli_placeholders"],
        "verification_policy": TRANSCRIPT_POLICY,
        "transcript_lookup": transcript["lookup"],
        "transcript_baseline": transcript["baseline"],
        "cli_version": transcript["cli_version"],
        # Denials are recorded honestly; they do not by themselves prove the
        # task failed, but they are surfaced for Codex to inspect.
        "permission_denials": stream.get("permission_denials", 0),
        "stream_lines": stream.get("lines", 0),
        "stream_parse_errors": stream.get("parse_errors", 0),
        "report": clip(stream.get("result_text")),
    }


# --------------------------------------------------------------------------- #
# Launching a round
# --------------------------------------------------------------------------- #


def new_round(job_id, index, kind, prompt_rel, prompt_sha, baseline, baseline_status):
    short = job_id.replace("-", "")[:8]
    return {
        "round": index,
        "kind": kind,
        "run_token": "codexdel-%s-r%03d-%s" % (short, index, uuid.uuid4().hex[:10]),
        # Single-use: the worker consumes it, so a second private worker for
        # this round cannot invoke Claude again.
        "launch_claim": uuid.uuid4().hex,
        "claim_consumed": False,
        "prompt": prompt_rel,
        "prompt_sha256": prompt_sha,
        "status": "launching",
        "started_at": iso(),
        "started_epoch": now(),
        "finished_at": None,
        "exit_code": None,
        "finalized": False,
        "evidence": {
            "stdout": "rounds/r%03d/stdout.ndjson" % index,
            "stderr": "rounds/r%03d/stderr.log" % index,
            "worker_log": "rounds/r%03d/worker.log" % index,
        },
        "prior_assistant_uuids": baseline,
        "baseline_status": baseline_status,
        # Process identity lives on the round, so a later round can never erase
        # the record of an earlier round's still-running process.
        "worker": None,
        "claude": None,
        "claude_launch_pending": False,
        "verification": None,
    }


def job_environment(job, env):
    """Preserve unset vs explicit config path: Claude uses distinct auth stores."""
    if job.get("backend", "claude") != "claude":
        return env
    if "claude_config_env" in job:
        original = job["claude_config_env"]
        if original is None:
            env.pop("CLAUDE_CONFIG_DIR", None)
        else:
            env["CLAUDE_CONFIG_DIR"] = original
    elif job.get("claude_config_dir"):
        from claude_quota import account_home
        if account_home() != job["claude_config_dir"]:
            raise CliError("account_context_changed", "restore the original Claude config environment before resuming this legacy job")
    return env


def launch_transaction(ctx, job, index, resume):
    """Spawn the round's worker and register its identity, still under the lock.

    The caller must hold the state lock.  The worker's first act is to take the
    same lock, so it cannot proceed until this transaction has committed; no
    observer ever sees a launched-but-unregistered round.  The worker is
    session-detached with no inherited pipes, so the calling command returns
    promptly, Claude's output never reaches the caller's stdio, and a
    controller crash cannot take the run down.
    """
    job_root = ctx.job_dir(job["job_id"])
    record = job["rounds"][index]
    if job.get("transport") == "sdk":
        from claude_sdk_backend import reuse_worker
        if reuse_worker(ctx, job, record):
            return record["worker"]
    run_token = record["run_token"]
    worker_log = os.path.join(ensure_dir(round_dir(job_root, index)), "worker.log")

    argv = [
        sys.executable,
        os.path.realpath(__file__),
        # Global options belong to the top-level parser: they must precede the
        # subcommand or argparse rejects them.
        "--state-dir",
        ctx.state_dir,
        "_worker",
        "--job",
        job["job_id"],
        "--round",
        str(index),
        "--run-token",
        run_token,
        "--claim",
        record["launch_claim"],
    ]
    if resume:
        argv.append("--resume")

    log_fd = os.open(worker_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            cwd=job_root,
            env=job_environment(job, dict(os.environ)),
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        record.update(status="not_started", finalized=True, finished_at=iso())
        job["phase"] = PHASE_FAILED
        job["attention"] = {"reason": "worker_spawn_failed", "errno": exc.errno}
        ctx.save(job)
        raise CliError("worker_spawn_failed", "worker could not start", errno=exc.errno)
    finally:
        os.close(log_fd)

    record["worker"] = capture_identity(proc.pid, run_token)
    if job.get("transport") == "sdk":
        job["sdk_worker"] = record["worker"]
        job["sdk_closing"] = False
    ctx.save(job)
    # A long-lived MCP parent must reap detached workers after they exit.
    threading.Thread(target=proc.wait, name="delegate-worker-reaper", daemon=True).start()
    return record["worker"]


# --------------------------------------------------------------------------- #
# Live-process blockers and crash reconciliation live in
# delegate_recovery and are re-exported here for the existing surface.


# --------------------------------------------------------------------------- #
# Public commands
# --------------------------------------------------------------------------- #


def backend_baseline(job):
    """Provider-neutral baseline lookup via the transport adapter registry."""
    from delegate_transport import for_job
    return for_job(job).baseline(job)


def cmd_capabilities(ctx, args):
    from tool_catalog import capabilities
    return capabilities()


def cmd_start(ctx, args):
    owner = ctx.owner()
    cwd = validate_cwd(args.cwd)
    prompt = validate_input_file(args.prompt_file, "--prompt-file", PROMPT_MAX_BYTES)
    timeout = validate_timeout(args.timeout)
    max_revisions = validate_max_revisions(args.max_revisions)
    allow_tools = validate_allow_rules(args.allow_tool)
    backend = args.backend
    transport = getattr(args, "transport", "cli")
    # Provider-specific start preparation is selected through the adapter.
    adapter = delegate_transport.for_name(backend)
    adapter.transport_preflight(transport)
    read_dirs, required_files = read_access(cwd, getattr(args, "read_dir", []), getattr(args, "require_file", []))
    fields, result_extra, session_id = adapter.start_config(ctx, args, prompt, read_dirs, required_files)
    quota = result_extra.get("quota")
    key = reservation_key(cwd)

    job_id = str(uuid.uuid4())

    with StateLock(ctx.state_dir):
        conflict = find_conflict(ctx, key)
        if conflict:
            raise CliError(
                "checkout_conflict",
                "an unfinished job already holds this checkout",
                conflict_job=conflict.get("job_id"),
                conflict_phase=conflict.get("phase"),
                conflict_mine=conflict.get("owner") == owner,
                reservation=key,
            )
        job_root = ctx.job_dir(job_id)
        ensure_dir(job_root)
        ensure_dir(os.path.join(job_root, "prompts"))
        ensure_dir(os.path.join(job_root, "rounds"))
        ensure_dir(os.path.join(job_root, "notes"))

        saved_prompt = prompt_path(job_root, 0)
        copy_private(prompt, saved_prompt, PROMPT_MAX_BYTES)

        job = {
            "schema": SCHEMA_VERSION,
            "schema_namespace": JOB_STATE_SCHEMA_NAMESPACE,
            "job_id": job_id,
            "owner": owner,
            "cwd": cwd,
            "reservation_key": key,
            "session_id": session_id,
            "phase": PHASE_STARTING,
            "created_at": iso(),
            "updated_at": iso(),
            "timeout": timeout,
            "max_revisions": max_revisions,
            "revisions_used": 0,
            "current_round": 0,
            "backend": backend,
            "transport": transport,
            **fields,
            "allow_tools": allow_tools,
            "read_dirs": read_dirs,
            "required_files": required_files,
            "stop_requested": False,
            "process_state": "starting",
            "cli_version": None,
            "final_report": "",
            "rounds": [
                new_round(
                    job_id,
                    0,
                    "initial",
                     os.path.relpath(saved_prompt, job_root),
                     sha256_file(saved_prompt),
                     *delegate_transport.for_name(backend).initial_baseline(session_id, cwd)
                 )
            ],
        }
        job["rounds"][0]["timeout"] = timeout
        job["rounds"][0]["request"] = getattr(args, "request", None)
        ctx.save(job)
        # Spawn and identity registration commit inside this same lock hold.
        identity = launch_transaction(ctx, job, 0, resume=False)

    return {
        "ok": True,
        "job_id": job_id,
        "backend": backend,
        "phase": job["phase"],
        "round": 0,
        "session_id": session_id,
        "owner": owner,
        "cwd": cwd,
        "reservation": key,
        "timeout": timeout,
        "max_revisions": max_revisions,
        "state_dir": ctx.state_dir,
        "worker_identity_verified": bool(identity.get("identity_verified")),
        "quota": quota,
        "read_dirs": read_dirs,
        "required_files": required_files,
    }


def cmd_revise(ctx, args):
    try:
        return revise_transaction(ctx, args)
    except CliError as exc:
        if exc.code != "sdk_scope_changed":
            raise
        # Drain only the already-idle connection, outside the state lock. The
        # second transaction rechecks the round, ownership and checkout lock.
        from claude_sdk_backend import close_idle
        close_idle(ctx, args.job, exc.extra["round"])
        refreshed = argparse.Namespace(**vars(args))
        refreshed.expected_round = exc.extra["round"]
        return revise_transaction(ctx, refreshed)


def revise_transaction(ctx, args):
    prompt = validate_input_file(args.prompt_file, "--prompt-file", PROMPT_MAX_BYTES)
    requested_timeout = getattr(args, "timeout", "inherit")
    if requested_timeout != "inherit":
        requested_timeout = validate_timeout(requested_timeout)

    with StateLock(ctx.state_dir):
        job = ctx.load_owned(args.job)
        job, _ = reconcile(ctx, job)
        phase = job.get("phase")
        expected = getattr(args, "expected_round", None)
        if expected is not None and expected != job.get("current_round"):
            raise CliError("stale_round", "job advanced; inspect the current round before revising")

        if phase in BUSY_PHASES:
            raise CliError("busy", "job is still running (phase=%s); wait or stop it first" % phase, phase=phase)
        if phase in RECOVER_PHASES:
            if not args.recover:
                raise CliError(
                    "needs_recover",
                    "phase=%s requires --recover and a prompt written after inspecting evidence" % phase,
                    phase=phase,
                )
        elif phase not in (PHASE_AWAITING_REVIEW, PHASE_ACCEPTED):
            raise CliError("bad_phase", "cannot revise from phase=%s" % phase, phase=phase)

        # --recover acknowledges an inspected, finished task; it is never
        # permission to start a second Claude beside one that may still be live.
        blockers = live_blockers(job, allow_idle_sdk=True)
        if blockers:
            raise CliError(
                "live_process",
                "refusing to revise while earlier processes are not provably gone; "
                "stop the job first",
                blockers=blockers,
                phase=phase,
            )

        used = int(job.get("revisions_used", 0))
        requested_limit = getattr(args, "max_revisions", None)
        limit = validate_max_revisions(requested_limit if requested_limit is not None else job.get("max_revisions"))
        if limit is not None and used >= limit:
            raise CliError(
                "revision_limit",
                "max-revisions=%d already used; no further correction calls" % limit,
                revisions_used=used,
                max_revisions=limit,
            )

        # Provider-specific revision preparation is selected through the adapter.
        adapter = delegate_transport.for_job(job)
        additions = validate_allow_rules(getattr(args, "allow_tool", None))
        adapter.revision_additions(additions)
        allow_tools = list(dict.fromkeys((job.get("allow_tools") or []) + additions))
        dirs = list(dict.fromkeys((job.get("read_dirs") or []) + (getattr(args, "read_dir", None) or [])))
        inputs = list(dict.fromkeys([v["path"] for v in job.get("required_files") or []] + (getattr(args, "require_file", None) or [])))
        read_dirs, required_files = read_access(job["cwd"], dirs, inputs)
        revision_fields = adapter.revision_config(ctx, job, read_dirs, required_files)

        # A revision must still own the checkout; if another job took it while
        # this one was released (e.g. after stop), refuse rather than double-write.
        conflict = find_conflict(ctx, job["reservation_key"], ignore_job_id=job["job_id"])
        if conflict:
            raise CliError(
                "checkout_conflict",
                "another unfinished job now holds this checkout",
                conflict_job=conflict.get("job_id"),
                conflict_phase=conflict.get("phase"),
            )

        job.update(revision_fields)

        if (job.get("transport") == "sdk" and identity_state(job.get("sdk_worker")) == "alive"
                and (read_dirs != job.get("read_dirs", []) or allow_tools != job.get("allow_tools", []))):
            raise CliError("sdk_scope_changed", "refreshing idle connection for authorized additions",
                           round=job["current_round"])

        job_root = ctx.job_dir(job["job_id"])
        index = len(job["rounds"])
        saved_prompt = prompt_path(job_root, index)
        copy_private(prompt, saved_prompt, PROMPT_MAX_BYTES)

        adapter.revision_prompt_check(prompt)

        record = new_round(
            job["job_id"],
            index,
            "revision",
            os.path.relpath(saved_prompt, job_root),
            sha256_file(saved_prompt),
            # Only assistant entries added after this point count as this
            # round's work; an untrustworthy baseline is carried, not hidden.
            *backend_baseline(job)
        )
        # Retain the prior round's policy before changing this job's default.
        current_round(job)[1].setdefault("timeout", job.get("timeout"))
        if requested_timeout != "inherit":
            job["timeout"] = requested_timeout
        record["timeout"] = job.get("timeout")
        record["recovered_from"] = phase if args.recover else None
        record["request"] = getattr(args, "request", None)
        current_round(job)[1].setdefault("access", dict(allow_tools=job.get("allow_tools", []),
                                                     read_dirs=job.get("read_dirs", []),
                                                     required_files=job.get("required_files", [])))
        job["rounds"].append(record)
        job["current_round"] = index
        job["revisions_used"] = used + 1
        job["max_revisions"] = limit
        current_round(job)[1]["access"] = dict(allow_tools=allow_tools, read_dirs=read_dirs,
                                                 required_files=required_files)
        job["allow_tools"] = allow_tools
        job["read_dirs"], job["required_files"] = read_dirs, required_files
        if job.get("accepted"):
            job.setdefault("acceptance_history", []).append(job.pop("accepted"))
        record["continued_from"] = phase
        job["phase"] = PHASE_STARTING
        job["process_state"] = "starting"
        job["stop_requested"] = False
        job.pop("attention", None)
        ctx.save(job)
        identity = launch_transaction(ctx, job, index, resume=True)

    return {
        "ok": True,
        "job_id": job["job_id"],
        "phase": job["phase"],
        "round": index,
        "session_id": job["session_id"],
        "resume": True,
        "revisions_used": job["revisions_used"],
        "max_revisions": job["max_revisions"],
        "recovered": bool(args.recover),
        "worker_identity_verified": bool(identity.get("identity_verified")),
    }


def status_payload(ctx, job):
    job_root = os.path.join(ctx.jobs_dir, job["job_id"])
    index, record = current_round(job)
    verification = record.get("verification") or {}
    evidence = record.get("evidence") or {}

    payload = {
        "ok": True,
        "job_id": job["job_id"],
        "owner": job.get("owner"),
        "phase": job.get("phase"),
        "cwd": job.get("cwd"),
        "reservation": job.get("reservation_key"),
        "reservation_held": job.get("phase") in RESERVING_PHASES,
        "session_id": job.get("session_id"),
        "round": index,
        "round_status": record.get("status"),
        "rounds_total": len(job.get("rounds") or []),
        "revisions_used": job.get("revisions_used", 0),
        "max_revisions": job.get("max_revisions"),
        "timeout": job.get("timeout"),
        "backend": job.get("backend", "claude"),
        "transport": job.get("transport", "cli"),
        "opencode_tools": job.get("opencode_tools"),
        "kimi_tools": job.get("kimi_tools"),
        "kimi_hooks": job.get("kimi_hooks"),
        "pi_tools": job.get("pi_tools"),
        "codex_tools": job.get("codex_tools"),
        "requested": {"model": job.get("model"), "effort": job.get("effort")},
        "verified": {
            "ok": bool(verification.get("ok")),
            "model": bool(verification.get("model_verified")),
            "effort": bool(verification.get("effort_verified")),
            "session": bool(verification.get("session_ok")),
            "exit_code": verification.get("exit_code"),
            "completion_basis": verification.get("completion_basis", "process_exit"),
            "actual_models": verification.get("assistant_models") or [],
            "actual_efforts": verification.get("efforts") or [],
            "new_assistant_entries": verification.get("new_assistant_entries"),
            "transcript": verification.get("transcript_lookup"),
            "permission_denials": verification.get("permission_denials", 0),
            "reasons": verification.get("reasons") or [],
        },
        "process": process_view(job),
        "cli_version": job.get("cli_version"),
        "allow_tools": job.get("allow_tools") or [],
        "final_report": clip(job.get("final_report")),
        "artifacts": {
            "job_dir": job_root,
            "job_state": os.path.join(job_root, "job.json"),
            "prompt": os.path.join(job_root, record["prompt"]) if record.get("prompt") else None,
            "stdout": os.path.join(job_root, evidence["stdout"]) if evidence.get("stdout") else None,
            "stderr": os.path.join(job_root, evidence["stderr"]) if evidence.get("stderr") else None,
            "worker_log": os.path.join(job_root, evidence["worker_log"]) if evidence.get("worker_log") else None,
        },
        "updated_at": job.get("updated_at"),
    }
    monitor_path = os.path.join(round_dir(job_root, index), "monitor.json")
    try:
        payload["progress"] = read_json(monitor_path).get("progress")
    except (OSError, ValueError):
        payload["progress"] = None
    if job.get("attention"):
        payload["attention"] = job["attention"]
    if job.get("accepted"):
        payload["accepted"] = job["accepted"]
    if job.get("backend", "claude") == "claude":
        from claude_quota import status
        payload["quota"] = status(ctx.state_dir, job.get("claude_config_dir"))
        payload["read_dirs"] = job.get("read_dirs") or []
        payload["required_files"] = job.get("required_files") or []
    return payload


def cmd_status(ctx, args):
    with StateLock(ctx.state_dir):
        job = ctx.load_owned(args.job)
        job, _ = reconcile(ctx, job)
        return status_payload(ctx, job)


def cmd_quota(ctx, args):
    from claude_quota import refresh_from_jobs
    return dict(ok=True, **refresh_from_jobs(ctx.state_dir))


def cmd_wait(ctx, args):
    try:
        seconds = int(args.seconds)
    except (TypeError, ValueError):
        raise CliError("bad_wait", "--seconds must be an integer")
    if seconds < 0:
        raise CliError("bad_wait", "--seconds must be >= 0")
    capped = min(seconds, WAIT_CAP_SECONDS)

    started = time.monotonic()
    deadline = started + capped
    settled = False
    while True:
        # Read-only with respect to Claude: this only polls recorded state and
        # local process liveness, and never makes a model call.
        with StateLock(ctx.state_dir):
            job = ctx.load_owned(args.job)
            job, _ = reconcile(ctx, job)
        if job.get("phase") not in BUSY_PHASES:
            settled = True
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(WAIT_POLL_SECONDS, remaining))

    payload = status_payload(ctx, job)
    payload["requested_seconds"] = seconds
    payload["waited_seconds"] = round(time.monotonic() - started, 3)
    payload["wait_capped"] = seconds > WAIT_CAP_SECONDS
    payload["settled"] = settled
    return payload


def cmd_await_event(ctx, args):
    """Quiet local wait; returns only an incident, checkpoint, or settled state."""
    try:
        after_round, after_seq = (int(x) for x in args.after.split(":"))
        if after_round < -1 or after_seq < 0:
            raise ValueError()
    except (ValueError, AttributeError):
        raise CliError("bad_cursor", "--after must be round:sequence, initially -1:0")
    last_reconcile = -float("inf")
    while True:
        with StateLock(ctx.state_dir):
            job = ctx.load_owned(args.job)
            if time.monotonic() - last_reconcile >= 5:
                job, _ = reconcile(ctx, job)
                last_reconcile = time.monotonic()
        index, record = current_round(job)
        if after_round > index:
            raise CliError("bad_cursor", "cursor belongs to a future round")
        path = os.path.join(round_dir(ctx.job_dir(args.job), index), "monitor.json")
        try:
            monitor = read_json(path)
        except FileNotFoundError:
            monitor = {"events": [], "progress": None}
        events = monitor.get("events") or []
        seq = after_seq if after_round == index else 0
        # A final state supersedes queued diagnostics/checkpoints. Never send
        # an obsolete error or 10-minute check after completion has been saved.
        if job.get("phase") not in BUSY_PHASES:
            payload = status_payload(ctx, job)
            terminal_seq = len(events) + 1
            payload.update(cursor="%d:%d" % (index, terminal_seq),
                           event={"kind": "settled"} if seq < terminal_seq else None,
                           progress=monitor.get("progress"))
            return payload
        pending = [event for event in events if event["seq"] > seq]
        if pending:
            payload = status_payload(ctx, job)
            # One burst may contain a hook and fallback error; use the latest
            # snapshot so Codex can already see subsequent successful activity.
            payload.update(cursor="%d:%d" % (index, pending[-1]["seq"]),
                           event=pending[-1], progress=monitor.get("progress"))
            return payload
        time.sleep(WAIT_POLL_SECONDS)


def cmd_list(ctx, args):
    owner = ctx.owner()
    items = []
    with StateLock(ctx.state_dir):
        for job in ctx.all_jobs():
            if job.get("owner") != owner:
                continue
            job, _ = reconcile(ctx, job)
            index, record = current_round(job)
            items.append(
                {
                    "job_id": job.get("job_id"),
                    "backend": job.get("backend", "claude"),
                    "phase": job.get("phase"),
                    "cwd": job.get("cwd"),
                    "reservation": job.get("reservation_key"),
                    "reservation_held": job.get("phase") in RESERVING_PHASES,
                    "session_id": job.get("session_id"),
                    "round": index,
                    "round_status": record.get("status"),
                    "revisions_used": job.get("revisions_used", 0),
                    "max_revisions": job.get("max_revisions"),
                    "process": process_view(job)["state"],
                    "updated_at": job.get("updated_at"),
                }
            )
    items.sort(key=lambda item: item.get("updated_at") or "")
    return {"ok": True, "owner": owner, "count": len(items), "jobs": items}


def cmd_stop(ctx, args):
    with StateLock(ctx.state_dir):
        job = ctx.load_owned(args.job)
        # Committed under the lock, before any signal.  The launch transaction
        # also runs under this lock, so afterwards either a worker is already
        # registered here or it will read this flag and abort before spawning
        # Claude: no launch can still create a process we have not seen.
        job["stop_requested"] = True
        ctx.save(job)
        targets = [
            (record.get("round"), name, record.get(name))
            for record in job.get("rounds") or []
            for name in ("claude", "worker")
            if record.get(name)
        ]

    # Signalling happens outside the lock; the worker needs the lock to record
    # its own teardown.  Nothing is deleted here: evidence is retained.
    signals = []
    for index, name, record in targets:
        result = terminate_recorded(record)
        result.update({"round": index, "process": name, "pid": record.get("pid")})
        signals.append(result)

    with StateLock(ctx.state_dir):
        job = ctx.load(args.job)
        remaining = live_blockers(job)
        # Provider-neutral stop transition lives in delegate_recovery.
        delegate_recovery.stop_transition(job, remaining, signals)
        ctx.save(job)

    return {
        "ok": True,
        "job_id": job["job_id"],
        "phase": job["phase"],
        "stopped": not remaining,
        "reservation_released": job["phase"] not in RESERVING_PHASES,
        "signals": signals,
        "remaining": remaining,
        "evidence_retained": True,
    }


def revalidation_sources(ctx, job, record):
    """Require original completion seals; never manufacture a historical seal."""
    from review_evidence import inside, EvidenceError
    try:
        root = ctx.job_dir(job["job_id"])
        stdout = str(inside(root, record["evidence"]["stdout"]))
        prompt = str(inside(root, record["prompt"]))
        sealed = (record.get("evidence_sha256") or {}).get("stdout")
        if not sealed or sha256_file(stdout) != sealed:
            raise CliError("evidence_changed", "original finalized stream seal missing or changed")
        if not record.get("prompt_sha256") or sha256_file(prompt) != record["prompt_sha256"]:
            raise CliError("evidence_changed", "original task prompt changed")
        transcript, _ = find_transcript(job["session_id"], job["cwd"])
        if not transcript:
            raise CliError("evidence_missing", "session transcript missing")
        return stdout, {"stdout_sha256": sealed, "prompt_sha256": record["prompt_sha256"],
                        "transcript_sha256": sha256_file(transcript),
                        "transcript_anchor": "revalidation_time_only"}
    except (KeyError, EvidenceError) as exc:
        raise CliError("invalid_evidence", str(exc))


def revalidation_blockers(job):
    blockers = live_blockers(job)
    index, record = current_round(job)
    # A finalized successful process must have identities for both processes.
    # Generic live_blockers permits unknown for never-launched historical rounds.
    for name in ("worker", "claude"):
        state = identity_state(record.get(name))
        if state != "exited":
            blockers.append({"round": index, "process": name, "state": state})
    return blockers


def cmd_revalidate(ctx, args):
    """Recheck a finalized Claude verifier failure without a model invocation.

    Original verification, phase and status remain in an append-only audit
    entry. Only a fresh complete PASS can move the job to awaiting_review.
    Acceptance remains a separate Codex action.
    """
    with StateLock(ctx.state_dir):
        job = ctx.load_owned(args.job)
        index, record = current_round(job)
        old = record.get("verification") or {}
        reasons = old.get("reasons") or []
        if job.get("backend", "claude") != "claude" or job.get("phase") != PHASE_FAILED:
            raise CliError("bad_phase", "revalidate requires a failed Claude job")
        if index != args.expected_round:
            raise CliError("stale_round", "current round differs from --expected-round")
        if (job.get("stop_requested") or record.get("finalized") is not True
                or type(record.get("exit_code")) is not int or record["exit_code"] != 0
                or record.get("timed_out") or not record.get("finished_at")
                or old.get("ok") is not False or old.get("model_verified") is not True
                or old.get("session_ok") is not True or not reasons
                or not all(re.fullmatch(r"effort_missing_on_[1-9][0-9]*_entries", reason)
                           or reason.startswith("transcript_model_mismatch:") for reason in reasons)):
            raise CliError("not_revalidatable", "requires a finalized model/session-verified transcript-only failure")
        blockers = revalidation_blockers(job)
        if blockers:
            raise CliError("processes_not_gone", "all recorded processes must be provably exited", blockers=blockers)
        conflict = find_conflict(ctx, reservation_key(job["cwd"]), ignore_job_id=job["job_id"])
        if conflict:
            raise CliError("cwd_busy", "another job holds this working directory")
        stdout, before = revalidation_sources(ctx, job, record)
        if before["stdout_sha256"] != args.expected_stream_sha256:
            raise CliError("evidence_changed", "stream differs from --expected-stream-sha256")
        verification = verify_round(job, stdout, record["exit_code"],
                                    record.get("prior_assistant_uuids") or [],
                                    record.get("baseline_status") or "unknown")
        _, after = revalidation_sources(ctx, job, record)
        if before != after:
            raise CliError("evidence_changed", "evidence changed during revalidation")
        # This recovery path is specifically for the recognized non-model marker.
        if verification["ok"] and not verification.get("ignored_cli_placeholders"):
            raise CliError("not_revalidatable", "no positively bound CLI placeholder explains the old failure")
        audit = {"at": iso(), "by": job["owner"], "round": index,
                 "policy": TRANSCRIPT_POLICY, "verifier_sha256": sha256_file(os.path.realpath(__file__)),
                 "source_seals": after, "previous_phase": job["phase"],
                 "previous_status": record.get("status"), "previous_verification": old,
                 "verification": verification}
        record.setdefault("revalidations", []).append(audit)
        if verification["ok"]:
            record["verification"] = verification
            record["status"] = "done"
            job["phase"] = PHASE_AWAITING_REVIEW
            job.pop("attention", None)
        ctx.save(job)
    return {"ok": verification["ok"], "job_id": job["job_id"], "round": index,
            "phase": job["phase"], "revalidation": audit, "model_invoked": False}


def cmd_accept(ctx, args):
    notes = validate_input_file(args.notes_file, "--notes-file", NOTES_MAX_BYTES)
    if ctx.load_owned(args.job).get("transport") == "sdk":
        from claude_sdk_backend import close_idle
        close_idle(ctx, args.job, getattr(args, "expected_round", None))

    with StateLock(ctx.state_dir):
        job = ctx.load_owned(args.job)
        expected = getattr(args, "expected_round", None)
        if expected is not None and expected != job.get("current_round"):
            raise CliError("stale_round", "review refers to an older round")
        job, _ = reconcile(ctx, job)
        if job.get("phase") == PHASE_ACCEPTED:
            raise CliError("already_accepted", "job is already accepted")
        if job.get("phase") != PHASE_AWAITING_REVIEW:
            raise CliError(
                "bad_phase",
                "accept requires phase=awaiting_review, got %s" % job.get("phase"),
                phase=job.get("phase"),
            )
        index, record = current_round(job)
        verification = record.get("verification") or {}
        if not (verification.get("ok") and verification.get("model_verified") and verification.get("effort_verified")):
            raise CliError(
                "unverified",
                "refusing to accept: model/effort verification did not pass",
                reasons=verification.get("reasons") or ["missing_verification"],
            )

        if record.get("revalidations"):
            if revalidation_blockers(job):
                raise CliError("processes_not_gone", "revalidated job has an unverified process")
            _, current_seals = revalidation_sources(ctx, job, record)
            if current_seals != record["revalidations"][-1]["source_seals"]:
                raise CliError("evidence_changed", "evidence changed since revalidation")

        review_evidence = None
        if getattr(args, "evidence_dir", None):
            from review_evidence import verify, EvidenceError
            try:
                review_evidence = verify(args.evidence_dir, job=job, round_index=index,
                                         source_root=ctx.job_dir(job['job_id']))
            except (EvidenceError, OSError) as exc:
                raise CliError("invalid_review_evidence", str(exc))

        job_root = ctx.job_dir(job["job_id"])
        notes_dir = ensure_dir(os.path.join(job_root, "notes"))
        saved_notes = os.path.join(notes_dir, "r%03d-accept.md" % index)
        copy_private(notes, saved_notes, NOTES_MAX_BYTES)

        delegate_completion.apply_acceptance(
            job, index, os.path.relpath(saved_notes, job_root), sha256_file(saved_notes),
            review_evidence, job.get("model", MODEL), job.get("effort", EFFORT), iso())
        ctx.save(job)

    return {
        "ok": True,
        "job_id": job["job_id"],
        "phase": PHASE_ACCEPTED,
        "round": index,
        "reservation_released": True,
        "notes": saved_notes,
    }


def cmd_evidence(ctx, args):
    from review_evidence import export, verify, EvidenceError
    try:
        if args.verify_dir:
            if args.job or args.output_dir or args.subject or args.round is not None:
                raise CliError("evidence_arguments", "--verify-dir cannot be combined with export arguments")
            return verify(args.verify_dir, expected_sha256=args.expected_sha256)
        if args.expected_sha256:
            raise CliError("evidence_arguments", "--expected-sha256 requires --verify-dir")
        if not args.job or not args.output_dir or not args.subject:
            raise CliError("evidence_arguments", "export requires a job, --output-dir and --subject")
        with StateLock(ctx.state_dir):
            job = ctx.load_owned(args.job)
            index = job.get("current_round", 0) if args.round is None else args.round
        return export(ctx, job, index, args.output_dir, args.subject, iso())
    except (EvidenceError, OSError) as exc:
        raise CliError("invalid_review_evidence", str(exc))


# --------------------------------------------------------------------------- #
# Worker (private subcommand: one Claude invocation, then exit)
# --------------------------------------------------------------------------- #


def wlog(handle, event, **fields):
    """Compact metadata-only worker log.  Never Claude output, never env."""
    record = {"t": iso(), "event": event}
    record.update(fields)
    try:
        handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
        handle.flush()
    except (OSError, ValueError):
        pass


def cmd_worker(ctx, args):
    job_id = require_uuid(args.job, "job id")
    try:
        index = int(args.round)
    except (TypeError, ValueError):
        raise CliError("bad_round", "--round must be an integer")
    if index < 0:
        raise CliError("bad_round", "--round must be >= 0")

    job_root = ctx.job_dir(job_id)
    rdir = ensure_dir(round_dir(job_root, index))
    with open(os.path.join(rdir, "worker.log"), "a", encoding="utf-8") as log:
        try:
            return worker_run(ctx, args, job_id, index, job_root, log)
        except CliError as exc:
            wlog(log, "worker_error", code=exc.code, message=exc.message)
            if exc.code not in ("stale_worker", "claim_consumed", "bad_round"):
                finalize_worker_failure(ctx, args, job_id, index, [exc.code])
            return {"ok": False, "error": exc.code, "job_id": job_id, "round": index}
        except Exception as exc:  # defensive: a worker crash must stay visible
            wlog(log, "worker_crash", detail=str(exc)[:400])
            finalize_worker_failure(ctx, args, job_id, index, ["worker_exception"])
            return {"ok": False, "error": "worker_exception", "job_id": job_id, "round": index}


def guarded_round(job, index, args):
    """The round record, only if this worker still owns the current round.

    Guards every worker write: a stale worker (superseded round, recycled token
    or replayed claim) must never overwrite a newer round's state.
    """
    rounds = job.get("rounds") or []
    if job.get("current_round") != index or not (0 <= index < len(rounds)):
        raise CliError("stale_worker", "round %s is not the current round" % index)
    record = rounds[index]
    if record.get("run_token") != args.run_token or record.get("launch_claim") != args.claim:
        raise CliError("stale_worker", "run token or claim does not match round %s" % index)
    return record


def finalize_worker_failure(ctx, args, job_id, index, reasons):
    """Record an unrecoverable worker problem as needs_attention with evidence."""
    try:
        with StateLock(ctx.state_dir):
            job = ctx.load(job_id)
            record = guarded_round(job, index, args)
            if record.get("finalized") or job.get("phase") in TERMINAL_DECISION_PHASES or job.get("stop_requested"):
                return
            record["status"] = "failed"
            record["finished_at"] = iso()
            record["finalized"] = True
            record["verification"] = {"ok": False, "reasons": reasons, "needs_attention": True}
            job["phase"] = PHASE_NEEDS_ATTENTION
            job["process_state"] = "exited"
            job["attention"] = {"reason": ",".join(reasons), "at": iso()}
            ctx.save(job)
    except (CliError, OSError, ValueError):
        pass


def worker_run(ctx, args, job_id, index, job_root, log):
    if ctx.load(job_id).get("transport") == "sdk":
        from claude_sdk_backend import run_worker
        return run_worker(ctx, args, log)
    token = args.run_token
    wlog(log, "worker_boot", job=job_id, round=index, pid=os.getpid())

    # This lock is still held by the launch transaction that spawned us, so we
    # block here until our identity has been committed.
    self_identity = capture_identity(os.getpid(), token, settle_seconds=1.0)
    with StateLock(ctx.state_dir):
        job = ctx.load(job_id)
        record = guarded_round(job, index, args)
        if record.get("claim_consumed"):
            raise CliError("claim_consumed", "round %s was already launched" % index)
        if bool(args.resume) != (record.get("kind") == "revision"):
            raise CliError("bad_round", "--resume does not match the recorded round kind")
        if job.get("stop_requested") or job.get("phase") in TERMINAL_DECISION_PHASES:
            wlog(log, "aborted_before_launch", phase=job.get("phase"))
            return {"ok": False, "error": "stopped_before_launch", "job_id": job_id, "round": index}
        record["claim_consumed"] = True
        record["worker"] = self_identity
        record["status"] = "running"
        job["phase"] = PHASE_RUNNING
        job["process_state"] = "supervised"
        ctx.save(job)

    resume = bool(args.resume)
    prompt_file = os.path.join(job_root, record["prompt"])
    stdout_path = os.path.join(job_root, record["evidence"]["stdout"])
    stderr_path = os.path.join(job_root, record["evidence"]["stderr"])
    timeout = validate_timeout(job.get("timeout", DEFAULT_TIMEOUT))
    # Provider-neutral dispatch: the adapter owns invocation preparation and
    # native verification; the lifecycle below has no provider branches.
    transport = delegate_transport.for_job(job)
    argv, env, prompt_file, monitor = transport.prepare(
        ctx.state_dir, job, record, resume, prompt_file, round_dir(job_root, index))

    wlog(
        log,
        "launch",
        resume=resume,
        model=job.get("model", MODEL),
        effort=job.get("effort", EFFORT),
        timeout=timeout,
        allow_tools=len(job.get("allow_tools") or []),
        session=job["session_id"],
    )

    # The prompt reaches Claude on stdin straight from the saved file: no shell,
    # no interpolation.  Claude's own stdio lands in evidence files only.
    started = now()
    with open(prompt_file, "rb") as prompt_in, open(stdout_path, "ab", 0) as out, open(stderr_path, "ab", 0) as err:
        for handle in (out, err):
            try:
                os.fchmod(handle.fileno(), FILE_MODE)
            except OSError:
                pass
        # Keep stop and child registration mutually exclusive. If this worker
        # crashes after spawn but before saving, the pending flag stays locked.
        with StateLock(ctx.state_dir):
            stored = ctx.load(job_id)
            stored_record = guarded_round(stored, index, args)
            if stored.get("stop_requested"):
                stored_record["claude_launch_pending"] = False
                ctx.save(stored)
                return {"ok": False, "error": "stopped_before_launch"}
            # Recheck at the actual launch boundary; a different active job may
            # have reached the threshold since start/revise reserved us. The
            # adapter decides whether any gate applies (Claude quota/inputs).
            try:
                transport.launch_checks(ctx, stored, stored_record)
            except CliError as exc:
                stored_record.update(status="not_started", finalized=True, exit_code=None,
                                     claude_launch_pending=False, finished_at=iso())
                stored["phase"] = PHASE_NEEDS_ATTENTION
                stored["process_state"] = "exited"
                stored["attention"] = dict(reason=exc.code, detail=exc.message, at=iso())
                ctx.save(stored)
                return {"ok": False, "error": exc.code, "job_id": job_id, "round": index}
            transport.launch_stamp(job, stored, stored_record)
            stored_record["claude_launch_pending"] = True
            ctx.save(stored)
            try:
                proc = subprocess.Popen(
                    argv, stdin=prompt_in, stdout=out, stderr=err,
                    cwd=job["cwd"], env=env,
                    start_new_session=True, close_fds=True,
                )
            except OSError:
                stored_record["claude_launch_pending"] = False
                ctx.save(stored)
                raise
            claude_identity = transport.capture_child(job, proc.pid, token)
            stored_record["claude"] = claude_identity
            stored_record["claude_launch_pending"] = False
            ctx.save(stored)
    wlog(log, "claude_started", pid=proc.pid, identity_verified=bool(claude_identity.get("identity_verified")))

    # No lock is held across the run itself. Wait/timeout/termination is a
    # reusable process action; only the metadata logger is bound here.
    exit_code, timed_out = delegate_process.wait_with_timeout(
        proc, timeout, monitor, claude_identity,
        on_event=lambda name, **fields: wlog(log, name, **fields))
    duration = round(now() - started, 3)
    wlog(log, "claude_exited", exit_code=exit_code, duration_s=duration, timed_out=timed_out)

    verification = transport.verify(job, record, stdout_path, exit_code if exit_code is not None else -1)
    if timed_out:
        verification["ok"] = False
        verification["needs_attention"] = False
        if "timeout" not in verification["reasons"]:
            verification["reasons"].insert(0, "timeout")

    stdout_seal = sha256_file(stdout_path)
    with StateLock(ctx.state_dir):
        job = ctx.load(job_id)
        record = guarded_round(job, index, args)
        # Provider-neutral completion: the adapter verified; this only applies
        # the completion state. Acceptance remains a separate Codex act.
        delegate_completion.complete_round(
            job, record, verification,
            exit_code=exit_code, duration=duration, stdout_seal=stdout_seal,
            timed_out=timed_out, native_session=transport.native_session, at=iso())
        ctx.save(job)

    wlog(log, "finalized", phase=job["phase"], ok=verification["ok"], reasons=verification["reasons"][:6])
    return {"ok": verification["ok"], "job_id": job_id, "round": index, "phase": job["phase"]}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser():
    parser = argparse.ArgumentParser(
        prog="delegate.py",
        description="Delegate rounds to Claude Code, Kimi Code, OpenCode or Pi (Codex plans and reviews).",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="state root (default ${CODEX_HOME:-~/.codex}/claude-delegate)",
    )
    parser.add_argument("--owner", default=None, help="owning Codex task id (default $CODEX_THREAD_ID)")
    parser.add_argument("--claude-bin", default=None, help="claude executable (default: which('claude'))")

    sub = parser.add_subparsers(dest="command")
    sub.required = True

    start = sub.add_parser("start", help="start a new delegated job")
    start.add_argument("--backend", choices=("claude", "kimi", "opencode", "pi", "codex"), default="claude")
    start.add_argument("--transport", choices=("cli", "sdk"), default="cli", help="Claude transport; MCP uses sdk")
    start.add_argument("--opencode-bin", default=None, help="OpenCode executable")
    start.add_argument("--opencode-tool", action="append", default=[], choices=OPENCODE_TOOLS + tuple(OPENCODE_ALIASES))
    start.add_argument("--kimi-bin", default=None, help="Kimi executable (default: which('kimi'))")
    start.add_argument("--kimi-tool", action="append", default=[], choices=KIMI_TOOLS,
                       help="Kimi tool allowlist, repeatable; default Read/ReadMediaFile/Glob/Grep; Bash enables unrestricted shell capability")
    start.add_argument("--pi-bin", default=None, help="Pi coding-agent executable (default: which('pi'))")
    start.add_argument("--pi-tool", action="append", default=[], choices=PI_TOOLS,
                       help="Pi built-in tool allowlist, repeatable; default read/grep/find/ls; shell tools are unrestricted")
    start.add_argument("--codex-bin", default=None,
                       help="Codex executable (default: the ChatGPT app's bundled codex, else which('codex'))")
    start.add_argument("--codex-tool", action="append", default=[], choices=CODEX_TOOLS,
                       help="Codex sandbox selection, repeatable; default read (read-only); write = workspace-write; network requires write")
    start.add_argument("--cwd", required=True, help="absolute work directory")
    start.add_argument("--prompt-file", required=True, help="file containing the task text")
    start.add_argument("--allow-tool", action="append", default=[], help="extra narrow permission rule, repeatable")
    start.add_argument("--read-dir", action="append", default=[], help="additional authorized reference directory, repeatable")
    start.add_argument("--require-file", action="append", default=[], help="required input file to check before launch, repeatable")
    start.add_argument("--timeout", default=DEFAULT_TIMEOUT, help="explicit per-round seconds or unlimited (default: unlimited)")
    start.add_argument("--max-revisions", default=DEFAULT_MAX_REVISIONS, help="correction calls beyond the first; default unlimited")

    revise = sub.add_parser("revise", help="send a correction into the same saved Claude session")
    revise.add_argument("job")
    revise.add_argument("--prompt-file", required=True)
    revise.add_argument("--timeout", default="inherit", help="keep existing limit unless set to seconds or unlimited")
    revise.add_argument("--recover", action="store_true", help="required for interrupted/failed/stopped jobs")
    revise.add_argument("--max-revisions", default=None, help="change this job's cap, including unlimited for legacy jobs")
    revise.add_argument("--allow-tool", action="append", default=[], help="add an authorized command rule to this session")
    revise.add_argument("--expected-round", type=int, default=None, help="guard the round being continued")
    revise.add_argument("--read-dir", action="append", default=[], help="add an authorized reference directory")
    revise.add_argument("--require-file", action="append", default=[], help="add a required input file")

    status = sub.add_parser("status", help="compact job status")
    status.add_argument("job")

    wait = sub.add_parser("wait", help="bounded read-only wait, then print status")
    wait.add_argument("job")
    wait.add_argument("--seconds", default=30, help="bounded wait, capped at %d" % WAIT_CAP_SECONDS)

    event_wait = sub.add_parser("await-event", help="quiet wait for an incident, 10/15-minute check, or completion")
    event_wait.add_argument("job")
    event_wait.add_argument("--after", default="-1:0", help="last returned round:sequence cursor")


    sub.add_parser("capabilities", help="show supported tool choices and runtime dependencies; no model call")
    sub.add_parser("list", help="list this owner's jobs")
    sub.add_parser("quota", help="read current Claude quota from native local records; no model call")

    stop = sub.add_parser("stop", help="terminate this run's verified processes, keep evidence")
    stop.add_argument("job")

    revalidate = sub.add_parser("revalidate", help="recheck a finalized CLI-placeholder verification failure; no model call")
    revalidate.add_argument("job")
    revalidate.add_argument("--expected-round", required=True, type=int)
    revalidate.add_argument("--expected-stream-sha256", required=True)

    accept = sub.add_parser("accept", help="record Codex's independent review and close the job")
    accept.add_argument("job")
    accept.add_argument("--notes-file", required=True)
    accept.add_argument("--evidence-dir", help="verify this round's exported original responses before acceptance")

    evidence = sub.add_parser("evidence", help="export or verify visible model responses and provenance")
    evidence.add_argument("job", nargs="?")
    evidence.add_argument("--round", type=int)
    evidence.add_argument("--output-dir", help="new local directory; parent must already exist")
    evidence.add_argument("--subject", help="exact reviewed commit or document version, declared by the caller")
    evidence.add_argument("--verify-dir", help="check a saved export without calling a model")
    evidence.add_argument("--expected-sha256", help="compare provenance.json to a digest saved at export or acceptance")

    worker = sub.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("--job", required=True)
    worker.add_argument("--round", required=True)
    worker.add_argument("--run-token", required=True)
    worker.add_argument("--claim", required=True)
    worker.add_argument("--resume", action="store_true")

    return parser


HANDLERS = {
    "capabilities": cmd_capabilities,
    "start": cmd_start,
    "revise": cmd_revise,
    "status": cmd_status,
    "wait": cmd_wait,
    "await-event": cmd_await_event,
    "list": cmd_list,
    "quota": cmd_quota,
    "stop": cmd_stop,
    "accept": cmd_accept,
    "revalidate": cmd_revalidate,
    "evidence": cmd_evidence,
    "_worker": cmd_worker,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    state_dir = args.state_dir or os.environ.get("CLAUDE_DELEGATE_STATE_DIR") or default_state_dir()
    owner = args.owner if args.owner is not None else os.environ.get("CODEX_THREAD_ID")

    try:
        ctx = Context(state_dir, owner=owner, claude_bin=args.claude_bin)
        payload = HANDLERS[args.command](ctx, args)
    except CliError as exc:
        body = {"ok": False, "error": exc.code, "message": exc.message}
        body.update(exc.extra)
        emit(body)
        return 1
    except KeyboardInterrupt:
        emit({"ok": False, "error": "interrupted", "message": "cancelled"})
        return 130
    except OSError as exc:
        emit({"ok": False, "error": "os_error", "message": str(exc)})
        return 1

    # For the worker, "stdout" is its own private log file, not a caller pipe.
    emit(payload)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
