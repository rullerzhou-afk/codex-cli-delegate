"""Transport adapter registry: the seam between orchestration and providers.

Orchestration resolves exactly one adapter per job backend. The adapter owns
provider-specific start/revision preparation, invocation preparation, native
evidence parsing, and model/effort verification. Provider start/revision
preparation and CLI worker dispatch are adapter-driven.

One Claude Agent SDK compatibility branch remains outside this seam:
`revise_transaction` and `cmd_revise`/`cmd_accept` handle the idle SDK
connection refresh (`sdk_scope_changed` / `claude_sdk_backend.close_idle`)
directly. That is a disclosed remaining refactor, not adapter-routed behavior.

A new adapter (including a test fake) is added by implementing `Transport` and
calling `register(...)` for start/revision/CLI-worker behavior. The public
CLI/MCP backend allowlist is separate and still permits only the three supported
providers.

Adapters are thin and deliberately call back into `claude_task` and the
provider modules through module attributes, so existing white-box patch points
(e.g. `claude_task.verify_round`, `claude_task.build_claude_argv`) keep working.
That callback is an intentional compatibility seam, not a dependency inversion.
"""
from __future__ import annotations

import os
from typing import NamedTuple

from delegate_core import CliError


class PreparedRound(NamedTuple):
    """Everything the provider-independent lifecycle needs to launch one round."""

    argv: list
    env: dict
    prompt_file: str
    monitor: object


class Transport:
    """Provider adapter interface used by the provider-independent lifecycle.

    Subclasses override the hooks that differ by provider. The default
    implementations are safe no-ops so a minimal fake adapter only needs
    ``prepare``, ``verify`` and (for start) ``new_session_id``.
    """

    name = "?"
    #: True when a round establishes its session id from native evidence.
    native_session = False

    def new_session_id(self):
        """Session id allocated for a new job, or None when native evidence does."""
        return None

    def transport_preflight(self, transport):
        """Validate the requested CLI/SDK transport for this backend."""
        if transport == "sdk":
            raise CliError("wrong_backend", "SDK transport currently applies to Claude")

    def start_config(self, ctx, args, prompt, read_dirs, required_files):
        """Validate start options; return ``(job_fields, result_extra, session_id)``.

        ``job_fields`` merge into the new job; ``result_extra`` merge into the
        start JSON result. Raise CliError to refuse.
        """
        if getattr(args, "opencode_bin", None) or getattr(args, "opencode_tool", None):
            raise CliError("wrong_backend", "OpenCode options require --backend opencode")
        if read_dirs or required_files:
            raise CliError("wrong_backend", "--read-dir/--require-file currently apply to Claude")
        if getattr(args, "kimi_bin", None) or getattr(args, "kimi_tool", None):
            raise CliError("wrong_backend", "Kimi options require --backend kimi")
        return {}, {}, self.new_session_id()

    def revision_additions(self, additions):
        """Validate added permission rules for a revision."""
        if additions:
            raise CliError("wrong_backend", "Claude permission rules require the Claude backend")

    def revision_prompt_check(self, prompt):
        """Provider-specific size/shape check for a revision prompt."""
        return None

    def revision_config(self, ctx, job, read_dirs, required_files):
        """Validate/prepare a revision; return job fields to update."""
        if read_dirs or required_files:
            raise CliError("wrong_backend", "--read-dir/--require-file currently apply to Claude")
        return {}

    def baseline(self, job):
        """Return ``(prior_assistant_uuids, baseline_status)`` for a revision."""
        return [], "empty"

    def initial_baseline(self, session_id, cwd):
        """Return the baseline recorded for round 0 at start time."""
        return [], "empty"

    def prepare(self, state_dir, job, record, resume, prompt_file, directory):
        raise NotImplementedError

    def launch_checks(self, ctx, job, record):
        """Provider-specific pre-launch gate; raise CliError to refuse."""
        return None

    def launch_stamp(self, job, stored, stored_record):
        """Persist provider-specific fields immediately before spawning."""
        return None

    def capture_child(self, job, pid, token):
        from claude_task import capture_identity
        return capture_identity(pid, token)

    def verify(self, job, record, stdout_path, exit_code):
        raise NotImplementedError


