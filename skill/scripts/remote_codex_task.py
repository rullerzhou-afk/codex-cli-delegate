#!/usr/bin/env python3
"""Dispatch one bounded Codex exec task to Windows over a life-support SSH carrier."""
import argparse
import base64
import fcntl
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import secrets
import selectors
import stat
import subprocess
import sys
import time
import uuid

import remote_codex as observer

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'claude-delegate' / 'remote' / 'tasks'
PROMPT_MAX_BYTES = 1024 * 1024
FRAME_MAX_BYTES = 4 * 1024 * 1024
BUFFER_MAX_BYTES = 8 * 1024 * 1024
STREAM_MAX_BYTES = 256 * 1024 * 1024
PACKET_MAX_BYTES = 2 * 1024 * 1024
CARRIER_INTERVAL = 30
CARRIER_COUNT = 10
CARRIER_WINDOW = CARRIER_INTERVAL * CARRIER_COUNT
PS_UTF8 = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
TERMINAL = {'awaiting_review', 'incomplete_evidence', 'failed', 'timed_out', 'cwd_busy',
            'killed_by_carrier_loss', 'observer_error', 'interrupted', 'superseded', 'accepted'}
REMOTE_RECEIPT_STATUSES = {'accepted', 'starting', 'running', 'completed_claimed', 'failed',
                           'timed_out', 'cwd_busy', 'killed_by_carrier_loss'}
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', re.I)
HOST_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$')


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.' + secrets.token_hex(5))
    with open(temp, 'x', encoding='utf8') as handle:
        os.chmod(temp, 0o600)
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temp, path)


def read(path):
    return json.loads(path.read_text(encoding='utf8'))


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def ps_string(value):
    return "'" + value.replace("'", "''") + "'"


def validate_uuid(value, label):
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise ValueError('invalid ' + label)
    return str(uuid.UUID(value))


def validate_windows_path(value, label):
    if not isinstance(value, str) or not re.match(r'^[A-Za-z]:[\\/]', value) or value.startswith('\\\\'):
        raise ValueError(label + ' must be an absolute local-drive path')
    rest = value[2:]
    if '\0' in value or ':' in rest:
        raise ValueError(label + ' contains refused syntax')
    parts = [part for part in re.split(r'[\\/]+', value[3:]) if part]
    if any(part == '..' or re.search(r'[ .]$', part) or re.search(r'~\d(?:\.|$)', part, re.I) for part in parts):
        raise ValueError(label + ' contains a refused path segment')
    return ntpath.normpath(value)


def within(root, candidate):
    root = ntpath.normcase(ntpath.normpath(root)).rstrip('\\/')
    candidate = ntpath.normcase(ntpath.normpath(candidate)).rstrip('\\/')
    return candidate == root or candidate.startswith(root + '\\')


def load_policy(path):
    if path is None:
        raise ValueError('--policy is required for dispatch')
    path = path.expanduser().resolve()
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ValueError('policy file must not be group/world accessible')
    data = read(path)
    if data.get('version') != 1 or not isinstance(data.get('sites'), dict):
        raise ValueError('unsupported policy')
    return path, data


def select_site(policy, name, cwd, model, effort, sandbox, timeout):
    site = policy['sites'].get(name)
    if not isinstance(site, dict):
        raise ValueError('site is not allowlisted')
    host = site.get('host')
    if not isinstance(host, str) or not HOST_RE.fullmatch(host):
        raise ValueError('policy contains invalid host alias')
    roots = site.get('cwd_roots')
    if not isinstance(roots, list) or not roots:
        raise ValueError('policy must include cwd_roots')
    roots = [validate_windows_path(value, 'cwd_root') for value in roots]
    cwd = validate_windows_path(cwd, 'cwd')
    if not any(within(root, cwd) for root in roots):
        raise ValueError('cwd is outside the site allowlist')
    codex_home = validate_windows_path(site.get('codex_home'), 'codex_home')
    allowed_models = site.get('models', [])
    allowed_efforts = site.get('efforts', [])
    allowed_sandboxes = site.get('sandboxes', ['read-only'])
    windows_sandbox = site.get('windows_sandbox')
    if model not in allowed_models: raise ValueError('model is not allowlisted')
    if effort not in allowed_efforts: raise ValueError('effort is not allowlisted')
    if sandbox not in allowed_sandboxes or sandbox not in {'read-only', 'workspace-write'}:
        raise ValueError('sandbox is not allowlisted')
    if windows_sandbox not in {'elevated', 'unelevated'}:
        raise ValueError('windows_sandbox must be explicitly allowlisted')
    maximum = int(site.get('max_timeout_seconds', 1800))
    if timeout < 30 or timeout > maximum:
        raise ValueError('timeout is outside the site allowlist')
    return dict(site=name, host=host, cwd=cwd, allowed_roots=roots, codex_home=codex_home,
                model=model, effort=effort, sandbox=sandbox, timeout_seconds=timeout,
                windows_sandbox=windows_sandbox, allow_non_git=site.get('allow_non_git') is True)


