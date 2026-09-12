#!/usr/bin/env python3
"""Opt-in, per-round macOS notifications; detached watcher, no model calls.

Delivery receipts mean submission to macOS, not proof of a visible banner.
Claim before delivery: an ambiguous crash cannot cause automatic duplicates.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import claude_task as ct

APPLE_SCRIPT = '''on run argv
    display notification (item 2 of argv) with title (item 1 of argv) subtitle (item 3 of argv)
end run'''


def send(title, body, subtitle):
    if sys.platform != 'darwin':
        return {'state': 'failed', 'reason': 'macos_required'}
    try:
        result = subprocess.run(['/usr/bin/osascript', '-e', APPLE_SCRIPT, title, body, subtitle],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=10, check=False)
        return {'state': 'submitted' if result.returncode == 0 else 'failed',
                'returncode': result.returncode}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'state': 'uncertain' if isinstance(exc, subprocess.TimeoutExpired) else 'failed',
                'reason': type(exc).__name__}


def location(ctx, job, index=None):
    if index is None:
        index = job['current_round']
    return Path(ct.round_dir(ctx.job_dir(job['job_id']), index)) / 'notification.json'


def read(path):
    try:
        return ct.read_json(str(path))
    except FileNotFoundError:
        return {}


def summary(ctx, job):
    data = read(location(ctx, job))
    if not data:
        return {'enabled': False}
    result = {k: data.get(k) for k in ('enabled', 'round', 'state', 'deliveries', 'reason') if k in data}
    if data.get('state') in ('starting', 'watching'):
        result['process'] = ct.identity_state(data.get('worker'))
    result['receipt'] = str(location(ctx, job))
    return result


def arm(ctx, job_id, enabled=True, expected_round=None):
    """Subscribe only to the current round; safe to repeat after a lost reply."""
    if type(enabled) is not bool:
        raise ct.CliError('bad_notification', 'enabled must be a boolean')
    if enabled and sys.platform != 'darwin':
        raise ct.CliError('notification_unsupported', 'desktop notifications currently require macOS')
    with ct.StateLock(ctx.state_dir):
        job = ctx.load_owned(job_id)
        index, record = ct.current_round(job)
        if expected_round is not None and (type(expected_round) is not int or expected_round != index):
            raise ct.CliError('stale_round', 'notification refers to a different round')
        path = location(ctx, job)
        data = read(path)
        if not enabled:
            if data:
                data.update(enabled=False, state='disabled')
                ct.write_json(str(path), data)
            return {'ok': True, 'job_id': job_id, 'notification': summary(ctx, job)}
        if job['phase'] in ct.TERMINAL_DECISION_PHASES:
            raise ct.CliError('notification_closed', 'accepted or stopped rounds do not need a completion notification')
        if data.get('run_token') == record.get('run_token'):
            # A claimed terminal submission is never replayed, even if its
            # process died before saving the OS response.
            if 'terminal' in data.get('deliveries', {}):
                return {'ok': True, 'job_id': job_id, 'notification': summary(ctx, job)}
            if data.get('enabled') and data.get('state') in ('starting', 'watching'):
                state = ct.identity_state(data.get('worker'))
                if state == 'alive':
                    return {'ok': True, 'job_id': job_id, 'notification': summary(ctx, job)}
                if state not in ct.STATE_GONE:
                    raise ct.CliError('notification_identity', 'watcher identity is uncertain; inspect before rearming')
        generation = str(uuid.uuid4())
        data = dict(enabled=True, state='starting', round=index, run_token=record['run_token'],
                    generation=generation, deliveries=data.get('deliveries', {}), owner=ctx.owner())
        ct.write_json(str(path), data)
        argv = [sys.executable, str(Path(__file__).resolve()), '--state-dir', ctx.state_dir,
                '--owner', ctx.owner(), '_watch', job_id, '--round', str(index),
                '--generation', generation]
        fd = os.open(str(path.with_suffix('.log')), os.O_CREAT | os.O_APPEND | os.O_WRONLY, ct.FILE_MODE)
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=fd, stderr=fd,
                                    cwd=ctx.job_dir(job_id), start_new_session=True, close_fds=True)
            data['worker'] = ct.capture_identity(proc.pid, generation)
            ct.write_json(str(path), data)
            threading.Thread(target=proc.wait, daemon=True, name='notification-reaper').start()
        except OSError as exc:
            data.update(state='failed', reason='watcher_spawn_failed')
            ct.write_json(str(path), data)
            raise ct.CliError('notification_spawn_failed', 'notification watcher could not start', errno=exc.errno)
        finally:
            os.close(fd)
    # Do not report armed just because Popen succeeded. Await child handshake.
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        with ct.StateLock(ctx.state_dir):
            job = ctx.load_owned(job_id)
            if job['current_round'] != index:
                raise ct.CliError('stale_round', 'round changed while arming notifications')
            current = read(path)
            if current.get('state') != 'starting':
                return {'ok': current.get('state') in ('watching', 'finished'), 'job_id': job_id,
                        'notification': summary(ctx, job)}
        time.sleep(0.05)
    raise ct.CliError('notification_start_timeout', 'watcher did not confirm readiness; inspect notification receipt')


def event_for(job, monitor, deliveries, expired=False):
    if job.get('stop_requested') or job['phase'] in ct.TERMINAL_DECISION_PHASES:
        return None, True
    if job['phase'] not in ct.BUSY_PHASES:
        _, record = ct.current_round(job)
        ready = (job['phase'] == ct.PHASE_AWAITING_REVIEW and record.get('finalized')
                 and (record.get('verification') or {}).get('ok') is True)
        return ('terminal', '完成，等待验收' if ready else '需要检查',
                '执行结果已保存。回到原 Codex 任务，让 Codex 独立验收。' if ready else
                '任务失败、中断或未通过执行核验。回到原任务检查记录；不要重复派单。'), True
    if expired:
        return ('terminal', '通知监控已到时', '任务未确认结束，可能仍在运行。请回到原任务检查状态。'), True
    for event in monitor.get('events', []):
        kind = event.get('kind')
        if kind == 'quota_pause' and 'quota' not in deliveries:
            return ('quota', '后续调用已暂停', 'Claude 额度达到暂停阈值；当前轮继续，后续派单需等待额度恢复。'), False
        if (kind == 'checkpoint' and (event.get('comparison') or {}).get('assessment') == 'no_observed_progress'
                and 'progress' not in deliveries):
            return ('progress', '需要检查进度', '一段时间内没有观察到进展。任务未被停止，也尚未确认失败。'), False
    return None, False


def tick(ctx, job_id, index, generation, sender=send, expired=False, reconcile=True):
    """One locked decision plus bounded OS submission; never calls a model."""
    with ct.StateLock(ctx.state_dir):
        job = ctx.load_owned(job_id)
        path = location(ctx, job, index)
        data = read(path)
        if data.get('generation') != generation or not data.get('enabled'):
            return True
        if job['current_round'] != index or ct.current_round(job)[1].get('run_token') != data.get('run_token'):
            data.update(state='superseded', enabled=False)
            ct.write_json(str(path), data)
            return True
        if reconcile:
            job, _ = ct.reconcile(ctx, job)
        monitor = read(path.parent / 'monitor.json')
        event, done = event_for(job, monitor, data['deliveries'], expired)
        if event and event[0] not in data['deliveries']:
            key, title, body = event
            data['deliveries'][key] = {'state': 'submitting', 'at': ct.iso()}
            ct.write_json(str(path), data)
            # Fixed strings plus IDs only: never expose task/error content.
            backend = {'claude': 'Claude', 'kimi': 'Kimi', 'opencode': 'OpenCode'}.get(job.get('backend'), 'Agent')
            subtitle = '%s · %s · 第 %d 轮' % (backend, job_id[:8], index + 1)
        else:
            event = None
        if not event:
            data['state'] = 'finished' if done else 'watching'
            ct.write_json(str(path), data)
    if event:
        # Never hold the shared job lock while macOS may be slow to respond.
        result = sender('委派任务：' + title, body, subtitle)
        with ct.StateLock(ctx.state_dir):
            latest = read(path)
            if latest.get('generation') == generation:
                latest['deliveries'][key].update(result)
                if latest.get('enabled'):
                    latest['state'] = 'finished' if done else 'watching'
                ct.write_json(str(path), latest)
    return done


def watch(ctx, job_id, index, generation):
    with ct.StateLock(ctx.state_dir):
        job = ctx.load_owned(job_id)
        path = location(ctx, job, index)
        data = read(path)
        if data.get('generation') != generation or not data.get('enabled'):
            return
        if (data.get('worker') or {}).get('pid') != os.getpid():
            return
        data['state'] = 'watching'
        ct.write_json(str(path), data)
        deadline = time.monotonic() + ct.validate_timeout(job['timeout']) + 120
    last_reconcile = -float('inf')
    while True:
        now = time.monotonic()
        reconcile = now - last_reconcile >= 5
        if tick(ctx, job_id, index, generation, expired=now >= deadline, reconcile=reconcile):
            return
        if reconcile:
            last_reconcile = now
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', default=ct.default_state_dir())
    parser.add_argument('--owner', default=os.environ.get('CODEX_THREAD_ID'))
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('arm', 'disable', 'status'):
        command = sub.add_parser(name)
        command.add_argument('job')
        if name != 'status':
            command.add_argument('--expected-round', type=int, required=True)
    sub.add_parser('probe', help='send one visible test notification; no model call')
    worker = sub.add_parser('_watch', help=argparse.SUPPRESS)
    worker.add_argument('job')
    worker.add_argument('--round', type=int, required=True)
    worker.add_argument('--generation', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'probe':
            result = send('委派通知测试', '这是一条测试通知。后台任务完成后会使用同一通知通道。', 'Claude Delegate')
        else:
            ctx = ct.Context(args.state_dir, owner=args.owner)
            if args.command == '_watch':
                watch(ctx, args.job, args.round, args.generation)
                return 0
            if args.command == 'status':
                with ct.StateLock(ctx.state_dir):
                    result = summary(ctx, ctx.load_owned(args.job))
            else:
                result = arm(ctx, args.job, enabled=args.command == 'arm', expected_round=args.expected_round)
        ct.emit(result)
        return 0 if result.get('ok', result.get('state') != 'failed') else 1
    except (ct.CliError, OSError, ValueError) as exc:
        ct.emit({'ok': False, 'error': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)})
        return 1


if __name__ == '__main__':
    sys.exit(main())
