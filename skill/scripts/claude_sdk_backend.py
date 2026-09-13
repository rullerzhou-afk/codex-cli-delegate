"""Pinned Claude Agent SDK adapter. One detached SDK client per delegated job.

The existing job lock, round claims, native transcript verifier and evidence
format remain authoritative. Only finalized idle SDK processes may be reused.
No model call is made while waiting for a new round in the job manifest.
"""
import asyncio
import contextlib
import importlib.metadata
import json
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace

import claude_task as ct

SDK_VERSION = "0.2.152"


def preflight():
    try:
        version = importlib.metadata.version("claude-agent-sdk")
    except importlib.metadata.PackageNotFoundError:
        raise ct.CliError("sdk_missing", "use the Python environment containing skill/requirements.txt")
    if version != SDK_VERSION:
        raise ct.CliError("sdk_version", "SDK transport requires the tested pinned version", required=SDK_VERSION, actual=version)


def reuse_worker(ctx, job, record):
    worker = job.get("sdk_worker")
    if ct.identity_state(worker) in ct.STATE_GONE:
        return False
    if (ct.identity_state(worker) != "alive"
            or ct.identity_state(job.get("sdk_claude")) != "alive"
            or job.get("sdk_closing")):
        raise ct.CliError("sdk_unavailable", "SDK process identity is uncertain; inspect or stop before recovery")
    record["worker"] = worker
    record["claude"] = job["sdk_claude"]
    ctx.save(job)
    return True


def close_idle(ctx, job_id, expected_round=None):
    """Fence revisions before tearing down a reviewed SDK session for accept."""
    with ct.StateLock(ctx.state_dir):
        job = ctx.load_owned(job_id)
        if job.get("transport") != "sdk":
            return
        if expected_round is not None and expected_round != job.get("current_round"):
            raise ct.CliError("stale_round", "review refers to an older round")
        if job["phase"] != ct.PHASE_AWAITING_REVIEW:
            raise ct.CliError("bad_phase", "SDK acceptance requires a reviewed idle round")
        job["sdk_closing"] = True
        ctx.save(job)
        worker = job.get("sdk_worker")
    # The worker observes the fence and closes the SDK itself, flushing history.
    deadline = time.monotonic() + 8
    while ct.identity_state(worker) == "alive" and time.monotonic() < deadline:
        time.sleep(0.1)
    if ct.identity_state(worker) == "alive":
        ct.terminate_recorded(worker)
    with ct.StateLock(ctx.state_dir):
        job = ctx.load_owned(job_id)
        remaining = ct.live_blockers(job)
        if remaining:
            raise ct.CliError("sdk_close_incomplete", "SDK processes have not been confirmed stopped", blockers=remaining)


