#!/usr/bin/env python3
"""Local hook inbox and progress monitor; no model calls or application IPC."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import time


def hook_settings(script, state_dir, job, record):
    command = shlex.join([sys.executable, str(script), '--state-dir', state_dir,
                         '--job', job['job_id'], '--round', str(record['round']),
                         '--token', record['run_token']])
    # Restricted mode loads this per-run file, not user/project custom hooks.
    # Edit(path) covers both native Edit and Write. Write(path) is not consulted
    # by Claude. Absolute rules need two leading slashes; escape glob literals.
    cwd = job.get('cwd') or os.getcwd()
    literal = ''.join('\\' + ch if ch in '\\*?[]!#' else ch for ch in cwd.rstrip('/'))
    return {'permissions': {'allow': ['Edit(/' + literal + '/**)']},
            'hooks': {event: [{'matcher': 'idle_prompt' if event == 'Notification' else '',
                              'hooks': [{'type': 'command', 'command': command, 'timeout': 5}]}]
                      for event in ('Stop', 'StopFailure', 'PostToolUseFailure', 'Notification')}}


def receive_hook():
    import claude_task as ct
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('--round', type=int, required=True)
    parser.add_argument('--token', required=True)
    args = parser.parse_args()
    # Malformed/stale hooks are ignored. Hook output must never command Claude
    # to continue, reject a tool, or leak the task's transcript to stdout.
    try:
        raw = sys.stdin.buffer.read(1_048_577)
        if len(raw) > 1_048_576:
            return
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get('agent_id'):
            return
        ctx = ct.Context(args.state_dir)
        with ct.StateLock(ctx.state_dir):
            job = ctx.load(args.job)
            index, record = ct.current_round(job)
            if (index != args.round or record.get('run_token') != args.token
                    or record.get('finalized') or job.get('stop_requested')
                    or payload.get('session_id') != job['session_id']
                    or os.path.realpath(payload.get('cwd', '')) != os.path.realpath(job['cwd'])):
                return
            name = payload.get('hook_event_name')
            if name not in ('Stop', 'StopFailure', 'PostToolUseFailure', 'Notification'):
                return
            if name == 'Notification' and payload.get('notification_type') != 'idle_prompt':
                return
            item = {'at': time.time(), 'hook': name,
                    'tool': ct.clip(payload.get('tool_name'), 80),
                    'tool_use_id': ct.clip(payload.get('tool_use_id'), 160),
                    'error': ct.clip(payload.get('error'), 600),
                    'detail': ct.clip(payload.get('error_details'), 600),
                    'is_interrupt': payload.get('is_interrupt') is True,
                    'stop_hook_active': payload.get('stop_hook_active') is True}
            target = Path(ct.round_dir(ctx.job_dir(args.job), index)) / 'hooks.ndjson'
            fd = os.open(str(target), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, 'a') as out:
                fcntl.flock(out.fileno(), fcntl.LOCK_EX)
                out.write(json.dumps(item, ensure_ascii=False) + '\n')
    except (OSError, ValueError, TypeError, ct.CliError):
        return


class Monitor:
    """Single writer (round worker). Readers consume persisted event cursors.

    Only metadata and bounded error summaries are saved. Raw partial thinking
    already goes to the original private stdout evidence file, never the status.
    """
    def __init__(self, directory, session_id, started, write_json, clock=time.time, quota_observer=None):
        self.directory = Path(directory)
        self.session_id = session_id
        self.started = started
        self.write_json = write_json
        self.clock = clock
        self.offsets = {}
        self.messages = {}
        self.current_message = None
        self.tools = {}
        self.seen_errors = set()
        self.incident_active = False
        self.events = []
        self.checkpoints = {}
        self.last_progress = None
        self.progress_count = 0
        self.latest_error = None
        self.hooks_seen = {}
        self.result_usage = None
        self.quota_observer = quota_observer
        self.quota = None
        self.quota_notified = set()
        self.last_save = -float('inf')
        self.tick()

    def emit(self, kind, **fields):
        self.events.append(dict(seq=len(self.events) + 1, kind=kind, at=self.clock(), **fields))

    def progress(self):
        self.last_progress = self.clock()
        self.progress_count += 1

    def error(self, source, tool='', message='', fatal=False):
        import claude_task as ct
        message = ct.clip(message, 600)
        signature = hashlib.sha256((str(fatal) + tool + message).encode()).hexdigest()
        error = dict(source=source, tool=tool, message=message, fatal=fatal, at=self.clock())
        if source != 'tool_result' or not self.incident_active:
            self.latest_error = error
        # A continuing failure burst is one incident; repeating the same error
        # later in this round also stays quiet. Terminal exit still surfaces.
        if signature not in self.seen_errors and (fatal or not self.incident_active):
            self.emit('error', error=error)
        self.seen_errors.add(signature)
        self.incident_active = True

    @staticmethod
    def number(value):
        return value if type(value) is int and value >= 0 else None

    def stream(self, row):
        if not isinstance(row, dict) or row.get('session_id') not in (None, self.session_id):
            return
        kind = row.get('type')
        if kind == 'rate_limit_event' and self.quota_observer and row.get('session_id') == self.session_id:
            self.quota = self.quota_observer(row)
            for window in self.quota.get('blocking_windows', []):
                bucket = self.quota['windows'][window]
                key = (window, bucket.get('resets_at'))
                if key not in self.quota_notified:
                    self.emit('quota_pause', quota=self.quota,
                              message='Finish the current round; pause new Claude calls at 90% quota.')
                    self.quota_notified.add(key)
        elif kind == 'stream_event':
            event = row.get('event') or {}
            subtype = event.get('type')
            if subtype == 'message_start':
                message = event.get('message') or {}
                ident = message.get('id')
                if ident:
                    self.current_message = ident
                    self.messages.setdefault(ident, {'input': None, 'output': None})
                    self.input_usage(ident, message.get('usage') or {})
            elif subtype == 'message_delta' and self.current_message:
                value = self.number((event.get('usage') or {}).get('output_tokens'))
                if value is not None:
                    entry = self.messages[self.current_message]
                    if entry['output'] is None or value > entry['output']:
                        self.progress()
                    entry['output'] = max(entry['output'] or 0, value)
            elif subtype == 'content_block_delta':
                # Even if usage is published late, incoming content is progress.
                self.progress()
        elif kind == 'assistant':
            message = row.get('message') or {}
            ident = message.get('id')
            if ident:
                self.messages.setdefault(ident, {'input': None, 'output': None})
                self.input_usage(ident, message.get('usage') or {})
            # Assistant output_tokens are message_start placeholders. Never sum.
            for block in message.get('content') or []:
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    self.tools[block.get('id')] = block.get('name')
            self.progress()
        elif kind == 'user':
            for block in (row.get('message') or {}).get('content') or []:
                if not isinstance(block, dict) or block.get('type') != 'tool_result':
                    continue
                tool = self.tools.pop(block.get('tool_use_id'), '') or ''
                if block.get('is_error') is True:
                    # Hook supplies the bounded actual error. Stream fallback
                    # deliberately omits arbitrary tool-result/secret content.
                    self.error('tool_result', tool, 'tool failed; see hook/evidence')
                else:
                    self.incident_active = False
                    self.progress()
        elif kind == 'tool_progress':
            # Elapsed-time heartbeats prove only a running tool, not useful work.
            ident = row.get('tool_use_id')
            if ident:
                self.tools[ident] = row.get('tool_name')
        elif kind == 'result':
            usage = row.get('usage')
            if isinstance(usage, dict):
                self.result_usage = usage

    def input_usage(self, ident, usage):
        value = self.number(usage.get('input_tokens'))
        if value is not None:
            value += sum(self.number(usage.get(key)) or 0 for key in
                         ('cache_creation_input_tokens', 'cache_read_input_tokens'))
            self.messages[ident]['input'] = value

    def hook(self, row):
        name = row.get('hook')
        self.hooks_seen[name] = self.hooks_seen.get(name, 0) + 1
        if name in ('PostToolUseFailure', 'StopFailure') and not row.get('is_interrupt'):
            self.error(name, row.get('tool') or '', row.get('error') or row.get('detail') or name,
                       fatal=name == 'StopFailure')
        # Stop/idle_prompt are hints only. Verification and process exit decide
        # readiness; duplicate Stop, cron/subagent pauses never trigger review.

    def drain(self, filename, handler):
        path = self.directory / filename
        if not path.exists():
            return
        with path.open('rb') as source:
            source.seek(self.offsets.get(filename, 0))
            while True:
                offset = source.tell()
                line = source.readline(4 * 1024 * 1024 + 1)
                if not line:
                    break
                if not line.endswith(b'\n'):
                    # Keep a partial line for the next tick, bounded in memory.
                    if len(line) > 4 * 1024 * 1024:
                        raise ValueError('monitor line exceeds 4 MiB')
                    source.seek(offset)
                    break
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        handler(row)
                except (ValueError, TypeError):
                    pass  # Full completion verifier still rejects malformed data.
                self.offsets[filename] = source.tell()

    def snapshot(self):
        inputs = [m['input'] for m in self.messages.values() if m['input'] is not None]
        outputs = [m['output'] for m in self.messages.values() if m['output'] is not None]
        usage = self.result_usage
        return dict(at=self.clock(), elapsed_seconds=round(self.clock() - self.started, 1),
                    input_tokens=sum(inputs) if inputs else None,
                    output_tokens=self.number(usage.get('output_tokens')) if usage is not None
                                  else (sum(outputs) if outputs else None),
                    output_source='result' if usage is not None else
                                  ('partial_message_delta' if outputs else 'unknown'),
                    progress_count=self.progress_count, last_progress_at=self.last_progress,
                    active_tools=list(self.tools.values()), latest_error=self.latest_error,
                    hooks_seen=dict(self.hooks_seen), quota=self.quota)

    @staticmethod
    def compare(before, after):
        if before is None:
            return {'assessment': 'unknown', 'reason': 'no earlier observation'}
        interval = after['at'] - before['at']
        result = {'observed_seconds': round(interval, 1)}
        for key in ('input_tokens', 'output_tokens'):
            a, b = before.get(key), after.get(key)
            result[key + '_delta'] = b - a if a is not None and b is not None else None
        if interval < 240:
            result['assessment'] = 'unknown'
            result['reason'] = 'observation gap shorter than four minutes'
        elif after['progress_count'] > before['progress_count'] or any(
                (result[k + '_delta'] or 0) > 0 for k in ('input_tokens', 'output_tokens')):
            result['assessment'] = 'progressing'
        else:
            result['assessment'] = 'waiting_for_tool' if after['active_tools'] else 'no_observed_progress'
            result['reason'] = 'investigate once; missing output is not proof of a stuck process'
        return result

    def tick(self, complete=False):
        self.drain('hooks.ndjson', self.hook)
        self.drain('stdout.ndjson', self.stream)
        t = self.clock()
        if not complete:
            for threshold in (600, 900):
                if t - self.started >= threshold and threshold not in self.checkpoints:
                    shot = self.snapshot()
                    self.checkpoints[threshold] = shot
                    comparison = self.compare(self.checkpoints.get(600), shot) if threshold == 900 else None
                    self.emit('checkpoint', minute=threshold // 60, progress=shot, comparison=comparison)
        if complete or t - self.last_save >= 1 or len(self.events) != getattr(self, 'saved_events', -1):
            self.write_json(str(self.directory / 'monitor.json'),
                            dict(version=1, events=self.events, progress=self.snapshot()))
            self.last_save = t
            self.saved_events = len(self.events)


if __name__ == '__main__':
    receive_hook()
