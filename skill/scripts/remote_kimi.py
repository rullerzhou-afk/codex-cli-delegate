"""Kimi parts of the Windows remote dispatcher.

The Windows runner parses Kimi's native records first. This module repeats the
native checks in Python on bytes fetched from Windows, through the same
check_turn rules the macOS Kimi adapter uses, so both Kimi routes accept the
same evidence.
"""
import base64
import hashlib
import json
import ntpath
import re

import kimi_backend
from tool_catalog import KIMI_DEFAULT, KIMI_TOOLS

# The runner passes the task as a -p argument; CreateProcess caps a whole
# command line at 32767 UTF-16 units. Keep in step with remote_codex_runner.cjs.
MAX_PROMPT_UNITS = 24000
WIRE_MAX_BYTES = 128 * 1024 * 1024
SESSION_RE = re.compile(r'session_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
NO_LOCAL_WRITES = {'Read', 'ReadMediaFile', 'Glob', 'Grep', 'WebSearch', 'FetchURL', 'TodoList'}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def same_path(a, b):
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    def norm(value):
        return ntpath.normcase(ntpath.normpath(value)).rstrip('\\/')
    return norm(a) == norm(b)


def run_marker(request_id):
    # Derived rather than random, so a retried request keeps the same digest.
    return sha256(('kimi-run:' + request_id).encode())[:32]


def select(site, model, effort, tools, validate_windows_path):
    policy = site.get('kimi')
    if not isinstance(policy, dict):
        raise ValueError('site does not allowlist Kimi')
    home = validate_windows_path(policy.get('kimi_home'), 'kimi_home')
    model, effort = model or kimi_backend.MODEL, effort or kimi_backend.EFFORT
    if (model, effort) != (kimi_backend.MODEL, kimi_backend.EFFORT):
        raise ValueError('Kimi profile is fixed at %s / %s' % (kimi_backend.MODEL, kimi_backend.EFFORT))
    if model not in policy.get('models', []):
        raise ValueError('model is not allowlisted')
    if effort not in policy.get('efforts', []):
        raise ValueError('effort is not allowlisted')
    chosen = sorted(set(tools or KIMI_DEFAULT))
    unknown = [tool for tool in chosen if tool not in KIMI_TOOLS]
    if unknown:
        raise ValueError('unsupported Kimi tools: ' + ', '.join(unknown))
    denied = [tool for tool in chosen if tool not in policy.get('tools', KIMI_DEFAULT)]
    if denied:
        raise ValueError('Kimi tools are not allowlisted: ' + ', '.join(denied))
    return dict(kimi_home=home, model=model, effort=effort, kimi_tools=chosen)


def prompt_bytes(marker, text):
    prompt = kimi_backend.marked_prompt(marker, text)
    if '\0' in prompt or len(prompt.encode('utf-16-le')) // 2 > MAX_PROMPT_UNITS:
        raise ValueError('Kimi task text must stay under %d characters, because it travels as a '
                         'command-line argument' % MAX_PROMPT_UNITS)
    return prompt.encode('utf8')


def rows_from_bytes(data):
    """Same rules as kimi_backend.rows: every record ends in LF and is an object."""
    lines = data.split(b'\n')
    if lines[-1]:
        raise ValueError('incomplete native record')
    rows = []
    for line in lines[:-1]:
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError('invalid record')
        rows.append(row)
    return rows


def check_stream(path, session):
    """The private stream relayed by the carrier must name exactly this session."""
    hints, version, report = kimi_backend.stream_summary(list(kimi_backend.rows(path)))
    if hints != [session]:
        raise ValueError('local_stream_identity_mismatch')
    return version, report


def assemble(frames):
    """Rebuild the fetched native files and check sizes and digests."""
    meta = [frame for frame in frames if frame.get('kind') == 'evidence_meta']
    end = [frame for frame in frames if frame.get('kind') == 'evidence_end']
    chunks = [frame for frame in frames if frame.get('kind') == 'evidence_chunk']
    if len(meta) != 1 or len(end) != 1 or len(meta) + len(end) + len(chunks) != len(frames):
        raise ValueError('evidence_frame_set')
    meta, end = meta[0], end[0]
    size = meta.get('wire_bytes')
    if not isinstance(size, int) or not 0 < size <= WIRE_MAX_BYTES or end.get('wire_bytes') != size:
        raise ValueError('evidence_size')
    parts, offset = [], 0
    for chunk in chunks:
        if chunk.get('offset') != offset:
            raise ValueError('evidence_chunk_order')
        data = base64.b64decode(chunk.get('data_b64', ''), validate=True)
        if not data:
            raise ValueError('evidence_chunk_empty')
        parts.append(data)
        offset += len(data)
    wire = b''.join(parts)
    if (len(wire) != size or sha256(wire) != meta.get('wire_sha256')
            or end.get('wire_sha256') != meta.get('wire_sha256')):
        raise ValueError('evidence_digest')
    if not isinstance(meta.get('index'), dict) or not isinstance(meta.get('session_dir'), str):
        raise ValueError('evidence_index')
    return dict(session=meta.get('session'), session_dir=meta['session_dir'], index=meta['index'],
                state=base64.b64decode(meta.get('state_b64', ''), validate=True), wire=wire)


def verify(job, receipt, report, evidence):
    """Independent second parse; returns (reasons, loop requests, verified wire bytes)."""
    reasons = []
    session = receipt.get('session')
    if not SESSION_RE.fullmatch(session or '') or evidence['session'] != session:
        raise ValueError('session identity mismatch')
    if job.get('session_id') and session != job['session_id']:
        reasons.append('session_changed')
    index, directory = evidence['index'], evidence['session_dir']
    if (index.get('sessionId') != session or not same_path(index.get('workDir'), job['cwd'])
            or not same_path(index.get('sessionDir'), directory)
            or ntpath.basename(ntpath.normpath(directory)) != session
            or not same_path(directory, receipt.get('session_dir'))):
        reasons.append('session_index_mismatch')
    state = json.loads(evidence['state'])
    if state.get('id') != session or not same_path(state.get('cwd'), job['cwd']):
        reasons.append('session_mismatch')
    size, wire = receipt.get('wire_bytes'), evidence['wire']
    # The file may only have grown since completion; the claimed prefix must hold.
    if not isinstance(size, int) or len(wire) < size or sha256(wire[:size]) != receipt.get('wire_sha256'):
        raise ValueError('native evidence changed after completion')
    wire = wire[:size]
    offset = 0
    if job.get('session_id'):
        offset = job['baseline_bytes']
        if size <= offset or sha256(wire[:offset]) != job['baseline_sha256'] or wire[offset - 1:offset] != b'\n':
            raise ValueError('native baseline changed')
    found, requests = kimi_backend.check_turn(rows_from_bytes(wire[offset:]), rows_from_bytes(wire), state,
                                              job['run_marker'], job['kimi_tools'], report)
    return reasons + found, requests, wire
