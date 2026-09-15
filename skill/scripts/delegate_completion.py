"""Provider-neutral completion and acceptance record transitions.

Completion (a round's verified native result) is never acceptance. Acceptance is
an irreversible Codex decision that releases the checkout. These functions hold
that distinction in one place so provider adapters and the lifecycle do not
re-implement it.

They mutate the passed job/round dictionaries in place. They do not call a
provider and do not supervise, signal, or re-verify OS processes; they only set
persisted state fields. ``complete_round`` sets the round's completion fields
and ``job["process_state"]``; ``apply_acceptance`` sets the accepted record,
terminal phase, and ``job["process_state"]``.

Acceptance is terminal and reservation-free. It is not necessarily the end of
the job: an explicit later ``revise`` may continue from ``accepted``, which
archives the prior acceptance record into ``acceptance_history`` and reserves
the checkout for the new round.
"""


def complete_round(job, record, verification, *, exit_code, duration, stdout_seal,
                   timed_out, native_session, at):
    """Apply one round's finalized completion state to ``job``/``record``.

    ``verification`` is the provider adapter's result: it must carry ``ok``,
    ``reasons``, ``needs_attention`` and may carry ``report``, ``cli_version``
    and ``native_session_id``. An explicit stop/accept decision outranks
    whatever the round produced.
    """
    import claude_task as ct

    record["exit_code"] = exit_code
    record["finished_at"] = at
    record["duration_s"] = duration
    record["timed_out"] = timed_out
    record["verification"] = verification
    record["evidence_sha256"] = {"stdout": stdout_seal}
    if native_session and not job.get("session_id") and verification.get("native_session_id"):
        job["session_id"] = verification["native_session_id"]
    record["status"] = "done" if verification["ok"] else ("timeout" if timed_out else "failed")
    record["finalized"] = True
    if verification.get("cli_version"):
        job["cli_version"] = verification["cli_version"]
    job["final_report"] = verification.get("report") or ""
    job["process_state"] = "exited"

    if job.get("phase") in ct.TERMINAL_DECISION_PHASES or job.get("stop_requested"):
        return
    if verification["ok"]:
        # Completion is never acceptance: Codex still has to review.
        job["phase"] = ct.PHASE_AWAITING_REVIEW
        job.pop("attention", None)
    else:
        job["phase"] = ct.PHASE_NEEDS_ATTENTION if verification["needs_attention"] else ct.PHASE_FAILED
        job["attention"] = {
            "reason": ",".join(verification["reasons"][:6]) or "unverified",
            "detail": "round %s was not verified; inspect evidence before revising" % record.get("round"),
            "at": at,
        }


def acceptance_record(job, index, notes_rel, notes_sha256, review_evidence, model, effort, at):
    """Build the irreversible acceptance record for ``job``'s current round.

    ``notes_rel`` is the repo/job-relative path of the saved review notes.
    """
    return {
        "at": at,
        "by": job.get("owner"),
        "round": index,
        "notes": notes_rel,
        "notes_sha256": notes_sha256,
        "verified_model": model,
        "verified_effort": effort,
        "review_evidence": review_evidence,
    }


def apply_acceptance(job, index, notes_rel, notes_sha256, review_evidence, model, effort, at):
    """Apply the provider-neutral acceptance transition in place.

    Verification/evidence checks and file persistence stay with the caller;
    this only sets the accepted record, the terminal accepted phase, and
    ``process_state``. Acceptance releases the checkout and is never downgraded
    by process control, but an explicit later ``revise`` may continue from it
    and archive this record into ``acceptance_history``.
    """
    import claude_task as ct
    job["phase"] = ct.PHASE_ACCEPTED
    job["accepted"] = acceptance_record(job, index, notes_rel, notes_sha256,
                                        review_evidence, model, effort, at)
    job["process_state"] = "accepted"
