"""Claude subscription quota from native stream events. No credential/API access."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time

LIMIT = 90.0
MAX_AGE = 900
WINDOWS = ('five_hour', 'seven_day', 'seven_day_opus')

def account_home():
    return os.path.realpath(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))

def cache_path(state_dir, home=None):
    key = hashlib.sha256((home or account_home()).encode()).hexdigest()[:20]
    return Path(state_dir) / ('claude-quota-' + key + '.json')

def number(value):
    try: return float(value) if type(value) in (int, float) and math.isfinite(value) else None
    except OverflowError: return None

def fingerprint(event):
    return hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()

def parse_event(event, session):
    if (not isinstance(event, dict) or event.get('type') != 'rate_limit_event'
            or event.get('session_id') != session):
        return {}
    info = event.get('rate_limit_info')
    if not isinstance(info, dict): return {}
    unified = info.get('unifiedWindows') or info.get('unified_windows') or {}
    out = {}
    for name in WINDOWS:
        data = unified.get(name) if isinstance(unified, dict) else None
        if not isinstance(data, dict) and info.get('rateLimitType', info.get('rate_limit_type')) == name:
            data = info
        if not isinstance(data, dict): continue
        fraction = number(data.get('utilization'))
        reset = number(data.get('resetsAt', data.get('resets_at')))
        if fraction is not None and 0 <= fraction <= 1:
            # Preserve precision: an 89.6% display rounded to 90 must not pause.
            out[name] = dict(used_percent=fraction * 100, resets_at=reset)
        elif data is info and info.get('status') == 'rejected':
            out[name] = dict(used_percent=None, rejected=True, resets_at=reset)
    return out

def read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, ValueError): return {}

def store(state_dir, buckets, source, observed_at=None, home=None):
    if not buckets: return
    observed = time.time() if observed_at is None else observed_at
    path = cache_path(state_dir, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read(path)
        windows = data.setdefault('windows', {})
        for name, value in buckets.items():
            old = windows.get(name, {})
            if observed < old.get('observed_at', 0): continue
            old_reset, new_reset = number(old.get('resets_at')), number(value.get('resets_at'))
            if old_reset is not None and new_reset is not None and new_reset < old_reset: continue
            if source.get('kind') == 'native_stream_import' and old_reset == new_reset:
                old_used, new_used = number(old.get('used_percent')), number(value.get('used_percent'))
                if old_used is not None and new_used is not None and new_used < old_used:
                    continue  # A later file mtime cannot clear a known higher quota.
            windows[name] = {**value, 'observed_at': observed, 'source': source}
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.quota-')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, ensure_ascii=False); f.flush(); os.fsync(f.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp): os.unlink(temp)

def status(state_dir, home=None, now=None):
    now = time.time() if now is None else now
    data = read(cache_path(state_dir, home)); windows = {}; blocking = []
    for name, value in data.get('windows', {}).items():
        if name not in WINDOWS or not isinstance(value, dict): continue
        observed = number(value.get('observed_at')); used = number(value.get('used_percent'))
        reset = number(value.get('resets_at'))
        fresh = (observed is not None and 0 <= now - observed <= MAX_AGE
                 and value.get('source', {}).get('time_basis') != 'source_mtime')
        expired = reset is not None and now >= reset
        above = used is not None and used >= LIMIT or value.get('rejected') is True
        # An old above-threshold observation stays a hold until its known reset.
        # A reset makes usage unknown, never zero. A later fresh observation can clear it.
        held = above and not expired
        if held: blocking.append(name)
        windows[name] = {**value, 'fresh': fresh and not expired, 'reset_passed': expired}
    state = 'paused' if blocking else ('available' if windows and all(v['fresh'] for v in windows.values()) else 'unknown')
    return dict(state=state, threshold_percent=LIMIT, blocking_windows=blocking, windows=windows,
                action='finish_current_round_then_pause' if blocking else
                       ('quota_unavailable_or_stale' if state == 'unknown' else 'continue'))

def refresh_from_jobs(state_dir, home=None):
    """Import only quota fields from the newest bounded legacy/current streams.

    Legacy events have no timestamp. Their source file mtime is a conservative
    import timestamp, explicitly labelled; never refresh unchanged bytes to now.
    """
    home = home or account_home(); candidates = []
    for path in (Path(state_dir) / 'jobs').glob('*/job.json'):
        if path.parent.is_symlink(): continue
        job = read(path)
        if job.get('backend', 'claude') != 'claude': continue
        if job.get('claude_config_dir', os.path.realpath(str(Path.home() / '.claude'))) != home: continue
        for record in (job.get('rounds') or [])[-1:]:
            rel = (record.get('evidence') or {}).get('stdout')
            if not isinstance(rel, str): continue
            stream = path.parent / rel
            try:
                if not stream.resolve().is_relative_to(path.parent.resolve()): continue
                candidates.append((stream.stat().st_mtime, stream, job.get('session_id')))
            except (OSError, ValueError): continue
    for modified, path, session in sorted(candidates, key=lambda x:x[0])[-12:]:
        try:
            with path.open('rb') as f:
                size = os.fstat(f.fileno()).st_size; f.seek(max(0, size - 4 * 1024 * 1024))
                if f.tell(): f.readline()
                lines = f.read(4 * 1024 * 1024).splitlines(keepends=True)
            for line in reversed(lines):
                if not line.endswith(b'\n'): continue
                try: event = json.loads(line)
                except ValueError: continue
                buckets = parse_event(event, session)
                if buckets:
                    source = dict(kind='native_stream_import', session_id=session, path=str(path),
                                  event_sha256=fingerprint(event), time_basis='source_mtime')
                    # Never make an already observed event appear newer merely
                    # because unrelated tool output appended to the same file.
                    existing = read(cache_path(state_dir, home)).get('windows', {})
                    buckets = {k:v for k,v in buckets.items() if existing.get(k, {}).get('source', {}).get('event_sha256') != source['event_sha256']}
                    store(state_dir, buckets, source, observed_at=modified, home=home)
                    break
        except OSError: continue
    return status(state_dir, home)

class Observer:
    def __init__(self, state_dir, home, session):
        self.state_dir, self.home, self.session = state_dir, home, session

    def __call__(self, event):
        buckets = parse_event(event, self.session)
        if buckets:
            store(self.state_dir, buckets, dict(kind='native_stream_live', session_id=self.session,
                  event_sha256=fingerprint(event)), home=self.home)
        return status(self.state_dir, self.home)
