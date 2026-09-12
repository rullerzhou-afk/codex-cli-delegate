#!/usr/bin/env python3
"""Owned Kimi hook registration and silent, round-bound local inbox.

Reuse Clawd's event semantics and ownership rule, without its desktop IPC.
Never parse or copy credentials, rewrite foreign hooks, or return model input.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import time

EVENTS = ('Stop', 'StopFailure', 'PostToolUseFailure')
ROUTE_ENV = 'CODEX_DELEGATE_KIMI_ROUTE'
# This backend is macOS-only. Keep the command stable when a caller uses
# another Python version to inspect or manage the same installation.
PYTHON = '/usr/bin/python3'
BEGIN = b'\n\n# BEGIN codex-delegate-kimi-hooks v1\n'
END = b'# END codex-delegate-kimi-hooks v1\n'


def command(script=None):
    target = Path(script or __file__).resolve()
    return ('if [ -n "${' + ROUTE_ENV + ':-}" ]; then '
            + shlex.join([PYTHON, str(target), 'receive']) + '; fi')


def block(script=None):
    parts = [BEGIN.decode()]
    for event in EVENTS:
        parts.append('[[hooks]]\nevent = %s\ncommand = %s\ntimeout = 5\n\n' %
                     (json.dumps(event), json.dumps(command(script))))
    return (''.join(parts) + END.decode()).encode()


def split_owned(data):
    # Fail closed on partial/duplicate markers. Bytes outside our exact block
    # stay intact, including user sections after [[hooks]] and CRLF files.
    if b'codex-delegate-kimi-hooks' not in data:
        return data, b'', b''
    if data.count(BEGIN) != 1 or data.count(END) != 1 or data.count(b'codex-delegate-kimi-hooks') != 2:
        raise ValueError('Ambiguous managed hook markers; inspect the config locally')
    start, finish = data.index(BEGIN), data.index(END) + len(END)
    if finish <= start:
        raise ValueError('Invalid managed hook block')
    return data[:start], data[start:finish], data[finish:]


def check(home):
    try:
        path = Path(home) / 'config.toml'
        if path.is_symlink():
            return False
        _, owned, _ = split_owned(path.read_bytes())
        return owned == block()
    except (OSError, ValueError):
        return False


def configure(home, action):
    if action == 'install' and not os.access(PYTHON, os.X_OK):
        raise ValueError('The macOS Python receiver runtime is unavailable')
    path = Path(home) / 'config.toml'
    if path.is_symlink() or not path.is_file():
        raise ValueError('Expected a regular existing Kimi config.toml')
    lock = path.parent / '.codex-delegate-hooks.lock'
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        before = path.read_bytes()
        prefix, owned, suffix = split_owned(before)
        expected = block()
        if owned and owned != expected:
            raise ValueError('Managed hook block was edited or moved; no config was changed')
        if not owned and re.search(rb'^\s*hooks\s*=', before, re.M):
            raise ValueError('Inline hooks array needs manual inspection; no config was changed')
        after = prefix + (expected if action == 'install' else b'') + suffix
        if before != after:
            mode = path.stat().st_mode & 0o777
            temp_fd, name = tempfile.mkstemp(prefix='.delegate-hooks-', dir=str(path.parent))
            try:
                with os.fdopen(temp_fd, 'wb') as out:
                    os.fchmod(out.fileno(), mode)
                    out.write(after)
                    out.flush()
                    os.fsync(out.fileno())
                if path.is_symlink() or path.read_bytes() != before:
                    raise ValueError('Config changed concurrently; no config was replaced')
                os.replace(name, path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        return dict(ok=True, changed=before != after, action=action,
                    before_sha256=hashlib.sha256(before).hexdigest(),
                    after_sha256=hashlib.sha256(after).hexdigest(),
                    foreign_sha256=hashlib.sha256(prefix + suffix).hexdigest(),
                    managed_hooks=len(EVENTS) if action == 'install' else 0)


def route(state_dir, job, record):
    return json.dumps(dict(state_dir=state_dir, owner=job['owner'], job=job['job_id'],
                           round=record['round'], token=record['run_token']))


def receive(raw, routing):
    import claude_task as ct
    import kimi_backend as kimi
    try:
        if not routing or len(routing) > 4096 or len(raw) > 1_048_576:
            return
        address, payload = json.loads(routing), json.loads(raw)
        if not isinstance(address, dict) or not isinstance(payload, dict):
            return
        if payload.get('agent_id') not in (None, '', 'main') or payload.get('hook_event_name') not in EVENTS:
            return
        root = Path(address['state_dir'])
        # Unrelated Kimi invocations must not create state directories.
        if not root.is_absolute() or not (root / 'jobs').is_dir():
            return
        ctx = ct.Context(str(root), owner=address['owner'])
        with ct.StateLock(ctx.state_dir, timeout=2):
            job = ctx.load_owned(address['job'])
            index, record = ct.current_round(job)
            if (job.get('backend') != 'kimi' or not job.get('kimi_hooks')
                    or type(address.get('round')) is not int or index != address['round']
                    or record.get('run_token') != address.get('token')
                    or record.get('finalized') or job.get('stop_requested')
                    or not isinstance(payload.get('cwd'), str)
                    or os.path.realpath(payload['cwd']) != job['cwd']):
                return
            native = kimi.discover(job, record)
            if native is None or payload.get('session_id') != native.name:
                return
            prompts = (r for r in kimi.rows(kimi.wire_path(native))
                       if r.get('agentId') == 'main' and r.get('type') == 'turn.prompt')
            latest = None
            for latest in prompts:
                pass
            if latest is None or not kimi.is_prompt(latest, record['run_token']):
                return
            # Error bodies can include file contents or secrets. Save only
            # event/tool identity; bounded generic messages are made by monitor.
            tool = payload.get('tool_name')
            item = dict(at=time.time(), hook=payload['hook_event_name'], session_id=native.name,
                        tool=tool if tool in kimi.TOOLS else '',
                        tool_call_id=ct.clip(payload.get('tool_call_id'), 160),
                        stop_hook_active=payload.get('stop_hook_active') is True)
            target = Path(ct.round_dir(ctx.job_dir(job['job_id']), index)) / 'hooks.ndjson'
            fd = os.open(str(target), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, 'a') as out:
                out.write(json.dumps(item) + '\n')
    except (OSError, ValueError, TypeError, KeyError, ct.CliError):
        return


def main():
    if len(sys.argv) == 2 and sys.argv[1] == 'receive':
        routing = os.environ.get(ROUTE_ENV)
        if routing:
            receive(sys.stdin.buffer.read(1_048_577), routing)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'install', 'remove'))
    parser.add_argument('--kimi-home', default=os.environ.get('KIMI_CODE_HOME', str(Path.home() / '.kimi-code')))
    args = parser.parse_args()
    try:
        result = dict(ok=check(args.kimi_home)) if args.action == 'check' else configure(args.kimi_home, args.action)
    except (OSError, ValueError) as exc:
        result = dict(ok=False, error=str(exc))
    print(json.dumps(result))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
