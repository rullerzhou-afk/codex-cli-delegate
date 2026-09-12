"""Small application API shared by the MCP entrypoint and integration tests."""
import asyncio
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import claude_task as ct


class DelegateService:
    def __init__(self, state_dir=None, claude_bin=None):
        self.state_dir = state_dir or ct.default_state_dir()
        self.claude_bin = claude_bin

    def context(self, owner):
        # MCP servers may be shared across Codex tasks. Never capture the task
        # that happened to launch the server as owner of all later requests.
        ct.validate_owner(owner)
        return ct.Context(self.state_dir, owner=owner, claude_bin=self.claude_bin)

    def compact(self, ctx, job, replay=False):
        index, record = ct.current_round(job)
        v = record.get("verification") or {}
        root = Path(ctx.job_dir(job["job_id"]))
        data = dict(job_id=job["job_id"], backend=job.get("backend", "claude"),
                    phase=job["phase"], round=index, session_id=job.get("session_id"),
                    transport=job.get("transport", "cli"), replayed=replay)
        if v:
            data["verified"] = {k: v.get(k) for k in
                                ("ok", "model_verified", "effort_verified", "session_ok", "completion_basis", "reasons")}
            data["summary"] = ct.clip(job.get("final_report"), 1200)
            data["permission_denials"] = v.get("permission_denials", 0)
            usage = v.get("usage") or {}
            if usage:
                data["usage"] = {k: usage[k] for k in ("input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "output_tokens") if k in usage}
        if job.get("attention"):
            data["attention"] = job["attention"]
        data["evidence"] = {"job": str(root / "job.json"), "stream": str(root / record["evidence"]["stdout"])}
        if job.get("backend", "claude") == "claude":
            from claude_quota import status
            q = status(ctx.state_dir, job.get("claude_config_dir"))
            data["quota"] = {k: q[k] for k in ("state", "blocking_windows", "action")}
        data["next_action"] = (
            "wait" if job["phase"] in ct.BUSY_PHASES else
            "independently_review_then_accept_or_revise" if job["phase"] == ct.PHASE_AWAITING_REVIEW else
            "done" if job["phase"] == ct.PHASE_ACCEPTED else "inspect_evidence_before_recovery")
        return data

    def read(self, owner, job_id, details=False):
        ctx = self.context(owner)
        with ct.StateLock(ctx.state_dir):
            job = ctx.load_owned(job_id)
            job, _ = ct.reconcile(ctx, job)
        return ct.status_payload(ctx, job) if details else self.compact(ctx, job)

    def list(self, owner):
        ctx = self.context(owner)
        with ct.StateLock(ctx.state_dir):
            jobs = [j for j in ctx.all_jobs() if j.get("owner") == owner]
        return [{k: j.get(k) for k in ("job_id", "backend", "phase", "current_round", "session_id", "cwd")}
                for j in jobs]

    def dispatch(self, owner, request_id, operation, spec, callback):
        """Durable replay detection, including crash after job commit / before reply.

        Intent is stored before dispatch; the exact receipt also lives in the
        committed round. A crashed request never silently starts a second run.
        """
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise ct.CliError("bad_request_id", "provide a stable request_id of 1–200 characters")
        ctx = self.context(owner)
        key = hashlib.sha256((owner + "\0" + request_id).encode()).hexdigest()
        digest = hashlib.sha256(json.dumps([operation, spec], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        request = {"key": key, "sha256": digest, "operation": operation}
        directory = Path(ct.ensure_dir(os.path.join(ctx.state_dir, "requests")))
        fd = os.open(str(directory / (key + ".lock")), os.O_CREAT | os.O_RDWR, ct.FILE_MODE)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            receipt = directory / (key + ".json")
            if receipt.exists() and ct.read_json(str(receipt))["sha256"] != digest:
                raise ct.CliError("request_conflict", "request_id was already used with different inputs")
            with ct.StateLock(ctx.state_dir):
                for job in ctx.all_jobs():
                    if job.get("owner") != owner:
                        continue
                    for record in job.get("rounds") or []:
                        existing = record.get("request") or {}
                        if existing.get("key") == key:
                            if existing.get("sha256") != digest:
                                raise ct.CliError("request_conflict", "request_id was already used with different inputs")
                            job, _ = ct.reconcile(ctx, job)
                            return self.compact(ctx, job, replay=True)
            ct.write_json(str(receipt), request)
            result = callback(ctx, request, directory / (key + ".md"))
            return self.read(owner, result["job_id"])

    def start(self, owner, request_id, cwd, task, backend="claude", allow_tools=None,
              read_dirs=None, required_files=None, timeout=1800, max_revisions=None,
              kimi_tools=None, opencode_tools=None):
        if backend not in ("claude", "kimi", "opencode"):
            raise ct.CliError("unsupported_backend", "supported agents: claude, kimi, opencode; Pi is not implemented")
        if not isinstance(task, str) or not task.strip() or len(task.encode()) > ct.PROMPT_MAX_BYTES:
            raise ct.CliError("bad_task", "task must contain 1–1048576 UTF-8 bytes")
        spec = dict(cwd=ct.validate_cwd(cwd), task=task, backend=backend,
                    allow_tools=allow_tools or [], read_dirs=read_dirs or [], required_files=required_files or [],
                    timeout=ct.validate_timeout(timeout), max_revisions=ct.validate_max_revisions(max_revisions),
                    kimi_tools=kimi_tools or [], opencode_tools=opencode_tools or [])

        def launch(ctx, request, prompt):
            ct.atomic_write_bytes(str(prompt), task.encode())
            args = SimpleNamespace(cwd=spec["cwd"], prompt_file=str(prompt), timeout=spec["timeout"],
                                   max_revisions=spec["max_revisions"], allow_tool=spec["allow_tools"],
                                   backend=backend, transport="sdk" if backend == "claude" else "cli",
                                   read_dir=spec["read_dirs"], require_file=spec["required_files"],
                                   kimi_bin=None, kimi_tool=spec["kimi_tools"], opencode_bin=None,
                                   opencode_tool=spec["opencode_tools"], request=request)
            return ct.cmd_start(ctx, args)

        return self.dispatch(owner, request_id, "start", spec, launch)

    def revise(self, owner, request_id, job_id, expected_round, task, recover=False):
        if not isinstance(task, str) or not task.strip() or len(task.encode()) > ct.PROMPT_MAX_BYTES:
            raise ct.CliError("bad_task", "provide a nonempty bounded correction")
        spec = dict(job_id=job_id, expected_round=expected_round, task=task, recover=recover)

        def launch(ctx, request, prompt):
            ct.atomic_write_bytes(str(prompt), task.encode())
            return ct.cmd_revise(ctx, SimpleNamespace(job=job_id, prompt_file=str(prompt), recover=recover,
                                                      expected_round=expected_round, request=request))

        return self.dispatch(owner, request_id, "revise", spec, launch)

    def stop(self, owner, job_id):
        return ct.cmd_stop(self.context(owner), SimpleNamespace(job=job_id))

    def accept(self, owner, job_id, expected_round, notes, evidence_dir=None):
        if not isinstance(notes, str) or not notes.strip() or len(notes.encode()) > ct.NOTES_MAX_BYTES:
            raise ct.CliError("bad_notes", "describe independent review and verification")
        ctx = self.context(owner)
        with ct.StateLock(ctx.state_dir):
            job = ctx.load_owned(job_id)
            if job["current_round"] != expected_round:
                raise ct.CliError("stale_round", "review refers to an older round")
            if job["phase"] == ct.PHASE_ACCEPTED:
                return self.compact(ctx, job, replay=True)
        path = Path(ctx.job_dir(job_id)) / "notes" / ("r%03d-review-input.md" % expected_round)
        ct.atomic_write_bytes(str(path), notes.encode())
        ct.cmd_accept(ctx, SimpleNamespace(job=job_id, notes_file=str(path), evidence_dir=evidence_dir,
                                          expected_round=expected_round))
        return self.read(owner, job_id)

    async def wait(self, owner, job_id, cursor="-1:0", timeout=600):
        if type(timeout) is not int or not 0 <= timeout <= 1800:
            raise ct.CliError("bad_wait", "timeout must be 0–1800 seconds")
        try:
            after_round, after_seq = map(int, cursor.split(":"))
            if after_round < -1 or after_seq < 0:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ct.CliError("bad_cursor", "cursor must be round:sequence; start with -1:0")
        ctx = self.context(owner)
        deadline = time.monotonic() + timeout
        last_reconcile = -float("inf")
        while True:
            def snapshot():
                with ct.StateLock(ctx.state_dir):
                    job = ctx.load_owned(job_id)
                    if time.monotonic() - last_reconcile >= 5:
                        job, _ = ct.reconcile(ctx, job)
                return job
            job = await asyncio.to_thread(snapshot)
            if time.monotonic() - last_reconcile >= 5:
                last_reconcile = time.monotonic()
            index, record = ct.current_round(job)
            if after_round > index:
                raise ct.CliError("bad_cursor", "cursor refers to a future round")
            try:
                monitor = ct.read_json(os.path.join(ct.round_dir(ctx.job_dir(job_id), index), "monitor.json"))
            except FileNotFoundError:
                monitor = {}
            events = monitor.get("events") or []
            seq = after_seq if after_round == index else 0
            event = None
            settled = job["phase"] not in ct.BUSY_PHASES
            if settled:
                event = {"kind": "settled"} if seq < len(events) + 1 else None
                seq = len(events) + 1
            elif len(events) > seq:
                event = events[-1]
                seq = event["seq"]
            expired = time.monotonic() >= deadline
            if settled or event or expired:
                result = self.compact(ctx, job)
                result.update(cursor="%d:%d" % (index, seq), event=event, wait_expired=expired and not settled and not event)
                return result
            await asyncio.sleep(0.4)
