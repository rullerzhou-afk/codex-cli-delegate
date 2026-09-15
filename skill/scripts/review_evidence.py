"""Export exact, visible model responses with a small local provenance index.

No model calls. Thinking, tool inputs/results and raw streams stay outside the
export. Digests detect changed files; they are not provider signatures.
"""
import hashlib
import json
import os
from pathlib import Path
import re

MAX_FILE_BYTES = 128 * 1024 * 1024
VERIFICATION_FIELDS = ('model_verified', 'effort_verified', 'session_ok',
                       'assistant_models', 'efforts', 'ok', 'reasons')

# Evidence-manifest schema namespace and versions. Deliberately distinct from
# the job-state namespace in claude_task.py. Accepted manifests are immutable:
# export creates each provenance.json with O_EXCL and verify never rewrites it.
# An unsupported (typically newer) version fails closed instead of guessing.
SCHEMA_NAMESPACE = 'codex-cli-delegate/evidence-manifest'
SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION,)


class EvidenceError(ValueError):
    pass


def manifest_schema(manifest):
    """Return the manifest's schema version or raise on an unsupported one.

    The namespace is required on new manifests and tolerated as absent on
    legacy ones; a manifest that declares a different namespace is refused.
    """
    if not isinstance(manifest, dict):
        raise EvidenceError('Provenance manifest is not an object')
    namespace = manifest.get('schema_namespace')
    if namespace is not None and namespace != SCHEMA_NAMESPACE:
        raise EvidenceError('Unsupported provenance-manifest schema namespace')
    version = manifest.get('schema')
    if type(version) is not int or version not in SUPPORTED_SCHEMA_VERSIONS:
        raise EvidenceError('Unsupported provenance-manifest schema version')
    return version


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_bounded(path):
    with Path(path).open('rb') as source:
        data = source.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise EvidenceError('Evidence file exceeds 128 MiB; export is refused, never truncated')
    return data


def inside(root, relative):
    root, path = Path(root).resolve(), Path(relative)
    if path.is_absolute() or not path.parts or any(p in ('.', '..') for p in path.parts):
        raise EvidenceError('Evidence paths must stay inside their directory')
    candidate = root / path
    if any(p.is_symlink() for p in (candidate, *candidate.parents) if p != root and root in p.parents):
        raise EvidenceError('Symlinked evidence paths are not accepted')
    if root not in candidate.resolve().parents:
        raise EvidenceError('Evidence path escapes its directory')
    return candidate


def visible_responses(data, backend, session_id):
    responses, offset = [], 0
    for number, line in enumerate(data.splitlines(keepends=True), 1):
        if not line.endswith(b'\n'):
            raise EvidenceError('Source stream ends with an incomplete record')
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            raise EvidenceError('Source stream contains an invalid JSON record')
        if not isinstance(row, dict):
            raise EvidenceError('Source stream contains a non-object record')
        if session_id and row.get('session_id') not in (None, session_id):
            raise EvidenceError('Source stream contains another session')
        if backend == 'opencode' and row.get('sessionID') != session_id:
            raise EvidenceError('OpenCode source stream contains another session')
        texts = []
        if backend == 'claude' and not row.get('parent_tool_use_id'):
            if row.get('type') == 'assistant':
                message = row.get('message') or {}
                if not isinstance(message, dict) or not isinstance(message.get('content', []), list):
                    raise EvidenceError('Invalid assistant message')
                for index, part in enumerate(message.get('content', [])):
                    if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str):
                        texts.append(('/message/content/%d/text' % index, part['text'], 'assistant_text'))
            elif row.get('type') == 'result' and isinstance(row.get('result'), str):
                texts.append(('/result', row['result'], 'final_result'))
        elif backend == 'kimi' and row.get('role') == 'assistant' and isinstance(row.get('content'), str):
            texts.append(('/content', row['content'], 'assistant_text'))
        elif backend == 'opencode' and row.get('type') == 'text':
            part = row.get('part') or {}
            if isinstance(part.get('text'), str):
                texts.append(('/part/text', part['text'], 'assistant_text'))
        for pointer, value, kind in texts:
            if value:
                responses.append(dict(text=value, kind=kind, json_pointer=pointer, stream_line=number,
                                      record_byte_start=offset, record_byte_end=offset + len(line),
                                      record_sha256=digest(line)))
        offset += len(line)
    return responses


