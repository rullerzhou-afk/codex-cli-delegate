"""Live-process blockers and conservative crash reconciliation.

Provider-neutral and persistence-light: these functions only read the persisted
job/round shape, classify recorded processes, and (for reconciliation) apply a
phase transition through the caller's ``ctx.save``. They never signal a process;
signalling lives in ``delegate_process``.

``identity_state`` is resolved through ``claude_task`` at call time so the
existing patch points (``claude_task.identity_state``) keep working.
"""
from __future__ import annotations

from delegate_job_store import current_round
from delegate_process import STATE_GONE


def _identity_state(record):
    import claude_task as ct
    return ct.identity_state(record)


def live_blockers(job, allow_idle_sdk=False):
    """Everything that must be provably gone before a new round may launch.

    A recorded process only clears if we have proof it exited.  A round that
    was launched but never registered a worker also blocks: we cannot establish
    the absence of a process that may be about to invoke the model.
    """
    import claude_task as ct
    blockers = []
    idle_sdk = (allow_idle_sdk and job.get("transport") == "sdk"
                and job.get("phase") == ct.PHASE_AWAITING_REVIEW
                and current_round(job)[1].get("finalized")
                and not job.get("sdk_closing"))
    for record in job.get("rounds") or []:
        for name in ("worker", "claude"):
            state = _identity_state(record.get(name))
            if (idle_sdk and state == "alive"
                    and record.get(name) == job.get("sdk_" + name)):
                continue
            if state not in STATE_GONE:
                blockers.append(
                    {
                        "round": record.get("round"),
                        "process": name,
                        "state": state,
                        "pid": (record.get(name) or {}).get("pid"),
                    }
                )
    index, record = current_round(job)
    if record:
        if record.get("claude") is None and record.get("claude_launch_pending"):
            blockers.append({"round": index, "process": "claude", "state": "unregistered_launch"})
        elif not record.get("finalized") and not job.get("stop_requested") and record.get("worker") is None:
            blockers.append({"round": index, "process": "worker", "state": "unregistered_launch"})
    return blockers


def reconcile(ctx, job):
    """Bring a job's phase in line with observable process reality.

    Deliberately conservative: work is never relabelled done because a
    controller vanished, and the original task is never silently replayed.
    """
    import claude_task as ct
    if job.get("phase") not in ct.BUSY_PHASES:
        return job, False

    index, record = current_round(job)
    if record.get("finalized"):
        # The worker finalised and exited between our reads.
        return job, False
    if record.get("status") == "launching" and ct.now() - float(record.get("started_epoch") or 0) < ct.LAUNCH_GRACE_SECONDS:
        # Still inside the spawn window; nothing to conclude yet.
        return job, False

    worker_state = _identity_state(record.get("worker"))
    claude_state = _identity_state(record.get("claude"))

    if worker_state not in STATE_GONE:
        # Includes "mismatch"/"unverifiable": a pid is occupied but we cannot
        # prove it is ours.  Report it; never declare the run dead, never signal.
        job["process_state"] = "supervised" if worker_state == "alive" else "unverified"
        return job, False

    if claude_state not in STATE_GONE or record.get("claude_launch_pending"):
        job["phase"] = ct.PHASE_NEEDS_ATTENTION
        job["process_state"] = "orphaned"
        job["attention"] = {
            "reason": "worker_gone_claude_%s" % (claude_state if claude_state != "unknown" else "unregistered"),
            "detail": "supervisor exited while a Claude process may still be running; "
            "inspect evidence or stop the job, do not start duplicate work",
            "at": ct.iso(),
        }
        ctx.save(job)
        return job, True

    if worker_state == "unknown":
        # Launched (the round exists) but no identity was ever committed.
        job["phase"] = ct.PHASE_NEEDS_ATTENTION
        job["process_state"] = "unregistered"
        job["attention"] = {
            "reason": "launch_unregistered",
            "detail": "the launch transaction did not commit a worker identity; "
            "stop the job to fence it before recovering",
            "at": ct.iso(),
        }
        ctx.save(job)
        return job, True

    job["phase"] = ct.PHASE_INTERRUPTED
    job["process_state"] = "dead"
    job["attention"] = {
        "reason": "worker_gone_no_result",
        "detail": "no live process and no finalized round result; evidence retained, "
        "use revise --recover with a prompt written after inspection",
        "at": ct.iso(),
    }
    record["status"] = "interrupted"
    record["finished_at"] = record.get("finished_at") or ct.iso()
    ctx.save(job)
    return job, True


def stop_transition(job, remaining, signals):
    """Apply the provider-neutral stop result to ``job`` in place.

    A fully stopped job releases its reservation; an incomplete stop keeps it
    reserved. An already-terminal phase (accepted/stopped) stays terminal and
    reservation-free, so its detail must not claim a reservation it lacks.
    """
    import claude_task as ct
    index, record = current_round(job)
    if not remaining:
        if record and not record.get("finalized"):
            record["status"] = "stopped"
            record["finished_at"] = ct.iso()
            record["finalized"] = True
        if job.get("phase") != ct.PHASE_ACCEPTED:
            job["phase"] = ct.PHASE_STOPPED
        job["process_state"] = "stopped"
    else:
        if job.get("phase") not in ct.TERMINAL_DECISION_PHASES:
            job["phase"] = ct.PHASE_NEEDS_ATTENTION
        job["process_state"] = "stop_incomplete"
        reservation_held = job["phase"] in ct.RESERVING_PHASES
        job["attention"] = {
            "reason": "stop_incomplete",
            "detail": ("processes could not be confirmed stopped; reservation retained"
                       if reservation_held else
                       "processes could not be confirmed stopped; terminal job remains reservation-free"),
            "at": ct.iso(),
            "remaining": remaining,
        }
    job["stopped"] = {"at": ct.iso(), "by": job.get("owner"), "signals": signals, "complete": not remaining}


def process_view(job):
    _, record = current_round(job)
    worker = _identity_state(record.get("worker"))
    claude = _identity_state(record.get("claude"))
    if worker == "alive":
        state = "supervised"
    elif worker not in STATE_GONE:
        state = "unverified"
    elif claude not in STATE_GONE:
        state = "orphaned"
    else:
        state = "idle"
    return {"worker": worker, "claude": claude, "state": state}
