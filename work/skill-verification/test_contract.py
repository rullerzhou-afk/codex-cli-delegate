"""Codex's independent black-box tests. All Claude responses are simulated."""
import concurrent.futures
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

SCRIPT = Path(os.environ['DELEGATE_SCRIPT']).resolve()
FAKE = Path(__file__).with_name('fake_claude.py').resolve()


class Contract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='codex-claude-contract-')
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.root / 'claude'), PYTHONDONTWRITEBYTECODE='1')
        self.jobs = []

    def call(self, *args, owner='owner-a', ok=True):
        p = subprocess.run([sys.executable, str(SCRIPT), '--state-dir', str(self.state),
                            '--owner', owner, '--claude-bin', str(FAKE), *args],
                           env=self.env, capture_output=True, text=True, timeout=25)
        try:
            data = json.loads(p.stdout)
        except ValueError:
            self.fail(f'Non-JSON CLI output: {p.stdout[:500]} {p.stderr[:500]}')
        if ok:
            self.assertEqual(p.returncode, 0, data)
            self.assertTrue(data.get('ok'), data)
        else:
            self.assertNotEqual(p.returncode, 0, data)
            self.assertFalse(data.get('ok'), data)
        return data

    def prompt(self, **task):
        path = self.root / (str(uuid.uuid4()) + '.json')
        path.write_text(json.dumps(task))
        return str(path)

    def start(self, name='a', owner='owner-a', cwd=None, timeout=15, revisions=3, **task):
        directory = cwd or self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        data = self.call('start', '--cwd', str(directory), '--prompt-file', self.prompt(**task),
                         '--timeout', str(timeout), '--max-revisions', str(revisions), owner=owner)
        self.jobs.append((data['job_id'], owner))
        return data

    def settled(self, job, owner='owner-a'):
        data = self.call('wait', job, '--seconds', '12', owner=owner)
        self.assertNotIn(data['phase'], ('starting', 'running'), data)
        return data

    def ledger(self):
        p = self.root / 'claude' / 'calls.ndjson'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def tearDown(self):
        for path in (self.state / 'jobs').glob('*/job.json'):
            record = json.loads(path.read_text())
            pair = (record['job_id'], record['owner'])
            if pair not in self.jobs:
                self.jobs.append(pair)
        for job, owner in self.jobs:
            try:
                self.call('stop', job, owner=owner)
            except Exception:
                pass
        self.tmp.cleanup()

    def test_new_and_revision_keep_session_and_max(self):
        a = self.start(tag='round-one')
        first = self.settled(a['job_id'])
        self.assertEqual(first['phase'], 'awaiting_review', first)
        self.assertTrue(first['verified']['ok'], first)
        b = self.call('revise', a['job_id'], '--prompt-file', self.prompt(tag='round-two'))
        second = self.settled(a['job_id'])
        self.assertEqual(second['phase'], 'awaiting_review', second)
        self.assertEqual(a['session_id'], b['session_id'])
        self.assertEqual(second['final_report'], 'round-two')
        calls = self.ledger()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]['resume'], a['session_id'])
        self.assertTrue(all(x['effort'] == x['env_effort'] == 'max' for x in calls))
        notes = self.root / 'review.txt'; notes.write_text('Independently verified fixture output.')
        accepted = self.call('accept', a['job_id'], '--notes-file', str(notes))
        self.assertEqual(accepted['phase'], 'accepted')

    def test_two_owners_concurrent_and_no_cross_routing(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(self.start, 'a', 'owner-a', tag='A', delay=1)
            fb = pool.submit(self.start, 'b', 'owner-b', tag='B', delay=1)
            a, b = fa.result(), fb.result()
        self.assertNotEqual(a['session_id'], b['session_id'])
        self.assertEqual(self.settled(a['job_id'])['final_report'], 'A')
        self.assertEqual(self.settled(b['job_id'], 'owner-b')['final_report'], 'B')
        self.call('status', a['job_id'], owner='owner-b', ok=False)
        listed = self.call('list', owner='owner-b')['jobs']
        self.assertEqual([x['job_id'] for x in listed], [b['job_id']])

    def test_same_git_checkout_subdirectories_conflict(self):
        repo = self.root / 'repo'; repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True, capture_output=True)
        (repo / 'sub').mkdir()
        a = self.start(cwd=repo, tag='holds-checkout', delay=1)
        self.call('start', '--cwd', str(repo / 'sub'), '--prompt-file', self.prompt(tag='bad'),
                  owner='owner-b', ok=False)
        self.assertEqual(self.settled(a['job_id'])['phase'], 'awaiting_review')
        self.call('start', '--cwd', str(repo), '--prompt-file', self.prompt(tag='still-bad'),
                  owner='owner-b', ok=False)

    def test_wrong_effort_missing_evidence_and_synthetic_rejected(self):
        cases = [dict(actual_effort='high'), dict(scenario='missing_effort'),
                 dict(scenario='missing_transcript'), dict(scenario='synthetic_error'),
                 dict(actual_model='claude-sonnet-5'), dict(scenario='nested_effort'),
                 dict(scenario='missing_result_fields')]
        for i, case in enumerate(cases):
            with self.subTest(case=case):
                a = self.start(str(i), **case)
                status = self.settled(a['job_id'])
                self.assertIn(status['phase'], ('failed', 'needs_attention'), status)
                self.assertFalse(status['verified']['ok'])
                notes = self.root / 'no.txt'; notes.write_text('Cannot accept.')
                self.call('accept', a['job_id'], '--notes-file', str(notes), ok=False)

    def test_plain_parent_and_nested_git_checkout_conflict(self):
        parent = self.root / 'plain'; parent.mkdir()
        nested = parent / 'repo'; nested.mkdir()
        subprocess.run(['git', 'init', '-q', str(nested)], check=True, capture_output=True)
        a = self.start(cwd=parent, tag='holds-parent', delay=1)
        self.call('start', '--cwd', str(nested), '--prompt-file', self.prompt(tag='bad'),
                  owner='owner-b', ok=False)

    def test_normal_report_mentioning_api_error_is_not_a_refusal(self):
        a = self.start(tag='Fixed API error handling and a rate limit test')
        status = self.settled(a['job_id'])
        self.assertEqual(status['phase'], 'awaiting_review', status)

    def test_revision_limit_and_wait_cap(self):
        a = self.start(revisions=0, tag='done')
        self.settled(a['job_id'])
        self.call('revise', a['job_id'], '--prompt-file', self.prompt(tag='extra'), ok=False)
        self.assertEqual(len(self.ledger()), 1)
        result = self.call('wait', a['job_id'], '--seconds', '999')
        self.assertTrue(result['wait_capped'])
        self.assertLess(result['waited_seconds'], 5)

    def test_stop_and_timeout(self):
        a = self.start(tag='will-stop', delay=10)
        stopped = self.call('stop', a['job_id'])
        self.assertEqual(stopped['phase'], 'stopped', stopped)
        b = self.start('b', timeout=1, delay=5)
        status = self.settled(b['job_id'])
        self.assertNotEqual(status['phase'], 'awaiting_review', status)
        self.assertIn('timeout', str(status['verified']['reasons']))

    def test_worker_death_does_not_allow_duplicate_claude(self):
        a = self.start(tag='orphan', delay=4)
        statefile = self.state / 'jobs' / a['job_id'] / 'job.json'
        deadline = time.monotonic() + 4
        while True:
            data = json.loads(statefile.read_text())
            record = data['rounds'][data['current_round']]
            if (record.get('claude') or {}).get('identity_verified'):
                break
            self.assertLess(time.monotonic(), deadline, data)
            time.sleep(.05)
        worker = record['worker']
        cmd = subprocess.check_output(['/bin/ps', '-ww', '-p', str(worker['pid']), '-o', 'command='], text=True)
        self.assertIn(worker['run_token'], cmd)
        os.kill(worker['pid'], signal.SIGKILL)
        time.sleep(.15)
        status = self.call('status', a['job_id'])
        self.assertIn(status['phase'], ('needs_attention', 'orphaned'), status)
        self.call('revise', a['job_id'], '--recover', '--prompt-file', self.prompt(tag='duplicate'), ok=False)
        self.assertEqual(len(self.ledger()), 1)

    def test_controller_crash_worker_completes_without_redispatch(self):
        cwd = self.root / 'crash'; cwd.mkdir()
        task = self.prompt(tag='survived-controller', delay=1)
        receipt = self.root / 'receipt.json'
        args = [sys.executable, str(SCRIPT), '--state-dir', str(self.state), '--owner', 'owner-a',
                '--claude-bin', str(FAKE), 'start', '--cwd', str(cwd), '--prompt-file', task, '--timeout', '10']
        controller_code = 'import subprocess,sys,time; from pathlib import Path; p=subprocess.run(sys.argv[2:],capture_output=True,text=True); Path(sys.argv[1]).write_text(p.stdout); time.sleep(20)'
        controller = subprocess.Popen([sys.executable, '-c', controller_code, str(receipt), *args], env=self.env)
        try:
            deadline = time.monotonic() + 8
            while not receipt.exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.05)
            a = json.loads(receipt.read_text()); self.assertTrue(a['ok'], a)
            self.jobs.append((a['job_id'], 'owner-a'))
            controller.kill(); controller.wait(timeout=5)
            status = self.settled(a['job_id'])
            self.assertEqual(status['phase'], 'awaiting_review', status)
            self.assertEqual(status['final_report'], 'survived-controller')
            self.assertEqual(len(self.ledger()), 1)
        finally:
            if controller.poll() is None:
                controller.kill(); controller.wait(timeout=5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