class ClaudeTransport(Transport):
    """Claude Code CLI adapter (the SDK transport has its own worker)."""

    name = "claude"
    native_session = False

    def new_session_id(self):
        import uuid
        return str(uuid.uuid4())

    def transport_preflight(self, transport):
        if transport == "sdk":
            from claude_sdk_backend import preflight
            preflight()

    def start_config(self, ctx, args, prompt, read_dirs, required_files):
        import claude_task as ct
        if getattr(args, "opencode_bin", None) or getattr(args, "opencode_tool", None):
            raise CliError("wrong_backend", "OpenCode options require --backend opencode")
        if getattr(args, "kimi_bin", None) or getattr(args, "kimi_tool", None):
            raise CliError("wrong_backend", "Kimi options require --backend kimi")
        from claude_quota import account_home
        claude_bin = ct.resolve_claude_bin(ctx.claude_bin_raw)
        quota = ct.quota_gate(ctx, account_home(), refresh=True)
        fields = dict(claude_bin=claude_bin, model=ct.MODEL, effort=ct.EFFORT,
                      claude_config_dir=account_home(),
                      claude_config_env=os.environ.get("CLAUDE_CONFIG_DIR"))
        return fields, {"quota": quota}, self.new_session_id()

    def revision_additions(self, additions):
        # Claude's restricted settings accept narrow command rules.
        return None

    def revision_config(self, ctx, job, read_dirs, required_files):
        import claude_task as ct
        ct.quota_gate(ctx, job.get("claude_config_dir"), refresh=True)
        return {}

    def baseline(self, job):
        import claude_task as ct
        return ct.snapshot_assistant_baseline(job["session_id"], job["cwd"], job.get("claude_config_dir"))

    def initial_baseline(self, session_id, cwd):
        import claude_task as ct
        return ct.snapshot_assistant_baseline(session_id, cwd)

    def prepare(self, state_dir, job, record, resume, prompt_file, directory):
        import claude_task as ct
        from claude_events import Monitor, hook_settings
        from claude_quota import Observer, account_home
        settings_path = os.path.join(directory, "hook-settings.json")
        ct.write_json(settings_path, hook_settings(
            os.path.join(os.path.dirname(os.path.realpath(ct.__file__)), "claude_events.py"),
            state_dir, job, record))
        argv = ct.build_claude_argv(job, record["run_token"], resume=resume) + ["--settings", settings_path]
        monitor = Monitor(directory, job["session_id"], record["started_epoch"], ct.write_json,
                          quota_observer=Observer(state_dir, job.get("claude_config_dir") or account_home(),
                                                  job["session_id"]))
        env = ct.job_environment(job, ct.child_env())
        return PreparedRound(argv=argv, env=env, prompt_file=prompt_file, monitor=monitor)

    def launch_checks(self, ctx, job, record):
        import claude_task as ct
        ct.quota_gate(ctx, job.get("claude_config_dir"))
        ct.read_access(job["cwd"], job.get("read_dirs"),
                       [v["path"] for v in job.get("required_files") or []])

    def verify(self, job, record, stdout_path, exit_code):
        import claude_task as ct
        return ct.verify_round(job, stdout_path, exit_code,
                               record.get("prior_assistant_uuids") or [],
                               record.get("baseline_status") or "unknown")