def round_sources(job, round_index, source_root):
    if type(round_index) is not int or not 0 <= round_index < len(job.get('rounds', [])):
        raise EvidenceError('Requested round does not exist')
    record = job['rounds'][round_index]
    verification = record.get('verification') or {}
    sdk_result = (job.get('backend', 'claude') == 'claude' and job.get('transport') == 'sdk'
                  and verification.get('completion_basis') == 'sdk_result'
                  and isinstance(verification.get('result_event'), dict)
                  and verification.get('session_ok') is True)
    if record.get('finalized') is not True or (type(record.get('exit_code')) is not int and not sdk_result):
        raise EvidenceError('Export requires a finalized round with an observed process exit or SDK result')
    prompt = read_bounded(inside(source_root, record['prompt']))
    if digest(prompt) != record.get('prompt_sha256'):
        raise EvidenceError('Saved task prompt no longer matches its digest')
    source = inside(source_root, record['evidence']['stdout'])
    data = read_bounded(source)
    sealed = (record.get('evidence_sha256') or {}).get('stdout')
    if sealed is not None and sealed != digest(data):
        raise EvidenceError('Source stream changed after round finalization')
    if sdk_result:
        if sealed is None:
            raise EvidenceError('SDK export requires the original round finalization seal')
        try:
            results = [row for line in data.splitlines() if (row := json.loads(line)).get('type') == 'result']
        except (ValueError, AttributeError):
            raise EvidenceError('Invalid SDK source stream')
        if (len(results) != 1 or results[0].get('session_id') != job.get('session_id')
                or any(results[0].get(k) != v for k, v in verification['result_event'].items())):
            raise EvidenceError('SDK result does not match the finalized source stream')
    backend = job.get('backend', 'claude')
    if backend not in ('claude', 'kimi', 'opencode'):
        raise EvidenceError('Unsupported backend')
    session = verification.get('native_session_id') or job.get('session_id')
    metadata = dict(job_id=job['job_id'], owner=job['owner'], backend=backend,
                    session_id=session, round=round_index, run_token=record['run_token'], cwd=job['cwd'],
                    requested=dict(model=job.get('model'), effort=job.get('effort')),
                    recorded_verification={key: verification.get(key) for key in VERIFICATION_FIELDS},
                    exit_code=record['exit_code'], prompt_sha256=digest(prompt),
                    source_stream=dict(path=str(source), sha256=digest(data), bytes=len(data),
                                       integrity_anchor='round_finalization' if sealed else 'export_time_only'))
    if job.get('transport') == 'sdk':
        metadata.update(transport='sdk', completion_basis=verification.get('completion_basis'))
    files, sources = {'prompt.md': prompt}, []
    for index, response in enumerate(visible_responses(data, backend, session), 1):
        filename = 'response-%04d.md' % index
        value = response.pop('text').encode('utf-8')
        files[filename] = value
        sources.append(dict(id='R%03d-S%04d' % (round_index, index), file=filename,
                            sha256=digest(value), bytes=len(value), **response))
    if not sources:
        raise EvidenceError('No visible model response was recorded; do not invent an original report')
    return metadata, files, sources


def index_bytes(manifest):
    index = ['# Model response evidence', '',
             '- Job: `%s`; round: `%s`; session: `%s`.' % (manifest['job_id'], manifest['round'], manifest['session_id']),
             '- Reviewed subject (declared by exporter): ' + manifest['subject']['value'],
             '- Integrity anchor: `' + manifest['source_stream']['integrity_anchor'] + '`.',
             '- Model/effort verification fields are in provenance.json; requested settings alone are not proof.',
             '- These are exact visible responses, not a Codex summary. Findings and disagreements belong in a separate decision record.',
             '- Keep this package private. Review content and upload authorization before any backup or sharing.',
             '', '| Source ID | Original response | Stream line | JSON field |', '| --- | --- | --- | --- |']
    for s in manifest['sources']:
        index.append('| %s | [%s](%s) | %s | `%s` |' % (s['id'], s['file'], s['file'], s['stream_line'], s['json_pointer']))
    return ('\n'.join(index) + '\n').encode('utf-8')


