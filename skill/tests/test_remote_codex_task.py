import argparse
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import remote_codex_task as m


OWNER = '11111111-2222-4333-8444-555555555555'
REQUEST = '7f3a0d8e-7d9f-4f75-8b4c-38c9bf844761'


class RemoteCodexTaskContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.policy = self.root / 'policy.json'
        self.policy.write_text(json.dumps({
            'version': 1,
            'sites': {'windows': {
                'host': 'windows-host',
                'cwd_roots': ['D:\\work'],
                'codex_home': 'C:\\Users\\Example\\.codex',
                'models': ['gpt-6-astra'],
                'efforts': ['xhigh'],
                'sandboxes': ['read-only', 'workspace-write'],
                'windows_sandbox': 'unelevated',
                'max_timeout_seconds': 1200,
            }},
        }), encoding='utf8')
        os.chmod(self.policy, 0o600)
        self.prompt = self.root / 'prompt.md'
        self.prompt.write_text('中文 prompt ✅\n', encoding='utf8')

    def tearDown(self):
        self.tmp.cleanup()

    def args(self):
        return argparse.Namespace(
            owner=OWNER, request_id=REQUEST, agent='codex', policy=self.policy,
            site='windows', cwd='D:\\work\\repo', prompt_file=self.prompt,
            model='gpt-6-astra', effort='xhigh', sandbox='workspace-write',
            timeout=900, state_dir=self.root / 'state')

    def test_windows_path_and_allowlist_fail_closed(self):
        self.assertEqual(m.validate_windows_path('D:\\work\\repo', 'cwd'), 'D:\\work\\repo')
        for value in ('\\\\server\\share', 'D:\\work\\..\\Windows',
                      'D:\\work\\PROGRA~1', 'D:\\work\\trail. ', 'D:\\work:file'):
            with self.assertRaises(ValueError, msg=value):
                m.validate_windows_path(value, 'cwd')
        self.assertTrue(m.within('D:\\work', 'd:\\WORK\\repo'))
        self.assertFalse(m.within('D:\\work', 'D:\\worker'))

    def test_policy_permissions_and_remote_agent_gate(self):
        _, policy = m.load_policy(self.policy)
        site = m.select_site(policy, 'windows', 'D:\\work\\repo', 'gpt-6-astra',
                             'xhigh', 'workspace-write', 900)
        self.assertEqual(site['host'], 'windows-host')
        os.chmod(self.policy, 0o644)
        with self.assertRaisesRegex(ValueError, 'group/world'):
            m.load_policy(self.policy)
        args = self.args(); args.agent = 'claude'
        with self.assertRaisesRegex(ValueError, 'unsupported_remote_agent'):
            m.dispatch(args)

    def test_carrier_ssh_is_strict_and_separate_from_observer(self):
        carrier = m.ssh_argv('windows-host', 'fixed', carrier=True)
        query = m.ssh_argv('windows-host', 'fixed', carrier=False)
        self.assertIn('StrictHostKeyChecking=yes', carrier)
        self.assertIn('ServerAliveInterval=30', carrier)
        self.assertIn('ServerAliveCountMax=10', carrier)
        self.assertIn('ServerAliveInterval=10', query)
        for host in ('host;whoami', '-oProxyCommand=x', 'user@host'):
            with self.assertRaises(ValueError): m.ssh_argv(host, 'fixed')

    def test_initial_dispatch_status_allows_worker_startup_grace(self):
        directory = self.root / 'job'; directory.mkdir()
        job = dict(job='job', owner=OWNER, request_id=REQUEST, site='windows', host='windows-host',
                   agent='codex', cwd='D:\\work', model='gpt-6-astra', effort='xhigh',
                   sandbox='workspace-write', windows_sandbox='unelevated', timeout_seconds=900,
                   created_at=m.time.time())
        m.save(directory / 'state.json', {'status': 'preparing', 'cursor': 0})
        self.assertEqual(m.public(directory, job)['status'], 'preparing')
        m.save(directory / 'state.json', {'status': 'carrier_aborted_by_local_error', 'cursor': 1,
                                          'carrier_terminated_by_mac': True})
        self.assertEqual(m.public(directory, job)['status'], 'carrier_aborted_by_local_error')

    def test_prompt_is_in_stdin_packet_not_powershell_command(self):
        site = dict(site='windows', host='windows-host', cwd='D:\\work\\repo',
                    codex_home='C:\\Users\\Example\\.codex', model='gpt-6-astra', effort='xhigh',
                    sandbox='workspace-write', timeout_seconds=900,
                    windows_sandbox='unelevated', allowed_roots=['D:\\work'], allow_non_git=False)
        identity, digest = m.request_identity(OWNER, REQUEST, site, m.sha256('中文 prompt ✅'.encode()))
        job = dict(job='job', **identity, request_digest=digest, token='a' * 64)
        packet = json.loads(base64.b64decode(m.packet_for(job, 'dispatch', '中文 prompt ✅'.encode())))
        self.assertEqual(base64.b64decode(packet['prompt_b64']).decode(), '中文 prompt ✅')
        command = "$ErrorActionPreference='Stop'; & 'node.exe' 'runner.cjs'"
        self.assertNotIn('中文', command)
        self.assertNotIn('D:\\work', command)
        with self.assertRaisesRegex(ValueError, 'packet_size_limit'):
            m.packet_for({**job, 'allowed_roots': ['D:\\' + 'x' * 2000] * 1000},
                         'dispatch', '中文 prompt ✅'.encode())

    def test_remote_preserves_bounded_runner_error_code(self):
        old = m.subprocess.run
        frame = json.dumps({'protocol': 1, 'kind': 'error', 'error': 'lock_not_stale'}).encode()
        m.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 2, stdout=frame + b'\n', stderr=b'')
        try:
            with self.assertRaisesRegex(RuntimeError, 'remote_runner_error:lock_not_stale'):
                m.remote('windows-host', 'fixed')
        finally:
            m.subprocess.run = old

    def test_request_digest_binds_policy_controls(self):
        site = dict(site='windows', host='windows-host', cwd='D:\\work\\repo',
                    codex_home='C:\\Users\\Example\\.codex', model='gpt-6-astra', effort='xhigh',
                    sandbox='workspace-write', timeout_seconds=900,
                    windows_sandbox='unelevated', allowed_roots=['D:\\work'], allow_non_git=False)
        _, original = m.request_identity(OWNER, REQUEST, site, 'a' * 64)
        _, non_git = m.request_identity(OWNER, REQUEST, {**site, 'allow_non_git': True}, 'a' * 64)
        _, roots = m.request_identity(OWNER, REQUEST, {**site, 'allowed_roots': ['D:\\other']}, 'a' * 64)
        _, native = m.request_identity(OWNER, REQUEST, {**site, 'windows_sandbox': 'elevated'}, 'a' * 64)
        self.assertNotEqual(original, non_git)
        self.assertNotEqual(original, roots)
        self.assertNotEqual(original, native)

    def test_request_id_is_idempotent_and_conflicts_on_changed_prompt(self):
        calls = []
        old_deploy, old_start = m.deploy_runner, m.start_worker
        m.deploy_runner = lambda host: calls.append(host) or {'node': 'node.exe', 'path': 'runner.cjs', 'sha256': 'f' * 64}
        m.start_worker = lambda args, directory, job: None
        try:
            with contextlib.redirect_stdout(io.StringIO()): m.dispatch(self.args())
            with contextlib.redirect_stdout(io.StringIO()): m.dispatch(self.args())
            self.assertEqual(calls, ['windows-host'])
            self.prompt.write_text('changed', encoding='utf8')
            with self.assertRaisesRegex(ValueError, 'request_conflict'):
                with contextlib.redirect_stdout(io.StringIO()): m.dispatch(self.args())
        finally:
            m.deploy_runner, m.start_worker = old_deploy, old_start

    def test_frame_identity_and_private_stream(self):
        directory = self.root / 'job'; directory.mkdir()
        job = {'token': 'a' * 64, 'request_id': REQUEST}
        state = {'status': 'running', 'cursor': 0}
        with open(directory / 'stream.ndjson', 'ab') as stream:
            frame = {'protocol': 1, 'token': job['token'], 'request_id': REQUEST,
                     'kind': 'codex', 'line_b64': base64.b64encode('秘密输出'.encode()).decode()}
            state = m.apply_frame(directory, job, state, frame, stream)
        self.assertEqual((directory / 'stream.ndjson').read_text().strip(), '秘密输出')
        frame['token'] = 'b' * 64
        with open(directory / 'stream.ndjson', 'ab') as stream:
            with self.assertRaisesRegex(ValueError, 'frame_identity'):
                m.apply_frame(directory, job, state, frame, stream)

    def test_remote_status_whitelist_cannot_bypass_second_verification(self):
        directory = self.root / 'job'; directory.mkdir()
        job = {'token': 'a' * 64, 'request_id': REQUEST}
        state = {'status': 'running', 'cursor': 0}
        receipt = {'request_id': REQUEST, 'status': 'awaiting_review'}
        with open(directory / 'stream.ndjson', 'ab') as stream:
            with self.assertRaisesRegex(ValueError, 'unknown_remote_status'):
                m.apply_frame(directory, job, state, {'protocol': 1, 'token': job['token'],
                    'request_id': REQUEST, 'kind': 'receipt', 'receipt': receipt}, stream)
            accepted = m.apply_frame(directory, job, state, {'protocol': 1, 'token': job['token'],
                'request_id': REQUEST, 'kind': 'receipt',
                'receipt': {'request_id': REQUEST, 'status': 'accepted'}}, stream)
        self.assertEqual(accepted['status'], 'connecting')
        with open(directory / 'stream.ndjson', 'ab') as stream:
            risky = m.apply_frame(directory, job, {**state, 'partial_write_risk': True},
                {'protocol': 1, 'token': job['token'], 'request_id': REQUEST, 'kind': 'receipt',
                 'receipt': {'request_id': REQUEST, 'status': 'running', 'partial_write_risk': False}}, stream)
        self.assertTrue(risky['partial_write_risk'])

    def test_local_stream_requires_one_matching_session_and_completion(self):
        directory = self.root / 'job'; directory.mkdir()
        receipt = {'session': OWNER}
        stream = directory / 'stream.ndjson'
        stream.write_text('\n'.join((json.dumps({'type': 'thread.started', 'thread_id': OWNER}),
                                      json.dumps({'type': 'turn.completed'}))) + '\n', encoding='utf8')
        m.validate_local_stream(directory, receipt)
        stream.write_text('\n'.join((json.dumps({'type': 'thread.started', 'thread_id': OWNER}),
                                      json.dumps({'type': 'thread.started', 'thread_id': OWNER}),
                                      json.dumps({'type': 'turn.completed'}))) + '\n', encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'identity_mismatch'):
            m.validate_local_stream(directory, receipt)

    def test_missing_remote_runtime_fails_cleanly(self):
        directory = self.root / 'job'; directory.mkdir()
        m.save(directory / 'state.json', {'status': 'preparing', 'cursor': 0})
        with self.assertRaisesRegex(ValueError, 'runtime is unavailable'):
            m.query_remote_receipt(directory, {'host': 'windows-host'})

    def test_completion_receipt_must_match_requested_identity_and_controls(self):
        job = {'cwd': 'D:\\work', 'model': 'gpt-6-astra', 'effort': 'xhigh',
               'sandbox': 'workspace-write', 'windows_sandbox': 'unelevated'}
        receipt = {'session': OWNER, 'turn': REQUEST, 'log': 'C:\\log.jsonl', 'cwd': job['cwd'],
                   'model': job['model'], 'effort': job['effort'], 'sandbox': job['sandbox'],
                   'windows_sandbox': job['windows_sandbox'],
                   'approval_policy': 'never', 'cli_version': 'codex-cli 0.155.0',
                   'codex_path': 'C:\\codex.js', 'process_pid': 42, 'process_start_time': 'time',
                   'final_text_sha256': 'a' * 64}
        m.validate_completion_receipt(job, receipt)
        for key, value in (('model', 'other'), ('sandbox', 'read-only'), ('windows_sandbox', 'elevated'),
                           ('approval_policy', 'on-request')):
            with self.assertRaisesRegex(ValueError, 'dispatch_evidence_mismatch'):
                m.validate_completion_receipt(job, {**receipt, key: value})
        with self.assertRaisesRegex(ValueError, 'missing_dispatch_identity'):
            m.validate_completion_receipt(job, {**receipt, 'turn': None})


if __name__ == '__main__': unittest.main()
