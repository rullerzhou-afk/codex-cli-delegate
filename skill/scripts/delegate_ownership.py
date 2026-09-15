"""Provider-independent ownership, checkout reservation, and request dedupe.

These transactions are deliberately independent of any transport or process
control: they read persisted jobs and the shared state lock only. Persisted job
shapes and the on-disk request receipt format are unchanged.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
from pathlib import Path
from delegate_core import CliError


def validate_owner(owner):
    import claude_task as ct
    if not owner or not str(owner).strip():
        raise CliError("no_owner", "an owner is required: pass --owner or set CODEX_THREAD_ID")
    owner = str(owner).strip()
    if not ct.OWNER_RE.match(owner):
        raise CliError("bad_owner", "owner must be 1-200 chars of [A-Za-z0-9._:@+-]")
    return owner


def reservation_key(cwd):
    """Identify the checkout a job will write to.

    Git worktrees each report their own toplevel, so distinct worktrees of one
    repository may run concurrently while two subdirectories of the same
    checkout collide.  Non-Git directories fall back to path containment.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0:
        top = proc.stdout.decode("utf-8", "replace").strip()
        if top:
            return "git:" + os.path.realpath(top)
    return "path:" + cwd


def keys_conflict(a, b):
    pa, pb = a.partition(":")[2], b.partition(":")[2]
    return pa == pb or pa.startswith(pb.rstrip(os.sep) + os.sep) or pb.startswith(pa.rstrip(os.sep) + os.sep)


def find_conflict(ctx, key, ignore_job_id=None):
    import claude_task as ct
    for job in ctx.all_jobs():
        if job.get("job_id") == ignore_job_id:
            continue
        if job.get("phase") not in ct.RESERVING_PHASES:
            continue
        other = job.get("reservation_key")
        if other and keys_conflict(key, other):
            return job
    return None


def request_key(owner, request_id):
    return hashlib.sha256((owner + "\0" + request_id).encode()).hexdigest()


def request_digest(operation, spec):
    return hashlib.sha256(json.dumps([operation, spec], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def dispatch_request(ctx, owner, request_id, operation, spec, callback, compact, read):
    """Durable replay detection, including crash after job commit / before reply.

    Intent is stored before dispatch; the exact receipt also lives in the
    committed round. A crashed request never silently starts a second run.
    """
    import claude_task as ct
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
        raise CliError("bad_request_id", "provide a stable request_id of 1-200 characters")
    key = request_key(owner, request_id)
    digest = request_digest(operation, spec)
    request = {"key": key, "sha256": digest, "operation": operation}
    directory = Path(ct.ensure_dir(os.path.join(ctx.state_dir, "requests")))
    fd = os.open(str(directory / (key + ".lock")), os.O_CREAT | os.O_RDWR, ct.FILE_MODE)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = directory / (key + ".json")
        if receipt.exists() and ct.read_json(str(receipt))["sha256"] != digest:
            raise CliError("request_conflict", "request_id was already used with different inputs")
        with ct.StateLock(ctx.state_dir):
            for job in ctx.all_jobs():
                if job.get("owner") != owner:
                    continue
                for record in job.get("rounds") or []:
                    existing = record.get("request") or {}
                    if existing.get("key") == key:
                        if existing.get("sha256") != digest:
                            raise CliError("request_conflict", "request_id was already used with different inputs")
                        job, _ = ct.reconcile(ctx, job)
                        return compact(ctx, job, replay=True)
        ct.write_json(str(receipt), request)
        result = callback(ctx, request, directory / (key + ".md"))
        return read(owner, result["job_id"])