def ssh_argv(host, script, *, carrier=False):
    if not HOST_RE.fullmatch(host or ''):
        raise ValueError('use an allowlisted SSH host alias, without shell syntax')
    encoded = base64.b64encode(script.encode('utf-16le')).decode()
    interval, count = (CARRIER_INTERVAL, CARRIER_COUNT) if carrier else (10, 2)
    return ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=10', '-o', f'ServerAliveInterval={interval}',
            '-o', f'ServerAliveCountMax={count}', host,
            'powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand ' + encoded]


def remote(host, script, payload=None, timeout=45):
    result = subprocess.run(ssh_argv(host, script), input=payload, capture_output=True, timeout=timeout)
    if result.returncode:
        try:
            frames = [json.loads(line) for line in result.stdout.decode('utf8').splitlines()
                      if line.strip().startswith('{')]
            error = next((frame.get('error') for frame in reversed(frames)
                          if frame.get('kind') == 'error'), None)
            if isinstance(error, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', error):
                raise RuntimeError('remote_runner_error:' + error)
        except (json.JSONDecodeError, UnicodeError):
            pass
        raise RuntimeError('SSH operation failed (exit %d); result may be unknown' % result.returncode)
    return result.stdout.decode('utf8').strip()


def deploy_runner(host):
    data = (HERE / 'remote_codex_runner.cjs').read_bytes()
    digest = sha256(data)
    script = PS_UTF8 + "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; " + \
        "$d=Join-Path $env:USERPROFILE '.codex/remote-delegate/runtime'; " + \
        "$null=New-Item -ItemType Directory -Force -Path $d; $p=Join-Path $d '" + digest + ".cjs'; " + \
        "$b=[Convert]::FromBase64String([Console]::In.ReadToEnd()); " + \
        "if (!(Test-Path -LiteralPath $p)) {[IO.File]::WriteAllBytes($p,$b)}; " + \
        "if ((Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLower() -ne '" + digest + "') {throw 'runtime_hash_mismatch'}; " + \
        "@{path=$p; node=(Get-Command node.exe).Source; sha256='" + digest + "'} | ConvertTo-Json -Compress"
    return json.loads(remote(host, script, base64.b64encode(data)))


def carrier_active(directory):
    with open(directory / 'worker.lock', 'a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return True
    return False


def public(directory, job):
    state = read(directory / 'state.json')
    active = carrier_active(directory)
    if (not active and time.time() - job['created_at'] > 5
            and state['status'] not in TERMINAL and state['status'] not in {
            'completed_claimed', 'carrier_lost_pending_recheck', 'carrier_aborted_by_local_error', 'unknown'}):
        state = {**state, 'status': 'carrier_lost_pending_recheck',
                 'recheck_after': state.get('recheck_after') or time.time() + CARRIER_WINDOW}
    return {**{key: job[key] for key in ('job', 'owner', 'request_id', 'site', 'host', 'agent', 'cwd',
                                         'model', 'effort', 'sandbox', 'windows_sandbox', 'timeout_seconds')},
            **state, 'carrier_active': active,
            'result_file': str(directory / 'final.md') if (directory / 'final.md').exists() else None}


def owned(args):
    validate_uuid(args.job, 'job ID')
    directory = args.state_dir / args.job
    job = read(directory / 'job.json')
    if not args.owner or job['owner'] != validate_uuid(args.owner, 'owner'):
        raise ValueError('owner mismatch; use the actual owning Codex task ID')
    return directory, job


def request_identity(owner, request_id, site, prompt_sha):
    data = dict(owner=owner, request_id=request_id, site=site['site'], host=site['host'], agent='codex',
                cwd=site['cwd'], codex_home=site['codex_home'], model=site['model'], effort=site['effort'],
                sandbox=site['sandbox'], timeout_seconds=site['timeout_seconds'], prompt_sha256=prompt_sha,
                windows_sandbox=site['windows_sandbox'], allowed_roots=site['allowed_roots'],
                allow_non_git=site['allow_non_git'],
                keepalive_window_seconds=CARRIER_WINDOW)
    digest = sha256(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode())
    return data, digest


def start_worker(args, directory, job):
    with open(directory / 'worker.log', 'ab') as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--state-dir', str(args.state_dir),
                          '--owner', job['owner'], '_worker', job['job']], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True)