class KimiTransport(Transport):
    name = "kimi"
    native_session = True

    def start_config(self, ctx, args, prompt, read_dirs, required_files):
        if getattr(args, "opencode_bin", None) or getattr(args, "opencode_tool", None):
            raise CliError("wrong_backend", "OpenCode options require --backend opencode")
        if read_dirs or required_files:
            raise CliError("wrong_backend", "--read-dir/--require-file currently apply to Claude")
        if os.path.getsize(prompt) > 64 * 1024:
            raise CliError("kimi_prompt_size", "Kimi prompt must be at most 64 KiB (CLI argument limit)")
        import kimi_backend
        fields = kimi_backend.prepare(args.kimi_bin, args.kimi_tool, args.allow_tool)
        fields.update(model=kimi_backend.MODEL, effort=kimi_backend.EFFORT, claude_bin=None,
                      claude_config_dir=None, claude_config_env=None)
        return fields, {}, None

    def revision_prompt_check(self, prompt):
        if os.path.getsize(prompt) > 64 * 1024:
            raise CliError("kimi_prompt_size", "Kimi prompt must be at most 64 KiB (CLI argument limit)")

    def revision_config(self, ctx, job, read_dirs, required_files):
        if read_dirs or required_files:
            raise CliError("wrong_backend", "--read-dir/--require-file currently apply to Claude")
        import kimi_backend
        kimi_backend.require_hooks(job["kimi_home"])
        return {"kimi_hooks": True}

    def baseline(self, job):
        import kimi_backend
        return kimi_backend.baseline(job)

    def prepare(self, state_dir, job, record, resume, prompt_file, directory):
        import claude_task as ct
        import kimi_backend
        from kimi_hooks import ROUTE_ENV, route
        argv = kimi_backend.argv(job, record, resume, prompt_file, directory)
        monitor = kimi_backend.KimiMonitor(directory, job, record, ct.write_json)
        env = dict(os.environ, KIMI_CODE_HOME=job["kimi_home"])
        env[ROUTE_ENV] = route(state_dir, job, record)
        return PreparedRound(argv=argv, env=env, prompt_file=prompt_file, monitor=monitor)

    def capture_child(self, job, pid, token):
        import kimi_backend
        return kimi_backend.capture_identity(pid, token, job["kimi_bin"])

    def verify(self, job, record, stdout_path, exit_code):
        import kimi_backend
        return kimi_backend.verify(job, record, stdout_path, exit_code)


class OpenCodeTransport(Transport):
    name = "opencode"
    native_session = True

    def start_config(self, ctx, args, prompt, read_dirs, required_files):
        if read_dirs or required_files:
            raise CliError("wrong_backend", "--read-dir/--require-file currently apply to Claude")
        if getattr(args, "kimi_bin", None) or getattr(args, "kimi_tool", None):
            raise CliError("wrong_backend", "Kimi options require --backend kimi")
        import opencode_backend
        fields = opencode_backend.prepare(args.opencode_bin, args.opencode_tool, args.allow_tool)
        fields.update(model=opencode_backend.MODEL, effort=opencode_backend.EFFORT, claude_bin=None,
                      claude_config_dir=None, claude_config_env=None)
        return fields, {}, None

    def baseline(self, job):
        import opencode_backend
        return opencode_backend.baseline(job)

    def prepare(self, state_dir, job, record, resume, prompt_file, directory):
        import claude_task as ct
        import opencode_backend
        argv, env, prompt_file = opencode_backend.setup(job, record, resume, prompt_file, directory)
        monitor = opencode_backend.OpenCodeMonitor(directory, job, record, ct.write_json)
        return PreparedRound(argv=argv, env=env, prompt_file=prompt_file, monitor=monitor)

    def launch_stamp(self, job, stored, stored_record):
        stored["opencode_version"] = job["opencode_version"]
        stored["opencode_compatibility"] = job["opencode_compatibility"]
        stored_record["opencode_version"] = job["opencode_version"]

    def capture_child(self, job, pid, token):
        import opencode_backend
        return opencode_backend.capture_identity(pid, token, job["opencode_bin"])

    def verify(self, job, record, stdout_path, exit_code):
        import opencode_backend
        return opencode_backend.verify(job, record, stdout_path, exit_code)


_REGISTRY = {}


def register(transport):
    """Register an adapter by name; returns it for convenience."""
    _REGISTRY[transport.name] = transport
    return transport


def names():
    return sorted(_REGISTRY)


def resolve(name):
    try:
        return _REGISTRY[name]
    except KeyError:
        raise CliError("unsupported_backend",
                       "no transport adapter for backend %r" % (name,), backend=name)


def for_job(job):
    return resolve(job.get("backend", "claude"))


def for_name(name):
    return resolve(name)


register(ClaudeTransport())
register(KimiTransport())
register(OpenCodeTransport())
