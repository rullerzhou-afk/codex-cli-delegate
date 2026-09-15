"""Process identity, liveness, and conservative termination.

Darwin-first: a process is signalled only when its recorded start time and
unique run token still re-verify. Everything else refuses. This module holds no
job/round persistence and no provider invocation, so it can be reasoned about
independently.

The classify/terminate functions are re-exported through ``claude_task`` so the
existing public surface and patch points are unchanged.
"""
from __future__ import annotations

import errno
import os
import signal
import subprocess
import time

# Only "exited" is proof of absence.  "unknown" means nothing was ever
# recorded.  Everything else means a process may exist that we must not signal.
STATE_GONE = frozenset(("exited", "unknown"))


def _ps_field(pid, fmt):
    try:
        proc = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", fmt],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", "replace").strip()
    return text or None


def ps_probe(pid):
    """Return {'lstart':..., 'command':...} for a live pid, else None."""
    if not isinstance(pid, int) or pid <= 1:
        return None
    lstart = _ps_field(pid, "lstart=")
    if lstart is None:
        return None
    command = _ps_field(pid, "command=")
    if command is None:
        return None
    return {"lstart": " ".join(lstart.split()), "command": command}


def capture_identity(pid, token, settle_seconds=None):
    """Pin a launched process' identity, waiting briefly for exec to land.

    Between fork and exec a child may still show the parent's argv, so poll
    until the unique token appears.  If it never does, record the process as
    unverified, which permanently disqualifies it from being signalled here.
    """
    import claude_task as ct
    if settle_seconds is None:
        settle_seconds = ct.IDENTITY_SETTLE_SECONDS
    deadline = ct.now() + settle_seconds
    probe = None
    while True:
        probe = ps_probe(pid)
        if probe and token in probe["command"]:
            return {
                "pid": pid,
                "run_token": token,
                "lstart": probe["lstart"],
                "identity_verified": True,
                "recorded_at": ct.iso(),
            }
        if ct.now() >= deadline:
            break
        time.sleep(0.05)
    return {
        "pid": pid,
        "run_token": token,
        "lstart": probe["lstart"] if probe else None,
        "identity_verified": False,
        "recorded_at": ct.iso(),
    }


def pid_liveness(pid):
    """``gone`` only on ESRCH; EPERM still means the pid is occupied."""
    if not isinstance(pid, int) or pid <= 1:
        return "gone"
    try:
        os.kill(pid, 0)
        return "present"
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return "gone"
        if exc.errno == errno.EPERM:
            return "present"
        return "unknown"


def identity_state(record):
    """Classify a recorded process.

    ``alive``         identity re-verified, safe to signal
    ``mismatch``      the pid is occupied by something that is not provably ours
    ``unverifiable``  the pid is occupied but ps could not describe it
    ``exited``        proof (ESRCH) that the pid is gone
    ``unknown``       nothing was ever recorded
    """
    if not record or not isinstance(record.get("pid"), int):
        return "unknown"
    liveness = pid_liveness(record["pid"])
    if liveness == "gone":
        return "exited"
    if record.get("identity_method") == "darwin_proc":
        from kimi_backend import identity_state as kimi_identity_state
        return kimi_identity_state(record)
    probe = ps_probe(record["pid"])
    if probe is None:
        # The pid is occupied (or its state is undecidable) but ps told us
        # nothing.  A ps failure is not evidence that the process ended.
        return "unverifiable"
    if not record.get("identity_verified"):
        return "mismatch"
    token = record.get("run_token") or ""
    if token and token not in probe["command"]:
        return "mismatch"
    recorded_lstart = record.get("lstart")
    if recorded_lstart and probe["lstart"] != recorded_lstart:
        return "mismatch"
    return "alive"


def wait_with_timeout(proc, timeout, monitor, identity, on_event=None):
    """Wait for a launched process under the per-round wall-clock timeout.

    Returns ``(exit_code, timed_out)``. On timeout the recorded process is
    terminated by re-verified identity only (never an arbitrary PID).
    ``on_event(name, **fields)`` is an optional metadata-only logger.
    """
    def log(name, **fields):
        if on_event:
            on_event(name, **fields)

    timed_out = False
    try:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                exit_code = proc.wait(timeout=0.4 if deadline is None else min(0.4, max(0.01, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                if deadline is not None and time.monotonic() >= deadline:
                    raise
                monitor.tick()
    except subprocess.TimeoutExpired:
        timed_out = True
        log("timeout", seconds=timeout)
        killed = terminate_recorded(identity)
        log("timeout_signal", state=killed.get("state"), signalled=killed.get("signalled"))
        try:
            exit_code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            exit_code = None
    monitor.tick(complete=True)
    return exit_code, timed_out


def _signal_run(pid, sig):
    """Signal the pid and, if it leads its own session, its group with it."""
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
            return
    except (OSError, AttributeError):
        pass
    os.kill(pid, sig)


def _await_exit(record, seconds):
    import claude_task as ct
    deadline = ct.now() + seconds
    while ct.now() < deadline:
        if identity_state(record) != "alive":
            return True
        time.sleep(0.1)
    return identity_state(record) != "alive"


def terminate_recorded(record, grace=5.0):
    """SIGTERM (then SIGKILL) a process only if its identity still matches.

    Anything other than ``alive`` is refused: an unrelated or unverifiable pid
    is never signalled.  The reported ``state`` is always re-derived afterwards
    so a caller cannot mistake "refused" for "stopped".
    """
    state = identity_state(record)
    if state != "alive":
        return {"signalled": False, "state": state, "refused": state in ("mismatch", "unverifiable")}
    pid = record["pid"]
    sent = []
    try:
        _signal_run(pid, signal.SIGTERM)
        sent.append("TERM")
    except OSError as exc:
        return {"signalled": False, "state": identity_state(record), "errno": exc.errno}

    if not _await_exit(record, grace):
        try:
            _signal_run(pid, signal.SIGKILL)
            sent.append("KILL")
        except OSError:
            pass
        _await_exit(record, 3.0)
    return {"signalled": True, "signals": sent, "state": identity_state(record)}