def dispatch(args):
    owner = validate_uuid(args.owner, 'owner')
    request_id = validate_uuid(args.request_id, 'request ID')
    if args.agent != 'codex': raise ValueError('unsupported_remote_agent')
    policy_path, policy = load_policy(args.policy)
    site = select_site(policy, args.site, args.cwd, args.model, args.effort, args.sandbox, args.timeout)
    prompt = args.prompt_file.read_bytes()
    if not prompt or len(prompt) > PROMPT_MAX_BYTES: raise ValueError('prompt must be 1..1048576 bytes')
    prompt_sha = sha256(prompt)
    identity, request_digest = request_identity(owner, request_id, site, prompt_sha)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(args.state_dir / 'registry.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for existing in args.state_dir.glob('*/job.json'):
            candidate = read(existing)
            if candidate.get('request_id') != request_id: continue
            if candidate.get('request_digest') != request_digest: raise ValueError('request_conflict')
            print(json.dumps(public(existing.parent, candidate), ensure_ascii=False)); return
        job_id = str(uuid.uuid4()); directory = args.state_dir / job_id
        directory.mkdir(mode=0o700)
        (directory / 'prompt.bin').write_bytes(prompt); os.chmod(directory / 'prompt.bin', 0o600)
        job = dict(job=job_id, **identity, request_digest=request_digest, token=secrets.token_hex(32),
                   policy=str(policy_path), policy_sha256=sha256(policy_path.read_bytes()),
                   created_at=time.time())
        save(directory / 'job.json', job)
        save(directory / 'state.json', dict(status='preparing', cursor=0, last_heartbeat=None))
    try:
        packet_for(job, 'dispatch', prompt)
        runtime = deploy_runner(job['host'])
        job = {**job, 'runtime': runtime}; save(directory / 'job.json', job)
        start_worker(args, directory, job)
    except Exception as error:
        state = read(directory / 'state.json')
        save(directory / 'state.json', {**state, 'status': 'failed', 'error': str(error),
                                        'cursor': state['cursor'] + 1})
    print(json.dumps(public(directory, job), ensure_ascii=False))


def packet_for(job, action, prompt=None, inspection_sha256=None):
    config = {key: job[key] for key in ('owner', 'request_id', 'request_digest', 'site', 'host', 'agent',
             'cwd', 'codex_home', 'model', 'effort', 'sandbox', 'timeout_seconds', 'prompt_sha256',
             'windows_sandbox', 'allowed_roots', 'allow_non_git', 'token', 'keepalive_window_seconds')}
    config.update(protocol=1, action=action)
    if inspection_sha256 is not None: config['inspection_sha256'] = inspection_sha256
    outer = {'config': config}
    if prompt is not None: outer['prompt_b64'] = base64.b64encode(prompt).decode()
    packet = base64.b64encode(json.dumps(outer, ensure_ascii=False, separators=(',', ':')).encode())
    if len(packet) > PACKET_MAX_BYTES: raise ValueError('packet_size_limit')
    return packet


def apply_frame(directory, job, state, frame, stream):
    if frame.get('protocol') != 1 or frame.get('token') != job['token'] or frame.get('request_id') != job['request_id']:
        raise ValueError('frame_identity_mismatch')
    kind = frame.get('kind')
    if kind == 'codex':
        line = base64.b64decode(frame.get('line_b64', ''), validate=True)
        if len(line) > FRAME_MAX_BYTES: raise ValueError('stream_line_limit')
        if stream.tell() + len(line) + 1 > STREAM_MAX_BYTES: raise ValueError('stream_size_limit')
        stream.write(line + b'\n'); stream.flush()
        return state
    if kind == 'heartbeat':
        return {**state, 'status': 'running', 'last_heartbeat': time.time(),
                'session': frame.get('session') or state.get('session')}
    if kind == 'state':
        if frame.get('status') != 'starting': raise ValueError('unknown_remote_status')
        return {**state, 'status': 'starting', 'process_pid': frame.get('process_pid'),
                'cli_version': frame.get('cli_version'), 'last_heartbeat': time.time()}
    if kind == 'receipt':
        receipt = frame.get('receipt')
        if not isinstance(receipt, dict) or receipt.get('request_id') != job['request_id']:
            raise ValueError('receipt_identity_mismatch')
        remote_status = receipt.get('status')
        if remote_status not in REMOTE_RECEIPT_STATUSES: raise ValueError('unknown_remote_status')
        save(directory / 'remote-receipt.json', receipt)
        local_status = 'connecting' if remote_status == 'accepted' else remote_status
        return {**state, 'status': local_status, 'remote_receipt': receipt,
                'session': receipt.get('session'), 'turn': receipt.get('turn'), 'log': receipt.get('log'),
                'partial_write_risk': bool(state.get('partial_write_risk')) or bool(receipt.get('partial_write_risk')),
                'recovery': receipt.get('recovery') or state.get('recovery'),
                'process_state': receipt.get('process_state'),
                'reconciliation': receipt.get('reconciliation'), 'receipt_checked_at': time.time()}
    if kind == 'error':
        if frame.get('status', 'unknown') != 'unknown': raise ValueError('unknown_remote_status')
        error = frame.get('error', 'runner_error')
        if not isinstance(error, str) or not error or len(error) > 512: raise ValueError('invalid_remote_error')
        return {**state, 'status': 'unknown', 'error': error, 'partial_write_risk': True,
                'recovery': 'query_remote_receipt_then_inspect_remote_worktree'}
    raise ValueError('unknown_frame_kind')


def validate_completion_receipt(job, receipt):
    required = ('session', 'turn', 'log', 'cwd', 'model', 'effort', 'sandbox', 'windows_sandbox', 'approval_policy',
                'cli_version', 'codex_path', 'process_pid', 'process_start_time', 'final_text_sha256')
    if any(receipt.get(key) in (None, '') for key in required):
        raise ValueError('missing_dispatch_identity')
    expected = {'cwd': job['cwd'], 'model': job['model'], 'effort': job['effort'],
                'sandbox': job['sandbox'], 'windows_sandbox': job['windows_sandbox'],
                'approval_policy': 'never'}
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError('dispatch_evidence_mismatch')


def validate_local_stream(directory, receipt):
    path = directory / 'stream.ndjson'
    if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > STREAM_MAX_BYTES:
        raise ValueError('local_stream_unavailable')
    sessions, completions = [], 0
    with open(path, 'rb') as stream:
        for raw in stream:
            if len(raw) > FRAME_MAX_BYTES + 1: raise ValueError('local_stream_line_limit')
            if not raw.strip(): continue
            try: event = json.loads(raw)
            except (json.JSONDecodeError, UnicodeError): raise ValueError('local_stream_invalid')
            if event.get('type') == 'thread.started': sessions.append(event.get('thread_id'))
            if event.get('type') == 'turn.completed': completions += 1
    if sessions != [receipt['session']] or completions != 1:
        raise ValueError('local_stream_identity_mismatch')


def verify_completion(directory, job, state):
    receipt = state.get('remote_receipt') or {}
    try: validate_completion_receipt(job, receipt)
    except ValueError as error:
        return {**state, 'status': 'incomplete_evidence', 'error': str(error)}
    try: validate_local_stream(directory, receipt)
    except ValueError as error:
        return {**state, 'status': 'incomplete_evidence', 'error': str(error)}
    runtime = observer.deploy(job['host'])
    proof = dict(log=receipt['log'], session=receipt['session'], turn=receipt['turn'], cwd=receipt['cwd'],
                 token=job['token'], once=True, single_turn=True)
    encoded = base64.b64encode(json.dumps(proof).encode()).decode()
    script = PS_UTF8 + "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; & " + \
        ps_string(runtime['node']) + ' ' + ps_string(runtime['path']) + ' ' + ps_string(encoded)
    output = remote(job['host'], script, timeout=60)
    lines = [json.loads(line) for line in output.splitlines() if line.strip().startswith('{')]
    if len(lines) != 1: return {**state, 'status': 'observer_error', 'error': 'observer_frame_count'}
    proof_job = dict(token=job['token'], session=receipt['session'], turn=receipt['turn'], log=receipt['log'])
    proof_state = dict(status='completed_claimed', cursor=state.get('cursor', 0))
    verified = observer.apply_frame(directory, proof_job, proof_state, lines[0])
    if verified.get('status') != 'awaiting_review':
        return {**state, 'status': 'incomplete_evidence',
                'error': 'observer_did_not_verify_completion', 'remote_receipt': receipt}
    if (verified.get('model') != job['model'] or verified.get('effort') != job['effort']
            or verified.get('sandbox') != job['sandbox'] or verified.get('approval_policy') != 'never'
            or verified.get('final_sha256') != receipt['final_text_sha256']):
        return {**state, 'status': 'incomplete_evidence', 'error': 'observer_evidence_mismatch',
                'remote_receipt': receipt}
    return {**state, **verified, 'remote_receipt': receipt}


def worker(args):
    directory, job = owned(args)
    with open(directory / 'worker.lock', 'a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        state = read(directory / 'state.json')
        state = {**state, 'status': 'connecting', 'cursor': state.get('cursor', 0) + 1}
        save(directory / 'state.json', state)
        runtime = job['runtime']
        script = PS_UTF8 + "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; & " + \
            ps_string(runtime['node']) + ' ' + ps_string(runtime['path'])
        child = None
        try:
            child = subprocess.Popen(ssh_argv(job['host'], script, carrier=True), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            child.stdin.write(packet_for(job, 'dispatch', (directory / 'prompt.bin').read_bytes()))
            child.stdin.close()
        except (OSError, BrokenPipeError) as error:
            state = {**state, 'status': 'unknown', 'error': str(error),
                     'partial_write_risk': child is not None,
                     'carrier_terminated_by_mac': child is not None,
                     'recheck_after': time.time() + CARRIER_WINDOW,
                     'recovery': 'wait_for_recheck_then_query_remote_receipt',
                     'cursor': state.get('cursor', 0) + 1}
            save(directory / 'state.json', state)
            if child is not None:
                if child.poll() is None:
                    child.terminate()
                    try: child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        try: child.wait(timeout=5)
                        except subprocess.TimeoutExpired: pass
                for handle in (child.stdin, child.stdout, child.stderr):
                    try: handle.close()
                    except Exception: pass
            return
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ, 'stdout')
        selector.register(child.stderr, selectors.EVENT_READ, 'stderr')
        buffers = {'stdout': b'', 'stderr': b''}
        with open(directory / 'stream.ndjson', 'ab') as stream, open(directory / 'carrier.stderr', 'ab') as errors:
            os.chmod(directory / 'stream.ndjson', 0o600); os.chmod(directory / 'carrier.stderr', 0o600)
            try:
                while selector.get_map():
                    for key, _ in selector.select(5):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj); continue
                        channel = key.data
                        if channel == 'stderr':
                            if errors.tell() < FRAME_MAX_BYTES:
                                errors.write(chunk[:FRAME_MAX_BYTES - errors.tell()]); errors.flush()
                            continue
                        buffers[channel] += chunk
                        if len(buffers[channel]) > BUFFER_MAX_BYTES: raise ValueError('frame_buffer_limit')
                        while b'\n' in buffers[channel]:
                            line, buffers[channel] = buffers[channel].split(b'\n', 1)
                            if not line.strip(): continue
                            if len(line) > FRAME_MAX_BYTES: raise ValueError('frame_size_limit')
                            next_state = apply_frame(directory, job, state, json.loads(line), stream)
                            if next_state is not state: save(directory / 'state.json', next_state)
                            state = next_state
                    if child.poll() is not None and not selector.get_map(): break
            except (ValueError, KeyError, UnicodeError, json.JSONDecodeError) as error:
                state = {**state, 'status': 'carrier_aborted_by_local_error', 'error': str(error),
                         'carrier_terminated_by_mac': True, 'partial_write_risk': True,
                         'recheck_after': time.time() + CARRIER_WINDOW,
                         'recovery': 'wait_for_recheck_then_query_remote_receipt_and_inspect_worktree',
                         'cursor': state.get('cursor', 0) + 1}
                save(directory / 'state.json', state)
            finally:
                selector.close()
                if child.poll() is None:
                    child.terminate()
                    try: child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        try: child.wait(timeout=5)
                        except subprocess.TimeoutExpired: pass
                child.stdout.close(); child.stderr.close()
        if state.get('status') == 'completed_claimed':
            try: state = verify_completion(directory, job, state)
            except Exception as error: state = {**state, 'status': 'observer_error', 'error': str(error)}
        elif state.get('status') not in TERMINAL and state.get('status') != 'carrier_aborted_by_local_error':
            state = {**state, 'status': 'carrier_lost_pending_recheck',
                     'recheck_after': max(time.time(), state.get('last_heartbeat') or 0) + CARRIER_WINDOW,
                     'partial_write_risk': True,
                     'recovery': 'wait_for_recheck_then_inspect_remote_worktree'}
        state['cursor'] = state.get('cursor', 0) + 1
        save(directory / 'state.json', state)


def query_remote_receipt(directory, job):
    runtime = job.get('runtime')
    if not isinstance(runtime, dict) or not runtime.get('node') or not runtime.get('path'):
        raise ValueError('remote runtime is unavailable; dispatch did not finish preparing')
    script = PS_UTF8 + "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; & " + \
        ps_string(runtime['node']) + ' ' + ps_string(runtime['path'])
    output = remote(job['host'], script, packet_for(job, 'receipt'), timeout=45)
    frames = [json.loads(line) for line in output.splitlines() if line.strip().startswith('{')]
    state = read(directory / 'state.json')
    with open(directory / 'stream.ndjson', 'ab') as stream:
        os.chmod(directory / 'stream.ndjson', 0o600)
        for frame in frames: state = apply_frame(directory, job, state, frame, stream)
    if state.get('status') == 'completed_claimed': state = verify_completion(directory, job, state)
    elif state.get('status') in {'accepted', 'connecting', 'starting', 'running'} and not carrier_active(directory):
        state = {**state, 'status': 'carrier_lost_pending_recheck', 'partial_write_risk': True,
                 'recheck_after': state.get('recheck_after') or
                     (state.get('last_heartbeat') or time.time()) + CARRIER_WINDOW,
                 'recovery': 'wait_for_recheck_then_inspect_remote_worktree'}
    state['cursor'] = state.get('cursor', 0) + 1
    save(directory / 'state.json', state)
    return state


def reclaim_remote_lock(directory, job, notes_file):
    state = read(directory / 'state.json')
    if state.get('status') != 'killed_by_carrier_loss': raise ValueError('lock reclaim requires killed_by_carrier_loss')
    notes = notes_file.read_text(encoding='utf8')
    if not notes.strip(): raise ValueError('inspection notes required')
    runtime = job.get('runtime')
    if not isinstance(runtime, dict) or not runtime.get('node') or not runtime.get('path'):
        raise ValueError('remote runtime is unavailable')
    notes_sha = sha256(notes.encode())
    script = PS_UTF8 + "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; & " + \
        ps_string(runtime['node']) + ' ' + ps_string(runtime['path'])
    output = remote(job['host'], script, packet_for(job, 'reclaim', inspection_sha256=notes_sha), timeout=45)
    frames = [json.loads(line) for line in output.splitlines() if line.strip().startswith('{')]
    if len(frames) != 1: raise ValueError('reclaim_frame_count')
    with open(directory / 'stream.ndjson', 'ab') as stream:
        os.chmod(directory / 'stream.ndjson', 0o600)
        updated = apply_frame(directory, job, state, frames[0], stream)
    receipt = updated.get('remote_receipt') or {}
    if not receipt.get('lock_reclaimed') or receipt.get('inspection_sha256') != notes_sha:
        raise ValueError('lock_reclaim_not_verified')
    save(directory / 'reclaim.json', {'owner': job['owner'], 'notes': notes, 'notes_sha256': notes_sha,
                                      'reclaimed_at': time.time(), 'remote_receipt': receipt})
    save(directory / 'state.json', {**updated, 'cursor': state.get('cursor', 0) + 1})


def accept(directory, job, notes_file):
    with open(directory / 'worker.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = read(directory / 'state.json')
        if state.get('status') != 'awaiting_review': raise ValueError('not awaiting review')
        final_sha = sha256((directory / 'final.md').read_bytes())
        if final_sha != state.get('final_sha256'): raise ValueError('result changed after receipt')
        notes = notes_file.read_text(encoding='utf8')
        if not notes.strip(): raise ValueError('acceptance notes required')
        save(directory / 'acceptance.json', dict(owner=job['owner'], accepted_at=time.time(), notes=notes,
                                                final_sha256=final_sha, remote_receipt=state.get('remote_receipt')))
        save(directory / 'state.json', {**state, 'status': 'accepted', 'cursor': state['cursor'] + 1})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=ROOT)
    parser.add_argument('--owner', default=os.environ.get('CODEX_THREAD_ID'))
    parser.add_argument('--policy', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    start = sub.add_parser('dispatch')
    start.add_argument('--site', required=True); start.add_argument('--agent', default='codex')
    start.add_argument('--cwd', required=True); start.add_argument('--request-id', required=True)
    start.add_argument('--prompt-file', type=Path, required=True)
    start.add_argument('--model', default='gpt-6-astra'); start.add_argument('--effort', default='xhigh')
    start.add_argument('--sandbox', default='workspace-write'); start.add_argument('--timeout', type=int, default=900)
    for name in ('status', 'receipt', 'await-event', 'accept', 'reclaim', '_worker'):
        command = sub.add_parser(name); command.add_argument('job')
        if name == 'await-event':
            command.add_argument('--after', type=int, default=-1); command.add_argument('--timeout', type=int, default=900)
        if name == 'accept': command.add_argument('--notes-file', type=Path, required=True)
        if name == 'reclaim': command.add_argument('--notes-file', type=Path, required=True)
    sub.add_parser('list')
    args = parser.parse_args(); args.state_dir = args.state_dir.expanduser().resolve()
    if args.command == 'dispatch': return dispatch(args)
    if args.command == 'list':
        owner = validate_uuid(args.owner, 'owner'); jobs = []
        if args.state_dir.exists():
            for path in args.state_dir.glob('*/job.json'):
                job = read(path)
                if job.get('owner') == owner: jobs.append(public(path.parent, job))
        print(json.dumps({'jobs': jobs}, ensure_ascii=False)); return
    directory, job = owned(args)
    if args.command == '_worker': return worker(args)
    if args.command == 'receipt': query_remote_receipt(directory, job)
    elif args.command == 'accept': accept(directory, job, args.notes_file)
    elif args.command == 'reclaim': reclaim_remote_lock(directory, job, args.notes_file)
    elif args.command == 'await-event':
        deadline = time.monotonic() + max(1, args.timeout)
        while time.monotonic() < deadline:
            state = read(directory / 'state.json')
            if state.get('cursor', 0) > args.after or state.get('status') in TERMINAL: break
            if not carrier_active(directory): break
            time.sleep(0.25)
    print(json.dumps(public(directory, job), ensure_ascii=False))


if __name__ == '__main__':
    try: main()
    except (ValueError, RuntimeError, FileNotFoundError, BlockingIOError, OSError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False)); sys.exit(1)