class SDKWorker:
    def __init__(self, ctx, args, log):
        self.ctx, self.args, self.log = ctx, args, log
        self.job = ctx.load(args.job)
        self.root = Path(ctx.job_dir(args.job))
        self.active = None
        self.monitor = None
        self.raw = None
        self.stopping = False
        self.client = None
        self.transport = None
        self.handled = -1

    def append(self, path, payload):
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, ct.FILE_MODE)
        with os.fdopen(fd, "a", encoding="utf-8") as out:
            out.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def tap(self, row):
        if self.raw:
            self.append(self.raw, row)
            if self.monitor:
                self.monitor.tick()
        else:
            self.append(self.root / "sdk-idle.ndjson", row)
            if row.get("type") in ("assistant", "result"):
                # A sealed round must not acquire unowned follow-up work.
                with ct.StateLock(self.ctx.state_dir):
                    job = self.ctx.load(self.job["job_id"])
                    if job["phase"] == ct.PHASE_AWAITING_REVIEW:
                        job["phase"] = ct.PHASE_NEEDS_ATTENTION
                        job["attention"] = {"reason": "sdk_unexpected_idle_output", "at": ct.iso()}
                        self.ctx.save(job)
                        self.stopping = True
        if row.get("type") == "rate_limit_event":
            from claude_quota import Observer
            Observer(self.ctx.state_dir, self.job["claude_config_dir"], self.job["session_id"])(row)

    async def hook(self, payload, tool_use_id, context):
        if payload.get("hook_event_name") == "PreToolUse":
            inputs = payload.get("tool_input") or {}
            if payload.get("tool_name") == "Bash" and inputs.get("run_in_background"):
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                        "permissionDecisionReason": "Delegated SDK rounds require foreground Bash completion."}}
            return {}
        if self.active is None:
            return {}
        from claude_events import record_hook
        record_hook(self.ctx, self.job["job_id"], self.active["round"], self.active["run_token"], payload)
        return {}

    def options(self, record):
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
        from claude_events import hook_settings
        settings = hook_settings("unused", self.ctx.state_dir, self.job, record)
        settings.pop("hooks")
        settings_path = self.root / "sdk-settings.json"
        ct.write_json(str(settings_path), settings)
        guidance = ("Work within the delegated task and primary working directory. "
                    "Additional directories are read-only references. Use foreground tools; "
                    "do not leave background work behind. Read required inputs with offset/limit as needed.\n"
                    + json.dumps(self.job.get("required_files") or [], ensure_ascii=False))
        return ClaudeAgentOptions(
            cli_path=self.job["claude_bin"], cwd=self.job["cwd"],
            model=ct.MODEL, effort=ct.EFFORT,
            system_prompt={"type": "preset", "preset": "claude_code", "append": guidance},
            tools=ct.enabled_tools(self.job),
            allowed_tools=list(ct.BASE_ALLOWED_TOOLS),
            permission_mode="dontAsk", settings=str(settings_path), setting_sources=[],
            strict_mcp_config=True, add_dirs=self.job.get("read_dirs") or [],
            env=dict(ct.CHILD_ENV_OVERRIDES),
            extra_args={"restricted": None, "name": self.args.run_token},
            session_id=None if self.args.resume else self.job["session_id"],
            resume=self.job["session_id"] if self.args.resume else None,
            include_partial_messages=True,
            hooks={event: [HookMatcher(hooks=[self.hook])] for event in
                   ("PreToolUse", "Stop", "StopFailure", "PostToolUseFailure", "Notification")},
            stderr=lambda text: self.append(self.root / "sdk-stderr.ndjson", {"at": ct.iso(), "text": text}),
        )

    def claim_round(self):
        from claude_events import Monitor
        from claude_quota import Observer
        with ct.StateLock(self.ctx.state_dir):
            job = self.ctx.load(self.job["job_id"])
            if job.get("stop_requested") or job.get("sdk_closing") or job["phase"] in ct.TERMINAL_DECISION_PHASES:
                self.stopping = True
                return False
            index, record = ct.current_round(job)
            if index <= self.handled:
                return False
            if self.handled == -1:
                ct.guarded_round(job, int(self.args.round), self.args)
                if bool(self.args.resume) != (record.get("kind") == "revision"):
                    raise ct.CliError("bad_round", "resume does not match the recorded round kind")
            if (job.get("sdk_worker") or {}).get("pid") != os.getpid():
                raise ct.CliError("stale_worker", "SDK worker PID does not own this job")
            if record.get("worker") != job.get("sdk_worker") or record.get("claim_consumed"):
                raise ct.CliError("stale_worker", "SDK round claim is not available")
            try:
                ct.quota_gate(self.ctx, job["claude_config_dir"])
                ct.read_access(job["cwd"], job.get("read_dirs"), [v["path"] for v in job.get("required_files") or []])
            except ct.CliError as exc:
                record.update(claim_consumed=True, finalized=True, status="not_started", finished_at=ct.iso())
                job["phase"] = ct.PHASE_NEEDS_ATTENTION
                job["attention"] = {"reason": exc.code, "detail": exc.message, "at": ct.iso()}
                self.ctx.save(job)
                self.stopping = True
                return False
            record["claim_consumed"] = True
            record["status"] = "running"
            job["phase"] = ct.PHASE_RUNNING
            if job.get("sdk_claude"):
                record["claude"] = job["sdk_claude"]
            self.ctx.save(job)
            self.job, self.active = job, record
            self.handled = index
        rdir = Path(ct.ensure_dir(ct.round_dir(str(self.root), index)))
        self.raw = self.root / record["evidence"]["stdout"]
        self.raw.touch(mode=ct.FILE_MODE)
        (self.root / record["evidence"]["stderr"]).touch(mode=ct.FILE_MODE)
        self.monitor = Monitor(str(rdir), job["session_id"], record["started_epoch"], ct.write_json,
                               quota_observer=Observer(self.ctx.state_dir, job["claude_config_dir"], job["session_id"]))
        return True

    def register_cli(self, transport):
        # This is the sole pinned use of the SDK transport's process handle.
        identity = ct.capture_identity(transport._process.pid, self.args.run_token)
        if not identity["identity_verified"]:
            raise ct.CliError("sdk_identity", "could not verify the SDK CLI child identity")
        with ct.StateLock(self.ctx.state_dir):
            job = self.ctx.load(self.job["job_id"])
            job["sdk_claude"] = identity
            job["rounds"][self.active["round"]]["claude"] = identity
            job["rounds"][self.active["round"]]["claude_launch_pending"] = False
            self.ctx.save(job)

    def finalize(self, failure=None):
        record = self.active
        self.monitor.tick(complete=True)
        verification = ct.verify_round(self.job, str(self.raw), None, record["prior_assistant_uuids"],
                                       record["baseline_status"], sdk_boundary=failure is None)
        if failure:
            verification.update(ok=False, needs_attention=True)
            verification["reasons"].insert(0, failure)
        with ct.StateLock(self.ctx.state_dir):
            job = self.ctx.load(self.job["job_id"])
            stored = job["rounds"][record["round"]]
            stored.update(finalized=True, status="done" if verification["ok"] else "failed",
                          finished_at=ct.iso(), duration_s=round(ct.now() - record["started_epoch"], 3),
                          verification=verification, exit_code=None,
                          evidence_sha256={"stdout": ct.sha256_file(str(self.raw))})
            job["final_report"] = verification.get("report", "")
            job["cli_version"] = verification.get("cli_version")
            job["process_state"] = "sdk_idle" if verification["ok"] else "sdk_failed"
            if not job.get("stop_requested") and job["phase"] not in ct.TERMINAL_DECISION_PHASES:
                job["phase"] = ct.PHASE_AWAITING_REVIEW if verification["ok"] else ct.PHASE_NEEDS_ATTENTION
                if verification["ok"]:
                    job.pop("attention", None)
                else:
                    job["attention"] = {"reason": ",".join(verification["reasons"][:6]), "at": ct.iso()}
            self.ctx.save(job)
        self.active, self.raw, self.monitor = None, None, None
        return verification["ok"]

    async def execute_round(self):
        prompt = (self.root / self.active["prompt"]).read_text()
        # Include current policy each round, including newly added inputs. Do
        # not make Claude guess which spelling a task-scoped rule permits.
        prompt += "\n\nDelegation execution context (use these authorized command forms; do not guess alternatives):\n" + json.dumps(
            dict(cwd=self.job["cwd"], allow_tools=self.job.get("allow_tools", []),
                 required_files=self.job.get("required_files", [])), ensure_ascii=False)
        try:
            async with asyncio.timeout(self.job["timeout"]):
                await self.client.query(prompt, session_id=self.job["session_id"])
                from claude_agent_sdk import ResultMessage
                received = False
                async for message in self.client.receive_response():
                    if isinstance(message, ResultMessage):
                        received = True
                if not received:
                    raise ct.CliError("sdk_missing_result", "SDK stream ended without a result")
                # Native history may flush immediately after the result event.
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    v = ct.verify_round(self.job, str(self.raw), None, self.active["prior_assistant_uuids"],
                                        self.active["baseline_status"], sdk_boundary=True)
                    if v["ok"] or not v["needs_attention"]:
                        break
                    await asyncio.sleep(0.1)
            return self.finalize()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.append(self.root / "sdk-errors.ndjson", {"at": ct.iso(), "error": ct.redact(str(exc))})
            with contextlib.suppress(Exception):
                await self.client.interrupt()
            self.finalize("timeout" if isinstance(exc, TimeoutError) else "sdk_round_error")
            return False

    async def run(self):
        from claude_agent_sdk import ClaudeSDKClient
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
        worker = self

        class EvidenceTransport(SubprocessCLITransport):
            async def connect(self):
                # Keep the launch fence under the existing shared state lock.
                with ct.StateLock(worker.ctx.state_dir):
                    job = worker.ctx.load(worker.job["job_id"])
                    if job.get("stop_requested"):
                        raise ct.CliError("stopped_before_launch", "job was stopped")
                    record = job["rounds"][worker.active["round"]]
                    record["claude_launch_pending"] = True
                    worker.ctx.save(job)
                try:
                    await super().connect()
                except BaseException:
                    with ct.StateLock(worker.ctx.state_dir):
                        job = worker.ctx.load(worker.job["job_id"])
                        job["rounds"][worker.active["round"]]["claude_launch_pending"] = False
                        worker.ctx.save(job)
                    raise
                worker.register_cli(self)

            async def read_messages(self):
                async for row in super().read_messages():
                    worker.tap(row)
                    yield row

        if not self.claim_round():
            return
        options = self.options(self.active)
        self.transport = EvidenceTransport(prompt=self.empty_input(), options=options)
        self.client = ClaudeSDKClient(options=options, transport=self.transport)
        try:
            async with self.client:
                while not self.stopping:
                    if self.active is not None:
                        if not await self.execute_round():
                            break
                    await asyncio.sleep(0.2)
                    self.claim_round()
        except BaseException as exc:
            self.append(self.root / "sdk-errors.ndjson", {"at": ct.iso(), "type": type(exc).__name__, "error": ct.redact(str(exc))})
            if self.active is not None:
                self.finalize("sdk_interrupted" if isinstance(exc, asyncio.CancelledError) else "sdk_startup_error")
            raise
        finally:
            if self.active is not None:
                self.finalize("sdk_interrupted")

    async def empty_input(self):
        if False:
            yield {}


def run_worker(ctx, args, log):
    preflight()
    worker = SDKWorker(ctx, args, log)

    async def main():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await worker.run()
        except asyncio.CancelledError:
            pass
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)

    asyncio.run(main())
    return {"ok": True, "job_id": args.job, "sdk_worker_exited": True}