def export(ctx, job, round_index, destination, subject, created_at):
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 2000:
        raise EvidenceError('Provide the exact reviewed commit or document version as --subject')
    root = Path(ctx.job_dir(job['job_id']))
    metadata, files, sources = round_sources(job, round_index, root)
    manifest = dict(schema=SCHEMA_VERSION, schema_namespace=SCHEMA_NAMESPACE,
                    created_at=created_at, **metadata,
                    subject=dict(value=subject.strip(), origin='declared_by_exporter'),
                    sources=sources)
    files['INDEX.md'] = index_bytes(manifest)
    manifest['files'] = {name: dict(sha256=digest(value), bytes=len(value)) for name, value in files.items()}
    dest = Path(destination).expanduser().absolute()
    if not dest.parent.is_dir():
        raise EvidenceError('Export parent directory must already exist')
    # Exclusive creation preserves earlier exports and user files. A failed
    # write leaves an incomplete directory without a valid manifest.
    dest.mkdir(mode=0o700, exist_ok=False)
    for name, value in files.items():
        fd = os.open(str(dest / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(value)
    fd = os.open(str(dest / 'provenance.json'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(manifest, output, ensure_ascii=False, indent=2)
        output.write('\n')
    return verify(dest, job=job, round_index=round_index, source_root=root)


def verify(directory, job=None, round_index=None, source_root=None, expected_sha256=None):
    root = Path(directory).expanduser().absolute()
    if root.is_symlink():
        raise EvidenceError('Symlinked evidence directory is not accepted')
    manifest_path = inside(root, 'provenance.json')
    raw = read_bounded(manifest_path)
    if expected_sha256 is not None and (not re.fullmatch(r'[a-fA-F0-9]{64}', expected_sha256)
                                       or digest(raw) != expected_sha256.lower()):
        raise EvidenceError('Provenance manifest differs from the saved digest')
    try:
        manifest = json.loads(raw)
        manifest_schema(manifest)
        if (not isinstance(manifest['sources'], list) or not manifest['sources']
                or not isinstance(manifest['files'], dict) or type(manifest['round']) is not int
                or manifest['round'] < 0 or not isinstance(manifest['job_id'], str)
                or manifest['subject']['origin'] != 'declared_by_exporter'
                or not isinstance(manifest['subject']['value'], str) or not manifest['subject']['value'].strip()
                or manifest['source_stream']['integrity_anchor'] not in ('round_finalization', 'export_time_only')):
            raise EvidenceError('Incomplete provenance manifest')
        contents = {}
        for name, expected in manifest['files'].items():
            value = contents[name] = read_bounded(inside(root, name))
            if len(value) != expected['bytes'] or digest(value) != expected['sha256']:
                raise EvidenceError('Evidence file changed: ' + name)
        if manifest['files']['prompt.md']['sha256'] != manifest['prompt_sha256']:
            raise EvidenceError('Prompt digest mismatch')
        ids = set()
        for number, source in enumerate(manifest['sources'], 1):
            if (source['id'] in ids or source['id'] != 'R%03d-S%04d' % (manifest['round'], number)
                    or source['file'] != 'response-%04d.md' % number
                    or manifest['files'][source['file']] != {k: source[k] for k in ('sha256', 'bytes')}):
                raise EvidenceError('Invalid response source entry')
            ids.add(source['id'])
        if set(contents) != {'prompt.md', 'INDEX.md'} | {s['file'] for s in manifest['sources']}:
            raise EvidenceError('Unexpected or missing evidence files')
        if contents['INDEX.md'] != index_bytes(manifest):
            raise EvidenceError('Evidence index differs from its provenance manifest')
        if job is not None:
            if source_root is None:
                raise EvidenceError('Job verification requires its trusted source directory')
            metadata, original_files, original_sources = round_sources(job, round_index, source_root)
            if any(manifest.get(key) != value for key, value in metadata.items()):
                raise EvidenceError('Provenance metadata differs from the saved job or round')
            if manifest['sources'] != original_sources or any(contents.get(k) != v for k, v in original_files.items()):
                raise EvidenceError('Exported responses differ from the recorded source stream')
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise EvidenceError('Invalid evidence package: ' + str(exc))
    return dict(ok=True, directory=str(root), index=str(root / 'INDEX.md'),
                provenance=str(manifest_path), provenance_sha256=digest(raw),
                job_id=manifest['job_id'], round=manifest['round'],
                subject=manifest['subject'], source_count=len(manifest['sources']),
                integrity_anchor=manifest['source_stream']['integrity_anchor'])
