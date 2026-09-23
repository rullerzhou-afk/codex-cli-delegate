#!/usr/bin/env python3
"""Observe an exact Windows Codex CLI turn over existing SSH; never launch/kill Codex."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'claude-delegate' / 'remote'
TERMINAL = {'awaiting_review', 'incomplete_evidence', 'failed', 'interrupted', 'superseded', 'observer_error', 'accepted', 'detached'}
PS_UTF8 = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "

def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.' + secrets.token_hex(5))
    with open(temp, 'x', encoding='utf8') as f:
        os.chmod(temp, 0o600)
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush(); os.fsync(f.fileno())
    os.replace(temp, path)

def read(path):
    return json.loads(path.read_text(encoding='utf8'))

def ps_string(s):
    return "'" + s.replace("'", "''") + "'"

def ssh_argv(host, script):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,120}', host):
        raise ValueError('use an existing SSH host alias, without shell syntax')
    encoded = base64.b64encode(script.encode('utf-16le')).decode()
    return ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2',
            host, 'powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand ' + encoded]

def remote(host, script, payload=None):
    r = subprocess.run(ssh_argv(host, script), input=payload, capture_output=True, timeout=45)
    if r.returncode:
        raise RuntimeError('SSH operation failed (exit %d); existing SSH configuration was not changed' % r.returncode)
    return r.stdout.decode('utf8').strip()

def deploy(host):
    data = (HERE / 'remote_codex_agent.cjs').read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    # stdin carries code, not a shell command. Content-addressed path is verified before reuse.
    script = PS_UTF8 + "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; " + \
        "$d=Join-Path $env:USERPROFILE '.codex/remote-delegate/runtime'; " + \
        "$null=New-Item -ItemType Directory -Force -Path $d; $p=Join-Path $d '" + digest + ".cjs'; " + \
        "$b=[Convert]::FromBase64String([Console]::In.ReadToEnd()); " + \
        "if (!(Test-Path -LiteralPath $p)) {[IO.File]::WriteAllBytes($p,$b)}; " + \
        "if ((Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLower() -ne '" + digest + "') {throw 'runtime_hash_mismatch'}; " + \
        "@{path=$p; node=(Get-Command node.exe).Source; sha256='" + digest + "'} | ConvertTo-Json -Compress"
    return json.loads(remote(host, script, base64.b64encode(data)))

def owned(args):
    d = args.state_dir / args.job
    if not re.fullmatch(r'[0-9a-f-]{36}', args.job):
        raise ValueError('invalid job ID')
    job = read(d / 'job.json')
    if not args.owner or job['owner'] != args.owner:
        raise ValueError('owner mismatch; use the actual owning Codex task ID')
    return d, job

def public(d, job):
    state = read(d / 'state.json')
    state['receiver_active'] = receiver_active(d)
    if not state['receiver_active'] and state['status'] not in TERMINAL and time.time() - job['created_at'] > 5:
        state['status'] = 'observer_stopped'
    return {**{k: job[k] for k in ('job', 'owner', 'host', 'session', 'turn', 'cwd')},
            **state, 'result_file': str(d / 'final.md') if (d / 'final.md').exists() else None}

def receiver_active(d):
    with open(d / 'worker.lock', 'a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return True
    return False

def track(args):
    if not args.owner: raise ValueError('CODEX_THREAD_ID or --owner is required')
    for value in (args.session, args.turn): uuid.UUID(value)
    if not re.match(r'^[A-Za-z]:[\\/]', args.cwd) or not re.match(r'^[A-Za-z]:[\\/]', args.log):
        raise ValueError('Windows cwd and exact absolute session log are required')
    runtime = deploy(args.host)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(args.state_dir / 'registry.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for p in args.state_dir.glob('*/job.json'):
            j = read(p)
            if (j['host'], j['session'], j['turn']) == (args.host, args.session, args.turn):
                if j['owner'] != args.owner: raise ValueError('turn already belongs to another owner')
                print(json.dumps(public(p.parent, j), ensure_ascii=False)); return
        job_id = str(uuid.uuid4()); d = args.state_dir / job_id
        job = dict(job=job_id, owner=args.owner, host=args.host, session=args.session, turn=args.turn,
                   cwd=args.cwd, log=args.log, token=secrets.token_hex(32), runtime=runtime, created_at=time.time())
        save(d / 'job.json', job)
        save(d / 'state.json', dict(status='connecting', cursor=0, last_received=None))
    start_worker(args, d, job)
    print(json.dumps(public(d, job), ensure_ascii=False))

def start_worker(args, d, job):
    with open(d / 'worker.log', 'ab') as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--state-dir', str(args.state_dir),
                          '--owner', job['owner'], '_worker', job['job']], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True)

def apply_frame(d, job, state, frame):
    if any(frame.get(k) != job[k] for k in ('token', 'session', 'turn')) or frame.get('protocol') != 1:
        raise ValueError('frame_identity_mismatch')
    status = frame.get('status') if frame.get('kind') == 'state' else 'observer_error'
    if status not in {'unknown', 'running'} | TERMINAL:
        raise ValueError('unknown_remote_status')
    new = {k: frame[k] for k in ('activity', 'model', 'effort', 'sandbox', 'approval_policy', 'network_access',
                                  'writable_roots', 'cli_version', 'source', 'error', 'observed_at') if k in frame}
    if status == 'awaiting_review':
        src = new.get('source', {})
        if (src.get('path') != job['log'] or not isinstance(src.get('bytes'), int) or src['bytes'] <= 0
                or not re.fullmatch(r'[0-9a-f]{64}', src.get('sha256', '')) or not new.get('cli_version')):
            raise ValueError('missing_source_evidence')
    if 'final_text' in frame:
        text = frame['final_text']; digest = hashlib.sha256(text.encode('utf8')).hexdigest()
        if digest != frame.get('final_sha256'): raise ValueError('result_hash_mismatch')
        if status == 'awaiting_review' and (not text.strip() or not new.get('model') or not new.get('effort')):
            raise ValueError('incomplete_result')
        temp = d / 'final.pending'; temp.write_text(text, encoding='utf8'); os.chmod(temp, 0o600)
        os.replace(temp, d / 'final.md'); new['final_sha256'] = digest
    elif status == 'awaiting_review': raise ValueError('missing_final')
    if status == 'running' and new.get('activity'):
        from datetime import datetime
        age = time.time() - datetime.fromisoformat(new['activity'].replace('Z', '+00:00')).timestamp()
        if age >= 900: status = 'stalled'
    changed = status != state.get('status')
    return {**state, **new, 'status': status, 'last_received': time.time(),
            'cursor': state.get('cursor', 0) + int(changed)}

def worker(args):
    d, job = owned(args)
    with open(d / 'worker.lock', 'a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        backoff = 1
        while True:
            state = read(d / 'state.json')
            if state['status'] in TERMINAL: return
            if (d / 'detach').exists():
                save(d / 'state.json', {**state, 'status': 'detached', 'cursor': state['cursor'] + 1}); return
            config = {k: job[k] for k in ('log', 'session', 'turn', 'cwd', 'token')}
            if state.get('source', {}).get('bytes'):
                config['checkpoint'] = state['source']
            encoded = base64.b64encode(json.dumps(config).encode()).decode()
            script = PS_UTF8 + "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; & " + \
                ps_string(job['runtime']['node']) + ' ' + ps_string(job['runtime']['path']) + ' ' + ps_string(encoded)
            child = subprocess.Popen(ssh_argv(job['host'], script), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            selector = selectors.DefaultSelector(); selector.register(child.stdout, selectors.EVENT_READ)
            buffer = b''; last = time.monotonic()
            try:
                while child.poll() is None or selector.select(0):
                    if (d / 'detach').exists(): break
                    events = selector.select(1)
                    if not events:
                        if time.monotonic() - last > 40: break
                        continue
                    part = os.read(child.stdout.fileno(), 65536)
                    if not part: break
                    buffer += part; last = time.monotonic()
                    if len(buffer) > 8 * 1024 * 1024: raise ValueError('frame_size_limit')
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        if not line.strip(): continue
                        state = apply_frame(d, job, state, json.loads(line))
                        save(d / 'state.json', state); backoff = 1
                    if state['status'] in TERMINAL: return
            except (ValueError, KeyError, UnicodeError) as error:
                save(d / 'state.json', {**state, 'status': 'observer_error', 'error': str(error), 'cursor': state['cursor'] + 1})
                return
            finally:
                selector.close()
                if child.poll() is None:
                    child.terminate()
                    try: child.wait(timeout=5)
                    except subprocess.TimeoutExpired: child.kill(); child.wait()
                child.stdout.close()
            if (d / 'detach').exists(): continue
            if state['status'] != 'disconnected':
                state.update(status='disconnected', cursor=state['cursor'] + 1)
                save(d / 'state.json', state)
            time.sleep(backoff); backoff = min(backoff * 2, 30)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-dir', type=Path, default=ROOT)
    p.add_argument('--owner', default=os.environ.get('CODEX_THREAD_ID'))
    sub = p.add_subparsers(dest='command', required=True)
    t = sub.add_parser('track')
    for k in ('host', 'session', 'turn', 'cwd', 'log'): t.add_argument('--' + k, required=True)
    for name in ('status', 'await-event', 'reconnect', 'detach', 'accept', '_worker'):
        q = sub.add_parser(name); q.add_argument('job')
        if name == 'await-event':
            q.add_argument('--after', type=int, default=-1); q.add_argument('--timeout', type=int, default=900)
        if name == 'accept': q.add_argument('--notes-file', type=Path, required=True)
    args = p.parse_args(); args.state_dir = args.state_dir.expanduser().resolve()
    if args.command == 'track': return track(args)
    if args.command == '_worker': return worker(args)
    d, job = owned(args)
    if args.command == 'reconnect': start_worker(args, d, job)
    elif args.command == 'detach': (d / 'detach').touch(mode=0o600)
    elif args.command == 'accept':
        with open(d / 'worker.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            s = read(d / 'state.json')
            if s['status'] != 'awaiting_review': raise ValueError('not awaiting review')
            if hashlib.sha256((d / 'final.md').read_bytes()).hexdigest() != s['final_sha256']:
                raise ValueError('result changed after receipt')
            notes = args.notes_file.read_text(encoding='utf8')
            if not notes.strip(): raise ValueError('acceptance notes required')
            save(d / 'acceptance.json', dict(owner=job['owner'], notes=notes, accepted_at=time.time(),
                                          final_sha256=s['final_sha256'], source=s['source']))
            save(d / 'state.json', {**s, 'status': 'accepted', 'cursor': s['cursor'] + 1})
    elif args.command == 'await-event':
        deadline = time.monotonic() + max(1, args.timeout)
        while time.monotonic() < deadline:
            s = read(d / 'state.json')
            if s['cursor'] > args.after or s['status'] in TERMINAL: break
            if not receiver_active(d) and time.time() - job['created_at'] > 5: break
            time.sleep(0.25)
    print(json.dumps(public(d, job), ensure_ascii=False))

if __name__ == '__main__':
    try: main()
    except (ValueError, RuntimeError, FileNotFoundError, BlockingIOError) as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False)); sys.exit(1)
