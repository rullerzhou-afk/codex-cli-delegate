"""Mac side of the Windows Kimi route: policy, packets, second verification, revisions."""
import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import kimi_backend
import remote_codex_task as m
import remote_kimi

OWNER = '11111111-2222-4333-8444-555555555555'
REQUEST = '7f3a0d8e-7d9f-4f75-8b4c-38c9bf844761'
REVISION = '3c9e2b1a-4d5f-4a6b-9c7d-8e9f0a1b2c3d'
SESSION = 'session_0a0a0a0a-bbbb-4ccc-8ddd-eeeeeeeeeeee'
READ = ['Glob', 'Grep', 'Read', 'ReadMediaFile']
SESSION_DIR = 'C:\\Users\\Example\\.kimi-code\\sessions\\wd_repo_000000000000\\' + SESSION


def sha(data):
    return hashlib.sha256(data).hexdigest()


def turn(marker, text='done', tools=READ, model='k3-256k', effort='max'):
    return [
        {'type': 'profile.bind', 'agentId': 'main', 'activeToolNames': tools},
        {'type': 'turn.prompt', 'agentId': 'main',
         'input': [{'type': 'text', 'text': '[delegation-run: %s]\ntask' % marker}]},
        {'type': 'llm.request', 'agentId': 'main', 'kind': 'loop', 'model': model,
         'modelAlias': 'kimi-code/k3-256k', 'thinkingEffort': effort},
        {'type': 'context.append_loop_event', 'agentId': 'main',
         'event': {'type': 'content.part', 'stepUuid': 's1', 'part': {'type': 'text', 'text': text}}},
        {'type': 'context.append_loop_event', 'agentId': 'main',
         'event': {'type': 'step.end', 'uuid': 's1', 'finishReason': 'end_turn'}},
        {'type': 'turn.ended', 'agentId': 'main', 'reason': 'completed'},
    ]


def encode(rows):
    return b''.join(json.dumps(row, ensure_ascii=False).encode() + b'\n' for row in rows)


def frames(wire, state, session=SESSION, index=None, token='a' * 64, chunk=7):
    base = {'protocol': 1, 'token': token, 'request_id': REQUEST}
    index = index or {'sessionId': session, 'sessionDir': SESSION_DIR.replace('\\', '/'), 'workDir': 'D:\\work\\repo'}
    out = [{**base, 'kind': 'evidence_meta', 'session': session, 'session_dir': SESSION_DIR, 'index': index,
            'state_b64': base64.b64encode(state).decode(), 'wire_bytes': len(wire), 'wire_sha256': sha(wire)}]
    for offset in range(0, len(wire), chunk):
        out.append({**base, 'kind': 'evidence_chunk', 'offset': offset,
                    'data_b64': base64.b64encode(wire[offset:offset + chunk]).decode()})
    return out + [{**base, 'kind': 'evidence_end', 'wire_bytes': len(wire), 'wire_sha256': sha(wire)}]


class RemoteKimiContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.policy = self.root / 'policy.json'
        self.policy.write_text(json.dumps({'version': 1, 'sites': {'windows': {
            'host': 'windows-host', 'cwd_roots': ['D:\\'], 'codex_home': 'C:\\Users\\Example\\.codex',
            'models': ['gpt-6-sol'], 'efforts': ['xhigh'], 'sandboxes': ['read-only'],
            'windows_sandbox': 'unelevated', 'max_timeout_seconds': 86400,
            'kimi': {'kimi_home': 'C:\\Users\\Example\\.kimi-code', 'models': ['kimi-code/k3-256k'],
                     'efforts': ['max'], 'tools': READ + ['Edit', 'Write', 'Bash']},
        }}}), encoding='utf8')
        os.chmod(self.policy, 0o600)
        self.prompt = self.root / 'task.md'
        self.prompt.write_text('请只读检查 ✅\n', encoding='utf8')
        self.old = m.deploy_runner, m.start_worker, m.fetch_kimi_evidence
        m.deploy_runner = lambda host: {'node': 'node.exe', 'path': 'runner.cjs', 'sha256': 'f' * 64}
        m.start_worker = lambda args, directory, job: None

    def tearDown(self):
        m.deploy_runner, m.start_worker, m.fetch_kimi_evidence = self.old
        self.tmp.cleanup()

    def args(self, **extra):
        values = dict(owner=OWNER, request_id=REQUEST, agent='kimi', policy=self.policy, site='windows',
                      cwd='D:\\work\\repo', prompt_file=self.prompt, model=None, effort=None, sandbox=None,
                      kimi_tool=None, timeout=86400, state_dir=self.root / 'state')
        values.update(extra)
        return argparse.Namespace(**values)

    def dispatch(self, **extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            m.dispatch(self.args(**extra))
        return json.loads(output.getvalue())

    def test_policy_fixes_the_profile_and_allowlists_tools(self):
        _, policy = m.load_policy(self.policy)
        site = m.select_kimi_site(policy, 'windows', 'D:\\work\\repo', None, None, None, 60)
        self.assertEqual((site['model'], site['effort'], site['kimi_tools']), (kimi_backend.MODEL, 'max', READ))
        self.assertEqual(m.select_kimi_site(policy, 'windows', 'D:\\work\\repo', None, None, ['Read', 'Bash', 'Read'],
                                            60)['kimi_tools'], ['Bash', 'Read'])
        for model, effort, tools, error in (
                ('kimi-code/k3', None, None, 'fixed'), (None, 'high', None, 'fixed'),
                (None, None, ['FetchURL'], 'not allowlisted'), (None, None, ['Shell'], 'unsupported')):
            with self.assertRaisesRegex(ValueError, error):
                m.select_kimi_site(policy, 'windows', 'D:\\work\\repo', model, effort, tools, 60)
        del policy['sites']['windows']['kimi']
        with self.assertRaisesRegex(ValueError, 'does not allowlist Kimi'):
            m.select_kimi_site(policy, 'windows', 'D:\\work\\repo', None, None, None, 60)

    def test_dispatch_marks_the_prompt_and_is_idempotent(self):
        first = self.dispatch()
        again = self.dispatch()
        self.assertEqual(first['job'], again['job'])
        self.assertEqual((first['agent'], first['kimi_tools'], first['session_id']), ('kimi', READ, None))
        directory = self.root / 'state' / first['job']
        job = json.loads((directory / 'job.json').read_text())
        marker = remote_kimi.run_marker(REQUEST)
        self.assertEqual(job['run_marker'], marker)
        self.assertTrue((directory / 'prompt.bin').read_text().startswith('[delegation-run: %s]\n' % marker))
        profile = (directory / 'kimi-agent.md').read_bytes()
        self.assertEqual(job['kimi_profile_sha256'], sha(profile))
        self.assertEqual(profile.decode(), kimi_backend.agent_profile(READ))
        with self.assertRaisesRegex(ValueError, 'request_conflict'):
            self.dispatch(kimi_tool=['Read', 'Edit'])
        with self.assertRaisesRegex(ValueError, 'sandbox applies only to Codex'):
            self.dispatch(request_id=REVISION, sandbox='read-only')
        with self.assertRaisesRegex(ValueError, 'applies only to Kimi'):
            self.dispatch(request_id=REVISION, agent='codex', kimi_tool=['Read'])

    def test_task_text_is_limited_by_the_command_line(self):
        self.prompt.write_text('x' * remote_kimi.MAX_PROMPT_UNITS, encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'command-line argument'):
            self.dispatch()

    def test_packets_carry_only_the_agent_controls(self):
        job = json.loads((self.root / 'state' / self.dispatch()['job'] / 'job.json').read_text())
        outer = json.loads(base64.b64decode(m.packet_for(job, 'dispatch', b'x', profile=b'p')))
        self.assertEqual(outer['config']['kimi_tools'], READ)
        self.assertNotIn('sandbox', outer['config'])
        self.assertEqual(base64.b64decode(outer['kimi_profile_b64']), b'p')
        evidence = json.loads(base64.b64decode(m.packet_for(job, 'evidence', evidence_session=SESSION)))
        self.assertEqual((evidence['config']['action'], evidence['config']['evidence_session']),
                         ('evidence', SESSION))

    def test_stream_frames_must_match_the_job_agent(self):
        directory = self.root / 'job'; directory.mkdir()
        base = {'protocol': 1, 'token': 'a' * 64, 'request_id': REQUEST}
        line = base64.b64encode(b'{"role":"assistant","content":"x"}').decode()
        with open(directory / 'stream.ndjson', 'ab') as stream:
            kimi = {'token': 'a' * 64, 'request_id': REQUEST, 'agent': 'kimi'}
            m.apply_frame(directory, kimi, {}, {**base, 'kind': 'kimi', 'line_b64': line}, stream)
            with self.assertRaisesRegex(ValueError, 'frame_agent_mismatch'):
                m.apply_frame(directory, kimi, {}, {**base, 'kind': 'codex', 'line_b64': line}, stream)
            with self.assertRaisesRegex(ValueError, 'frame_agent_mismatch'):
                m.apply_frame(directory, {**kimi, 'agent': 'codex'}, {},
                              {**base, 'kind': 'kimi', 'line_b64': line}, stream)

    def test_evidence_frames_are_reassembled_and_checked(self):
        wire, state = encode(turn('m' * 32)), b'{}'
        self.assertEqual(remote_kimi.assemble(frames(wire, state))['wire'], wire)
        broken = frames(wire, state)
        broken[2]['offset'] += 1
        with self.assertRaisesRegex(ValueError, 'evidence_chunk_order'):
            remote_kimi.assemble(broken)
        tampered = frames(wire, state)
        tampered[1]['data_b64'] = base64.b64encode(b'X' * 7).decode()
        with self.assertRaisesRegex(ValueError, 'evidence_digest'):
            remote_kimi.assemble(tampered)

    def complete(self, text='答复 ✅', rows=None, state_patch=None, receipt_patch=None):
        """Dispatch, then feed a completed stream and native evidence through verify_completion."""
        directory = self.root / 'state' / self.dispatch()['job']
        job = json.loads((directory / 'job.json').read_text())
        marker = job['run_marker']
        wire = encode(rows if rows is not None else turn(marker, text))
        state = json.dumps({'id': SESSION, 'cwd': 'D:\\work\\repo', 'lastTurnReason': 'completed',
                            **(state_patch or {})}).encode()
        stream = [{'role': 'meta', 'type': 'system.version', 'version': '0.42.0'},
                  {'role': 'assistant', 'content': text},
                  {'role': 'meta', 'type': 'session.resume_hint', 'session_id': SESSION}]
        (directory / 'stream.ndjson').write_bytes(encode(stream))
        receipt = {'status': 'completed_claimed', 'session': SESSION, 'session_dir': SESSION_DIR,
                   'wire_bytes': len(wire), 'wire_sha256': sha(wire), 'state_sha256': sha(state),
                   'final_text_sha256': sha(text.encode()), 'cwd': 'D:\\work\\repo', 'model': job['model'],
                   'effort': job['effort'], 'kimi_tools': job['kimi_tools'], 'cli_version': '0.42.0',
                   'kimi_path': 'C:\\npm\\main.mjs', 'process_pid': 42, 'process_start_time': 't',
                   **(receipt_patch or {})}
        m.fetch_kimi_evidence = lambda job_, session: remote_kimi.assemble(frames(wire, state, session))
        result = m.verify_completion(directory, job, {'status': 'completed_claimed', 'cursor': 3,
                                                      'remote_receipt': receipt})
        return directory, job, result

    def test_second_verification_accepts_a_clean_turn(self):
        directory, job, result = self.complete()
        self.assertEqual(result['status'], 'awaiting_review', result)
        self.assertEqual((directory / 'final.md').read_text(encoding='utf8'), '答复 ✅')
        self.assertEqual(result['kimi_verified']['session'], SESSION)
        self.assertEqual(result['kimi_verified']['wire_sha256'], sha((directory / 'kimi-wire.jsonl').read_bytes()))
        self.assertEqual(os.stat(directory / 'kimi-wire.jsonl').st_mode & 0o777, 0o600)
        m.save(directory / 'state.json', {**result, 'cursor': 4})
        notes = self.root / 'notes.md'; notes.write_text('checked', encoding='utf8')
        m.accept(directory, job, notes)
        self.assertEqual(json.loads((directory / 'state.json').read_text())['status'], 'accepted')

    def test_second_verification_rejects_mismatched_evidence(self):
        marker = remote_kimi.run_marker(REQUEST)
        for rows, state_patch, receipt_patch, expected in (
                (turn(marker, tools=READ + ['Bash']), None, None, 'tool_profile_mismatch'),
                (turn(marker, model='k3'), None, None, 'model_unverified'),
                (turn(marker, effort='high'), None, None, 'effort_unverified'),
                (turn(marker, text='other'), None, None, 'final_report_mismatch'),
                (None, {'cwd': 'D:\\Else'}, None, 'session_mismatch'),
                (None, None, {'kimi_tools': READ + ['Bash']}, 'dispatch_evidence_mismatch'),
                (None, None, {'wire_sha256': 'e' * 64}, 'changed after completion')):
            self.tearDown(); self.setUp()
            _, _, result = self.complete(rows=rows, state_patch=state_patch, receipt_patch=receipt_patch)
            self.assertEqual(result['status'], 'incomplete_evidence', expected)
            self.assertIn(expected, json.dumps(result, ensure_ascii=False), expected)

    def test_revision_continues_the_verified_session_as_a_linked_job(self):
        directory, parent, result = self.complete()
        m.save(directory / 'state.json', {**result, 'cursor': 4})
        output = io.StringIO()
        revise_args = self.args(request_id=REVISION, job=parent['job'], timeout=None)
        with contextlib.redirect_stdout(output):
            m.revise(revise_args)
        child = json.loads(output.getvalue())
        self.assertEqual((child['session_id'], child['parent_job'], child['round']), (SESSION, parent['job'], 1))
        stored = json.loads((self.root / 'state' / child['job'] / 'job.json').read_text())
        self.assertEqual((stored['baseline_bytes'], stored['baseline_sha256']),
                         (result['kimi_verified']['wire_bytes'], result['kimi_verified']['wire_sha256']))
        self.assertIsNone(stored['kimi_profile_sha256'])
        self.assertFalse((self.root / 'state' / child['job'] / 'kimi-agent.md').exists())
        self.assertEqual(json.loads((directory / 'state.json').read_text())['status'], 'superseded')
        with contextlib.redirect_stdout(io.StringIO()):
            m.revise(revise_args)  # the same request is idempotent
        with self.assertRaisesRegex(ValueError, 'latest verified round'):
            m.revise(self.args(request_id=REQUEST.replace('7f3a', '1111'), job=parent['job'], timeout=None))

    def test_revision_needs_a_verified_kimi_session(self):
        job = self.dispatch()
        with self.assertRaisesRegex(ValueError, 'no verified native Kimi session'):
            m.revise(self.args(request_id=REVISION, job=job['job'], timeout=None))

    def test_revision_baseline_must_be_intact(self):
        _, parent, result = self.complete()
        job = {**parent, 'session_id': SESSION, 'baseline_bytes': result['kimi_verified']['wire_bytes'],
               'baseline_sha256': 'e' * 64}
        wire = (self.root / 'state' / parent['job'] / 'kimi-wire.jsonl').read_bytes()
        more = wire + encode(turn(job['run_marker'], text='later'))
        evidence = remote_kimi.assemble(frames(more, json.dumps(
            {'id': SESSION, 'cwd': 'D:\\work\\repo', 'lastTurnReason': 'completed'}).encode()))
        receipt = {'session': SESSION, 'session_dir': SESSION_DIR, 'wire_bytes': len(more),
                   'wire_sha256': sha(more)}
        with self.assertRaisesRegex(ValueError, 'baseline changed'):
            remote_kimi.verify(job, receipt, 'later', evidence)
        job['baseline_sha256'] = sha(wire)
        reasons, _, _ = remote_kimi.verify({**job, 'run_marker': job['run_marker']}, receipt, 'later', evidence)
        self.assertEqual(reasons, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
