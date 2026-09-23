"""Kimi Code 0.39+ print-mode adapter; lifecycle/locking remain in claude_task.

Read native wire records for completion/model/effort and incremental usage.
Never publish private thinking or arbitrary tool output in monitor metadata.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from claude_events import Monitor

MODEL = 'kimi-code/k3-256k'
RAW_MODEL = 'k3-256k'
EFFORT = 'max'
from tool_catalog import KIMI_TOOLS as TOOLS, select
SESSION_RE = re.compile(r'session_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')


def error(code, message):
    from claude_task import CliError
    raise CliError(code, message)


def prepare(explicit, requested_tools, allow_rules):
    chosen = select('kimi', requested_tools)
    if sys.platform != 'darwin':
        error('kimi_platform', 'Kimi process identity is currently verified on macOS only')
    if allow_rules:
        error('kimi_permissions', 'Kimi -p cannot enforce Claude --allow-tool rules; use --kimi-tool for tool-level selection')
    binary = explicit or shutil.which('kimi')
    if not binary or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        error('kimi_missing', 'Kimi executable not found; provide --kimi-bin')
    home = Path(os.environ.get('KIMI_CODE_HOME', str(Path.home() / '.kimi-code'))).resolve()
    # Kimi exposes no per-invocation effort flag. Fail before launch unless the
    # existing config explicitly selects max; native requests are checked too.
    config = (home / 'config.toml').read_text()
    section = re.search(r'^\[thinking\]\s*\n(.*?)(?=^\[|\Z)', config, re.M | re.S)
    if (not section or not re.search(r'^effort\s*=\s*["\']max["\']\s*(?:#.*)?$', section[1], re.M)
            or not re.search(r'^enabled\s*=\s*true\s*(?:#.*)?$', section[1], re.M)):
        error('kimi_effort_config', 'Existing Kimi [thinking] must have enabled=true and effort="max"; no config was changed')
    require_hooks(home)
    return dict(kimi_bin=os.path.realpath(binary), kimi_home=str(home), kimi_tools=chosen, kimi_hooks=True)


def require_hooks(home):
    from kimi_hooks import check
    if not check(home):
        error('kimi_hooks_missing', 'Kimi delegation hooks are missing or changed; inspect scripts/kimi_hooks.py check/install; no config was changed')


def kernel_identity(pid):
    """Read PID birth time in microseconds from macOS, unaffected by title.

    Layout is PROC_PIDTBSDINFO from the macOS SDK sys/proc_info.h; no shell,
    process environment, private transcript, or second-resolution ps fallback.
    """
    import ctypes as c
    if sys.platform != 'darwin':
        return None
    class BsdInfo(c.Structure):
        _fields_ = [('prefix', c.c_uint32 * 12), ('comm', c.c_char * 16),
                    ('name', c.c_char * 32), ('suffix', c.c_uint32 * 6),
                    ('sec', c.c_uint64), ('usec', c.c_uint64)]
    try:
        lib = c.CDLL('/usr/lib/libproc.dylib')
        lib.proc_pidinfo.argtypes = [c.c_int, c.c_int, c.c_uint64, c.c_void_p, c.c_int]
        lib.proc_pidinfo.restype = c.c_int
        lib.proc_pidpath.argtypes = [c.c_int, c.c_void_p, c.c_uint32]
        lib.proc_pidpath.restype = c.c_int
        info, path = BsdInfo(), c.create_string_buffer(4096)
        if (lib.proc_pidinfo(pid, 3, 0, c.byref(info), c.sizeof(info)) != c.sizeof(info)
                or info.prefix[3] != pid or not info.sec or info.usec >= 1000000
                or lib.proc_pidpath(pid, path, len(path)) <= 0):
            return None
        return dict(birth_us=info.sec * 1000000 + info.usec, uid=info.prefix[5],
                    ppid=info.prefix[4], pgid=info.suffix[1], executable=os.path.realpath(os.fsdecode(path.value)))
    except (OSError, ValueError, AttributeError):
        return None


def capture_identity(pid, token, binary):
    from claude_task import iso
    deadline = time.monotonic() + 2
    while True:
        ident = kernel_identity(pid)
        verified = bool(ident and ident['ppid'] == os.getpid() and ident['pgid'] == pid
                        and ident['uid'] == os.getuid() and ident['executable'] == os.path.realpath(binary))
        if verified or time.monotonic() >= deadline:
            return dict(pid=pid, run_token=token, identity_method='darwin_proc',
                        kernel=ident, identity_verified=verified, recorded_at=iso())
        time.sleep(.05)


def identity_state(record):
    live = kernel_identity(record['pid'])
    if live is None:
        return 'unverifiable'  # Caller already checks ESRCH; never infer gone.
    saved = record.get('kernel') or {}
    if not record.get('identity_verified'):
        return 'mismatch'
    # Parent may become launchd after worker loss; exact birth and executable
    # still identify the original child, allowing conservative recovery.
    return 'alive' if all(saved.get(k) == live.get(k) for k in
                          ('birth_us', 'uid', 'pgid', 'executable')) else 'mismatch'


def candidates(job):
    index = Path(job['kimi_home']) / 'session_index.jsonl'
    if not index.exists():
        return []
    found = {}
    for line in index.read_text().splitlines():
        try:
            row = json.loads(line)
            sid = row.get('sessionId', '')
            directory = Path(row.get('sessionDir', '')).resolve()
            sessions = (Path(job['kimi_home']) / 'sessions').resolve()
            if (SESSION_RE.fullmatch(sid) and os.path.realpath(row.get('workDir', '')) == job['cwd']
                    and directory.name == sid and sessions in directory.parents
                    and (not job.get('session_id') or sid == job['session_id'])):
                found[sid] = directory
        except (ValueError, TypeError, OSError):
            continue
    return list(found.values())


def wire_path(directory):
    return directory / 'agents' / 'main' / 'wire.jsonl'


def digest_prefix(path, size):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        remaining = size
        while remaining:
            chunk = source.read(min(remaining, 1024 * 1024))
            if not chunk:
                return None
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def baseline(job):
    if not job.get('session_id'):
        error('kimi_session_missing', 'Cannot resume without an identified native Kimi session; inspect the failed round')
    dirs = candidates(job)
    if len(dirs) != 1:
        error('kimi_session_missing', 'Expected exactly one native Kimi session for this job and cwd')
    path = wire_path(dirs[0])
    size = path.stat().st_size
    return [dict(path=str(path), size=size, sha256=digest_prefix(path, size))], 'ok'


def agent_profile(tools):
    """Per-job agent definition; the Windows dispatcher sends this same text."""
    return ('---\nname: codex-delegated-kimi\ndescription: Authorized scoped delegated task\n'
            'tools: ' + json.dumps(tools) + '\nsubagents: []\n---\n'
            '${base_prompt}\nFollow the supplied task scope. Do not use git push, gh, deployment, '
            'proxy changes, or external messages unless the task explicitly authorizes them.\n')


def marked_prompt(token, text):
    # Kimi has no --name or stdin prompt option. The unique marker also anchors
    # native turn identity and keeps ps identity proof independent of the PID.
    return '[delegation-run: %s]\n%s' % (token, text)


def argv(job, record, resume, prompt_file, directory):
    from claude_task import atomic_write_bytes
    directory = Path(directory)
    empty_skills = directory / 'empty-skills'
    empty_skills.mkdir(exist_ok=True, mode=0o700)
    profile = directory / 'agent.md'
    atomic_write_bytes(str(profile), agent_profile(job['kimi_tools']).encode())
    prompt = marked_prompt(record['run_token'], Path(prompt_file).read_text())
    args = [job['kimi_bin'], '-m', MODEL, '--output-format', 'stream-json',
            '--skills-dir', str(empty_skills), '-p', prompt]
    if resume:
        if not SESSION_RE.fullmatch(job.get('session_id') or ''):
            error('kimi_session_missing', 'Exact Kimi session is unavailable for resume')
        args += ['--session', job['session_id']]
    else:
        args += ['--agent-file', str(profile)]
    return args


def rows(path, offset=0):
    with Path(path).open('rb') as source:
        source.seek(offset)
        for line in source:
            if not line.endswith(b'\n'):
                raise ValueError('incomplete native record')
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError('invalid record')
            yield row


def is_prompt(row, token):
    return (row.get('type') == 'turn.prompt' and row.get('agentId') == 'main'
            and any(isinstance(p, dict) and p.get('type') == 'text'
                    and p.get('text', '').startswith('[delegation-run: %s]\n' % token)
                    for p in row.get('input', [])))


def discover(job, record):
    matches = []
    for directory in candidates(job):
        try:
            # Other sessions in this cwd are never adopted by recency alone.
            if any(is_prompt(row, record['run_token']) for row in rows(wire_path(directory))):
                matches.append(directory)
        except (OSError, ValueError, TypeError):
            continue
    if len(matches) > 1:
        error('kimi_session_ambiguous', 'Multiple native sessions contain this unique run marker')
    return matches[0] if matches else None


class KimiMonitor(Monitor):
    def __init__(self, directory, job, record, write_json, clock=None):
        self.job, self.record = job, record
        self.native = None
        self.native_active = False
        self.next_discovery = 0
        self.usage_count = 0
        self.pending_errors = {}
        self.hook_errors = set()
        kwargs = {} if clock is None else {'clock': clock}
        super().__init__(directory, job.get('session_id'), record['started_epoch'], write_json, **kwargs)

    def stream(self, row):
        if row.get('role') == 'meta' and row.get('type') == 'turn.step.retrying':
            self.error('kimi_retry', message='%s (HTTP %s)' %
                       (row.get('error_name', 'provider retry'), row.get('status_code', 'unknown')))
        # Tool results / usage are checked in structured native records, not
        # inferred from arbitrary strings like "error" in successful output.

    def hook(self, row):
        name = row.get('hook')
        if name not in ('Stop', 'StopFailure', 'PostToolUseFailure'):
            return
        self.hooks_seen[name] = self.hooks_seen.get(name, 0) + 1
        if name == 'PostToolUseFailure':
            key = ('tool', row.get('tool_call_id'))
            self.hook_errors.add(key)
            self.pending_errors.pop(key, None)
            self.error(name, row.get('tool') or '', 'Kimi tool failed; see private evidence')
        elif name == 'StopFailure':
            self.hook_errors.add(('turn', None))
            self.pending_errors.pop(('turn', None), None)
            self.error(name, message='Kimi turn failed; see private evidence', fatal=True)
        # Stop only records a hint. Process exit and verify() decide readiness.

    def fallback_error(self, key, source, tool='', message='', fatal=False):
        if key in self.hook_errors:
            return
        fields = dict(source=source, tool=tool, message=message, fatal=fatal)
        if self.job.get('kimi_hooks'):
            # Kimi sends failure hooks asynchronously. Give them one second to
            # arrive; the same canonical message deduplicates late hooks too.
            self.pending_errors[key] = (self.clock() + 1, fields)
        else:
            self.error(**fields)

    def native_row(self, row):
        if row.get('agentId') != 'main':
            return
        if row.get('type') == 'turn.prompt':
            self.native_active = is_prompt(row, self.record['run_token'])
        if not self.native_active:
            return
        kind = row.get('type')
        if kind == 'usage.record' and row.get('usageScope') == 'turn':
            usage = row.get('usage') or {}
            self.usage_count += 1
            inputs = [self.number(usage.get(k)) for k in ('inputOther', 'inputCacheRead', 'inputCacheCreation')]
            self.messages[str(self.usage_count)] = {
                'input': sum(n for n in inputs if n is not None) if any(n is not None for n in inputs) else None,
                'output': self.number(usage.get('output'))}
            self.progress()
        elif kind == 'context.append_loop_event':
            event = row.get('event') or {}
            etype = event.get('type')
            if etype in ('content.part', 'tool.call', 'step.begin', 'step.end'):
                self.progress()
            if etype == 'tool.call':
                self.tools[event.get('toolCallId')] = event.get('name', '')
            elif etype == 'tool.result':
                result = event.get('result') or {}
                tool = self.tools.pop(event.get('toolCallId'), '')
                if result.get('isError') is True:
                    self.fallback_error(('tool', event.get('toolCallId')), 'tool_result', tool,
                                        'Kimi tool failed; see private evidence')
                else:
                    self.incident_active = False
                    self.progress()
        elif kind == 'turn.ended' and row.get('reason') != 'completed':
            self.fallback_error(('turn', None), 'kimi_turn',
                                message='Kimi turn failed; see private evidence', fatal=True)

    def tick(self, complete=False):
        self.drain('hooks.ndjson', self.hook)
        if self.native is None and (complete or self.clock() >= self.next_discovery):
            self.native = discover(self.job, self.record)
            self.next_discovery = self.clock() + 2
            if self.native:
                self.session_id = self.native.name
                self.offsets[str(wire_path(self.native))] = 0
        if self.native:
            self.drain(str(wire_path(self.native)), self.native_row)
        self.drain('hooks.ndjson', self.hook)
        for key, (deadline, fields) in list(self.pending_errors.items()):
            if complete or self.clock() >= deadline:
                self.error(**fields)
                del self.pending_errors[key]
        super().tick(complete)

    def snapshot(self):
        shot = super().snapshot()
        shot['output_source'] = 'kimi_native_usage_per_step' if self.usage_count else 'unknown'
        shot['session_id'] = self.session_id
        shot['notification_mode'] = 'hooks_with_native_fallback' if self.job.get('kimi_hooks') else 'native_fallback'
        return shot


def stream_summary(stream):
    """Resume hints, CLI version and final text from stream-json rows."""
    hints = [r.get('session_id') for r in stream if r.get('type') == 'session.resume_hint']
    version = next((r.get('version') for r in stream if r.get('type') == 'system.version'), None)
    texts = [r['content'] for r in stream if r.get('role') == 'assistant' and isinstance(r.get('content'), str)]
    return hints, version, texts[-1] if texts else ''


def check_turn(native, whole, state, token, tools, report):
    """Native checks shared by the local and Windows Kimi adapters.

    native holds the wire rows after any revision baseline, whole every row.
    Returns (reasons, loop llm.request rows); raises without one run marker.
    """
    reasons = []
    starts = [i for i, r in enumerate(native) if is_prompt(r, token)]
    if len(starts) != 1:
        raise ValueError('run prompt missing or duplicate')
    active = [r for r in native[starts[0]:] if r.get('agentId') == 'main']
    if sum(r.get('type') == 'turn.prompt' for r in active) != 1:
        reasons.append('foreign_turn_after_prompt')
    requests = [r for r in active if r.get('type') == 'llm.request' and r.get('kind') == 'loop']
    if not requests or any(r.get('modelAlias') != MODEL or r.get('model') != RAW_MODEL for r in requests):
        reasons.append('model_unverified')
    if not requests or any(r.get('thinkingEffort') != EFFORT for r in requests):
        reasons.append('effort_unverified')
    endings = [r for r in active if r.get('type') == 'turn.ended']
    if len(endings) != 1 or endings[0].get('reason') != 'completed' or state.get('lastTurnReason') != 'completed':
        reasons.append('native_completion_missing')
    bindings = [r for r in whole if r.get('type') == 'profile.bind' and r.get('agentId') == 'main']
    if not bindings or sorted(bindings[-1].get('activeToolNames', [])) != sorted(tools):
        reasons.append('tool_profile_mismatch')
    events = [r.get('event') or {} for r in active if r.get('type') == 'context.append_loop_event']
    steps = [e for e in events if e.get('type') == 'step.end']
    if not steps or steps[-1].get('finishReason') != 'end_turn':
        reasons.append('end_turn_missing')
    if not report:
        reasons.append('final_report_missing')
    elif steps:
        final_step = steps[-1].get('uuid')
        native_text = ''.join((e.get('part') or {}).get('text', '') for e in events
                              if e.get('type') == 'content.part' and e.get('stepUuid') == final_step
                              and (e.get('part') or {}).get('type') == 'text')
        if native_text != report:
            reasons.append('final_report_mismatch')
    return reasons, requests


def verify(job, record, stdout_path, exit_code):
    from claude_task import clip
    reasons, models, efforts, requests = [], [], [], []
    session_ok = False
    sid, directory, report, version = None, None, '', None
    try:
        hints, version, report = stream_summary(list(rows(stdout_path)))
        directory = discover(job, record)
        if directory is None:
            raise ValueError('native session missing')
        sid = directory.name
        state = json.loads((directory / 'state.json').read_text())
        session_ok = (hints == [sid] and state.get('id') == sid
                      and os.path.realpath(state.get('cwd', '')) == job['cwd']
                      and job.get('session_id') in (None, sid))
        if not session_ok:
            reasons.append('session_mismatch')
        prior = record.get('prior_assistant_uuids') or []
        offset = 0
        if record.get('kind') == 'revision':
            if len(prior) != 1 or record.get('baseline_status') != 'ok':
                raise ValueError('revision baseline missing')
            b = prior[0]
            if b['path'] != str(wire_path(directory)) or digest_prefix(b['path'], b['size']) != b['sha256']:
                raise ValueError('native baseline changed')
            offset = b['size']
        native = list(rows(wire_path(directory), offset))
        found, requests = check_turn(native, list(rows(wire_path(directory))), state, record['run_token'],
                                     job['kimi_tools'], report)
        reasons += found
        models = [r.get('model') for r in requests]
        efforts = [r.get('thinkingEffort') for r in requests]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        reasons.append('native_evidence_invalid:' + str(exc)[:160])
    if exit_code != 0:
        reasons.insert(0, 'exit_nonzero')
    return dict(ok=not reasons, needs_attention=exit_code == 0 and bool(reasons), reasons=reasons,
                report=clip(report), exit_code=exit_code, cli_version=version, session_ok=session_ok,
                native_session_id=sid, model_verified=bool(models) and 'model_unverified' not in reasons,
                effort_verified=bool(efforts) and 'effort_unverified' not in reasons,
                assistant_models=sorted(set(m or '<missing>' for m in models)),
                efforts=sorted(set(e or '<missing>' for e in efforts)), new_assistant_entries=len(requests),
                transcript_lookup=str(directory) if directory else 'missing')
