"""Original-response preservation and acceptance tests; no model calls."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(os.environ['DELEGATE_SCRIPT']).resolve()
sys.path.insert(0, str(SCRIPT.parent))
import review_evidence as evidence


def ndjson(rows):
    return b''.join((json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8') for row in rows)


class Evidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'job'
        self.source.mkdir()
        self.ctx = SimpleNamespace(job_dir=lambda job: self.source)
        self.prompt = '检查 commit abc123 的 blocker；保留反对意见。\n'.encode()
        self.original = '原话：这里有实质分歧。\r\n' + '界🐱é' * 2000 + '\n尾部也必须保留。\n'
        self.rows = [
            dict(type='system', session_id='session-a'),
            dict(type='assistant', session_id='session-a', message=dict(content=[
                dict(type='thinking', thinking='HIDDEN_THOUGHT'),
                dict(type='tool_use', input={'private': 'HIDDEN_INPUT'}),
                dict(type='text', text=self.original)])),
            dict(type='user', message=dict(content=[dict(type='tool_result', content='HIDDEN_TOOL_RESULT')])),
            dict(type='assistant', parent_tool_use_id='subagent', message=dict(content=[dict(type='text', text='SUBAGENT')])),
            dict(type='result', session_id='session-a', result=self.original),
        ]
        stream = ndjson(self.rows)
        (self.source / 'prompt.md').write_bytes(self.prompt)
        (self.source / 'stdout.ndjson').write_bytes(stream)
        self.job = dict(job_id='job-a', owner='owner-a', backend='claude', session_id='session-a',
                        cwd='/example/project', model='claude-opus-5', effort='max', current_round=0,
                        rounds=[dict(finalized=True, exit_code=0, prompt='prompt.md',
                                     prompt_sha256=evidence.digest(self.prompt), run_token='run-a',
                                     evidence={'stdout': 'stdout.ndjson'},
                                     evidence_sha256={'stdout': evidence.digest(stream)},
                                     verification=dict(ok=True, model_verified=True, effort_verified=True,
                                                       assistant_models=['claude-opus-5'], efforts=['max']))])
        self.dest = self.root / 'export'

    def tearDown(self):
        self.temp.cleanup()

    def export(self, job=None, dest=None):
        return evidence.export(self.ctx, job or self.job, 0, dest or self.dest, 'commit abc123', '2026-09-06')

    def manifest(self):
        return json.loads((self.dest / 'provenance.json').read_bytes())

    def save_manifest(self, manifest):
        (self.dest / 'provenance.json').write_text(json.dumps(manifest, ensure_ascii=False))

    def bound_verify(self):
        return evidence.verify(self.dest, self.job, 0, self.source)

    def test_exact_unicode_full_length_offsets_and_hidden_fields(self):
        result = self.export()
        manifest = self.manifest()
        self.assertEqual(result['source_count'], 2)
        self.assertEqual((self.dest / 'response-0001.md').read_bytes(), self.original.encode('utf-8'))
        self.assertEqual((self.dest / 'response-0002.md').read_bytes(), self.original.encode('utf-8'))
        self.assertEqual((self.dest / 'prompt.md').read_bytes(), self.prompt)
        source = manifest['sources'][0]
        stream = (self.source / 'stdout.ndjson').read_bytes()
        record = stream[source['record_byte_start']:source['record_byte_end']]
        self.assertEqual(evidence.digest(record), source['record_sha256'])
        self.assertEqual(json.loads(record)['message']['content'][2]['text'], self.original)
        self.assertEqual(source['json_pointer'], '/message/content/2/text')
        exported = b''.join(path.read_bytes() for path in self.dest.iterdir())
        for hidden in (b'HIDDEN_THOUGHT', b'HIDDEN_INPUT', b'HIDDEN_TOOL_RESULT', b'SUBAGENT'):
            self.assertNotIn(hidden, exported)
        self.assertEqual(self.dest.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in self.dest.iterdir()))

    def test_kimi_format_and_missing_identity_are_not_promoted(self):
        self.job.update(backend='kimi', model='kimi-code/k3-256k')
        record = self.job['rounds'][0]
        record['verification'] = {'ok': False, 'model_verified': False, 'reasons': ['missing_model']}
        record['exit_code'] = 1
        stream = ndjson([dict(role='assistant', content=self.original, reasoning_content='HIDDEN_THOUGHT'),
                         dict(role='tool', content='HIDDEN_TOOL_RESULT'), dict(role='system', content='resume')])
        (self.source / 'stdout.ndjson').write_bytes(stream)
        record['evidence_sha256']['stdout'] = evidence.digest(stream)
        self.export()
        manifest = self.manifest()
        self.assertEqual(manifest['recorded_verification']['model_verified'], False)
        self.assertIsNone(manifest['recorded_verification']['effort_verified'])
        self.assertEqual(manifest['exit_code'], 1)
        self.assertEqual(len(manifest['sources']), 1)
        self.assertEqual((self.dest / 'response-0001.md').read_bytes(), self.original.encode())

    def test_legacy_export_has_only_export_time_anchor(self):
        self.job['rounds'][0].pop('evidence_sha256')
        result = self.export()
        self.assertEqual(result['integrity_anchor'], 'export_time_only')
        self.assertTrue(self.bound_verify()['ok'])

    def test_no_overwrite_of_existing_directory(self):
        self.export()
        before = (self.dest / 'provenance.json').read_bytes()
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual((self.dest / 'provenance.json').read_bytes(), before)

    def test_running_or_unknown_exit_cannot_export(self):
        for changes in ({'finalized': False}, {'exit_code': None}):
            job = copy.deepcopy(self.job)
            job['rounds'][0].update(changes)
            with self.assertRaises(evidence.EvidenceError):
                self.export(job)
            self.assertFalse(self.dest.exists())

    def test_invalid_incomplete_empty_or_foreign_session_stream(self):
        for stream in (b'not-json\n', b'{}', b'[]\n', b'{}\n',
                       ndjson([dict(type='result', session_id='someone-else', result='bad')])):
            (self.source / 'stdout.ndjson').write_bytes(stream)
            self.job['rounds'][0]['evidence_sha256']['stdout'] = evidence.digest(stream)
            with self.assertRaises(evidence.EvidenceError):
                self.export()
            self.assertFalse(self.dest.exists())

    def test_raw_prompt_or_sealed_stream_changes_refused(self):
        for name in ('prompt.md', 'stdout.ndjson'):
            path = self.source / name
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            with self.assertRaises(evidence.EvidenceError):
                self.export()
            path.write_bytes(original)

    def test_changed_body_and_rehashed_manifest_fail_source_binding(self):
        result = self.export()
        manifest = self.manifest()
        value = '伪造的“大家一致通过”。'.encode()
        (self.dest / 'response-0001.md').write_bytes(value)
        changed = dict(sha256=evidence.digest(value), bytes=len(value))
        manifest['files']['response-0001.md'] = changed
        manifest['sources'][0].update(changed)
        self.save_manifest(manifest)
        # Internal consistency alone cannot authenticate a rewritten manifest.
        self.assertTrue(evidence.verify(self.dest)['ok'])
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify(self.dest, expected_sha256=result['provenance_sha256'])
        with self.assertRaises(evidence.EvidenceError):
            self.bound_verify()

    def test_changed_identity_source_location_or_round_refused(self):
        self.export()
        original = self.manifest()
        for mutate in (lambda m: m['recorded_verification'].update(assistant_models=['invented']),
                       lambda m: m['requested'].update(effort='low'),
                       lambda m: m.update(run_token='other-round'),
                       lambda m: m.update(owner='other-owner'),
                       lambda m: m['sources'][0].update(record_byte_start=1)):
            manifest = copy.deepcopy(original)
            mutate(manifest)
            self.save_manifest(manifest)
            with self.assertRaises(evidence.EvidenceError):
                self.bound_verify()

    def test_post_export_source_change_prevents_acceptance(self):
        self.export()
        (self.source / 'stdout.ndjson').write_bytes(b'{}\n')
        with self.assertRaises(evidence.EvidenceError):
            self.bound_verify()

    def test_file_tamper_index_rewrite_and_malformed_manifest(self):
        self.export()
        original = self.manifest()
        path = self.dest / 'response-0001.md'
        path.write_text('changed')
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify(self.dest)
        path.write_bytes(self.original.encode())
        value = b'All reviewers agreed.\n'
        (self.dest / 'INDEX.md').write_bytes(value)
        manifest = copy.deepcopy(original)
        manifest['files']['INDEX.md'] = dict(sha256=evidence.digest(value), bytes=len(value))
        self.save_manifest(manifest)
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify(self.dest)
        for value in ({}, [], {'schema': 1}, dict(original, subject=None)):
            self.save_manifest(value)
            with self.assertRaises(evidence.EvidenceError):
                evidence.verify(self.dest)

    def test_symlink_and_traversal_refused(self):
        self.export()
        path = self.dest / 'response-0001.md'
        path.unlink()
        path.symlink_to(self.source / 'prompt.md')
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify(self.dest)
        for relative in ('../outside', '/absolute', 'nested/../../outside'):
            with self.assertRaises(evidence.EvidenceError):
                evidence.inside(self.dest, relative)

    def test_explicit_size_limit_no_silent_truncation(self):
        with patch.object(evidence, 'MAX_FILE_BYTES', 100):
            with self.assertRaises(evidence.EvidenceError):
                self.export()
        self.assertFalse(self.dest.exists())

    def test_saved_hash_verifies_portable_package_without_raw_stream(self):
        result = self.export()
        (self.source / 'stdout.ndjson').unlink()
        self.assertTrue(evidence.verify(self.dest, expected_sha256=result['provenance_sha256'])['ok'])
        with self.assertRaises(OSError):
            self.bound_verify()


class CommandIntegration(unittest.TestCase):
    def test_worker_seal_owner_gates_and_rejected_accept_keeps_reservation(self):
        fake = Path(__file__).parents[1] / 'skill-verification' / 'fake_claude.py'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / 'state'
            project = root / 'project'
            project.mkdir()
            prompt = root / 'prompt.json'
            prompt.write_text(json.dumps({'tag': 'Original blocker text.\n' + '证据' * 2500}))
            env = dict(os.environ, CLAUDE_CONFIG_DIR=str(root / 'claude'), PYTHONDONTWRITEBYTECODE='1')

            def call(*args, owner='owner-a', ok=True):
                run = subprocess.run([sys.executable, str(SCRIPT), '--state-dir', str(state),
                                      '--owner', owner, '--claude-bin', str(fake), *args],
                                     env=env, capture_output=True, text=True, timeout=25)
                result = json.loads(run.stdout)
                self.assertEqual(run.returncode == 0, ok, result)
                return result

            job = call('start', '--cwd', str(project), '--prompt-file', str(prompt), '--timeout', '15')['job_id']
            try:
                settled = call('wait', job, '--seconds', '12')
                self.assertEqual(settled['phase'], 'awaiting_review', settled)
                record_path = state / 'jobs' / job / 'job.json'
                saved = json.loads(record_path.read_bytes())
                self.assertEqual(len(saved['rounds'][0]['evidence_sha256']['stdout']), 64)
                export_dir = root / 'evidence'
                export_args = ('evidence', job, '--subject', 'fixture commit', '--output-dir', str(export_dir))
                call(*export_args, owner='owner-b', ok=False)
                self.assertFalse(export_dir.exists())
                exported = call(*export_args)
                response = export_dir / 'response-0001.md'
                original = response.read_bytes()
                self.assertGreater(len(original), 4000)
                response.write_bytes(b'changed')
                notes = root / 'accept.md'
                notes.write_text('Original source and independent adjudication are linked here.')
                rejected = call('accept', job, '--notes-file', str(notes), '--evidence-dir', str(export_dir), ok=False)
                self.assertEqual(rejected['error'], 'invalid_review_evidence')
                self.assertEqual(json.loads(record_path.read_bytes())['phase'], 'awaiting_review')
                self.assertFalse((record_path.parent / 'notes' / 'r000-accept.md').exists())
                response.write_bytes(original)
                call('evidence', '--verify-dir', str(export_dir), '--expected-sha256', exported['provenance_sha256'], owner='owner-b')
                call('accept', job, '--notes-file', str(notes), '--evidence-dir', str(export_dir))
                accepted = json.loads(record_path.read_bytes())
                self.assertEqual(accepted['phase'], 'accepted')
                self.assertEqual(accepted['accepted']['review_evidence']['provenance_sha256'], exported['provenance_sha256'])
            finally:
                call('stop', job)


if __name__ == '__main__':
    unittest.main()
