"""Policy-allowlisted extra writable directories on the Windows Codex route."""
import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import remote_codex_task as m

OWNER = '11111111-2222-4333-8444-555555555555'
REQUEST = '7f3a0d8e-7d9f-4f75-8b4c-38c9bf844761'
THREAD = '0a0a0a0a-bbbb-7ccc-8ddd-eeeeeeeeeeee'
SKILLS = 'C:\\Users\\Example\\.codex\\skills'
LOG = 'C:\\Users\\Example\\.codex\\sessions\\rollout.jsonl'


class AddDirs(unittest.TestCase):
    def setUp(self):
        self.policy = {'version': 1, 'sites': {'windows': {
            'host': 'windows-host', 'cwd_roots': ['D:\\'], 'codex_home': 'C:\\Users\\Example\\.codex',
            'models': ['gpt-6-sol'], 'efforts': ['xhigh'], 'sandboxes': ['read-only', 'workspace-write'],
            'windows_sandbox': 'unelevated', 'max_timeout_seconds': 86400, 'add_dirs': [SKILLS]}}}

    def select(self, add_dirs, sandbox='workspace-write'):
        return m.select_site(self.policy, 'windows', 'D:\\work\\repo', 'gpt-6-sol', 'xhigh', sandbox, 900, add_dirs)

    def job(self, add_dirs=(SKILLS,)):
        identity, digest = m.request_identity(OWNER, REQUEST, self.select(list(add_dirs)), 'a' * 64)
        return dict(job='job', **identity, request_digest=digest, token='b' * 64)

    def receipt(self, **extra):
        values = {'session': THREAD, 'turn': REQUEST, 'log': LOG, 'cwd': 'D:\\work\\repo',
                  'model': 'gpt-6-sol', 'effort': 'xhigh', 'sandbox': 'workspace-write',
                  'windows_sandbox': 'unelevated', 'approval_policy': 'never',
                  'cli_version': 'codex-cli 0.156.1', 'codex_path': 'C:\\npm\\codex.js', 'process_pid': 42,
                  'process_start_time': 'time', 'final_text_sha256': hashlib.sha256(b'done').hexdigest(),
                  'add_dirs': [SKILLS.lower() + '\\'], 'writable_roots': [SKILLS], 'network_access': False}
        values.update(extra)
        return values

    def test_extra_directories_stay_inside_policy_roots(self):
        site = self.select([SKILLS + '\\claude-delegate', SKILLS])
        self.assertEqual(site['add_dirs'], sorted([SKILLS, SKILLS + '\\claude-delegate']))
        self.assertEqual(site['add_dir_roots'], [SKILLS])
        self.assertEqual((self.select(None)['add_dirs'], self.select(None)['add_dir_roots']), ([], []))
        for dirs, sandbox, error in (
                (['C:\\Users\\Example\\.codex'], 'workspace-write', 'outside the site allowlist'),
                (['C:\\Users\\Example\\.codex\\skills-other'], 'workspace-write', 'outside the site allowlist'),
                ([SKILLS], 'read-only', 'needs the workspace-write sandbox'),
                (['\\\\server\\share'], 'workspace-write', 'local-drive'),
                ([SKILLS + '\\..\\config'], 'workspace-write', 'refused path segment')):
            with self.assertRaisesRegex(ValueError, error):
                self.select(dirs, sandbox)
        del self.policy['sites']['windows']['add_dirs']
        with self.assertRaisesRegex(ValueError, 'outside the site allowlist'):
            self.select([SKILLS])

    def test_digest_and_packet_bind_extra_directories(self):
        _, plain = m.request_identity(OWNER, REQUEST, self.select(None), 'a' * 64)
        job = self.job()
        self.assertNotEqual(plain, job['request_digest'])
        config = json.loads(base64.b64decode(m.packet_for(job, 'dispatch', b'x')))['config']
        self.assertEqual((config['add_dirs'], config['add_dir_roots']), ([SKILLS], [SKILLS]))
        older = {key: value for key, value in job.items() if key not in ('add_dirs', 'add_dir_roots')}
        config = json.loads(base64.b64decode(m.packet_for(older, 'receipt')))['config']
        self.assertEqual((config['add_dirs'], config['add_dir_roots']), ([], []))

    def test_receipt_must_show_exactly_the_requested_roots_and_no_network(self):
        job = self.job()
        m.validate_completion_receipt(job, self.receipt())
        for patch in ({'writable_roots': []}, {'writable_roots': [SKILLS, 'C:\\Users\\Example']},
                      {'network_access': True}, {'add_dirs': []}):
            with self.assertRaisesRegex(ValueError, 'sandbox_scope_mismatch'):
                m.validate_completion_receipt(job, self.receipt(**patch))
        plain = self.job(())
        m.validate_completion_receipt(plain, self.receipt(add_dirs=[], writable_roots=[]))
        with self.assertRaisesRegex(ValueError, 'sandbox_scope_mismatch'):
            m.validate_completion_receipt(plain, self.receipt(add_dirs=[], writable_roots=[SKILLS]))

    def test_second_parse_must_agree_on_roots_and_network(self):
        old = m.observer.deploy, m.remote
        m.observer.deploy = lambda host: {'node': 'node.exe', 'path': 'agent.cjs'}
        try:
            for roots, network, expected in (([SKILLS], False, 'awaiting_review'),
                                              ([], False, 'incomplete_evidence'),
                                              ([SKILLS], True, 'incomplete_evidence')):
                with tempfile.TemporaryDirectory() as tmp:
                    directory, job = Path(tmp), self.job()
                    (directory / 'stream.ndjson').write_text(
                        json.dumps({'type': 'thread.started', 'thread_id': THREAD}) + '\n'
                        + json.dumps({'type': 'turn.completed'}) + '\n', encoding='utf8')
                    frame = {'protocol': 1, 'token': job['token'], 'session': THREAD, 'turn': REQUEST,
                             'kind': 'state', 'status': 'awaiting_review', 'model': 'gpt-6-sol',
                             'effort': 'xhigh', 'sandbox': 'workspace-write', 'approval_policy': 'never',
                             'network_access': network, 'writable_roots': roots,
                             'cli_version': 'codex-cli 0.156.1', 'final_text': 'done',
                             'final_sha256': hashlib.sha256(b'done').hexdigest(),
                             'source': {'path': LOG, 'bytes': 10, 'sha256': 'c' * 64}}
                    m.remote = lambda host, script, payload=None, timeout=45, line=json.dumps(frame): line
                    state = m.verify_completion(directory, job, {'status': 'completed_claimed', 'cursor': 1,
                                                                 'remote_receipt': self.receipt()})
                    self.assertEqual(state['status'], expected, (roots, network, state.get('error')))
        finally:
            m.observer.deploy, m.remote = old


if __name__ == '__main__':
    unittest.main(verbosity=2)
