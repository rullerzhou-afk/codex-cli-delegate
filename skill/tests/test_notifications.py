import json
from concurrent.futures import ThreadPoolExecutor
import time
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import claude_task as ct
import delegate_notify as dn


class Notifications(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx = ct.Context(self.tmp.name, owner='notification-fixture')
        self.job = dict(job_id=str(uuid.uuid4()), owner=self.ctx.owner(), current_round=0,
                        phase='running', timeout=20, backend='claude', cwd='/private-example',
                        final_report='SECRET REPORT', rounds=[dict(run_token='round-token', finalized=False)])
        Path(self.ctx.job_dir(self.job['job_id'])).mkdir()
        self.path = dn.location(self.ctx, self.job)
        self.path.parent.mkdir(parents=True)
        self.ctx.save(self.job)
        ct.write_json(str(self.path), dict(enabled=True, state='watching', generation='generation',
                                          round=0, run_token='round-token', deliveries={}))
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def sender(self, *args):
        self.sent.append(args)
        return {'state': 'submitted'}

    def tick(self, **kwargs):
        return dn.tick(self.ctx, self.job['job_id'], 0, 'generation', sender=self.sender, reconcile=False, **kwargs)

    def terminal(self, phase='awaiting_review', verified=True):
        self.job['phase'] = phase
        self.job['rounds'][0].update(finalized=True, verification={'ok': verified})
        self.ctx.save(self.job)

    def test_complete_is_review_not_accept_and_deduplicated(self):
        self.terminal()
        self.assertTrue(self.tick())
        self.assertTrue(self.tick())
        self.assertEqual(len(self.sent), 1)
        self.assertIn('等待验收', self.sent[0][0])
        self.assertNotIn('SECRET', str(self.sent))
        self.assertNotIn('/private-example', str(self.sent))
        self.assertEqual(self.ctx.load_owned(self.job['job_id'])['phase'], 'awaiting_review')

    def test_failed_and_unverified_never_announce_success(self):
        for phase in ('failed', 'interrupted', 'needs_attention', 'awaiting_review'):
            with self.subTest(phase=phase):
                self.terminal(phase, False)
                event, done = dn.event_for(self.job, {}, {})
                self.assertTrue(done)
                self.assertEqual(event[1], '需要检查')

    def test_plain_progress_and_recoverable_errors_are_silent(self):
        ct.write_json(str(self.path.parent / 'monitor.json'), {'events': [
            {'kind': 'error', 'error': {'message': 'SECRET ERROR'}},
            {'kind': 'checkpoint', 'comparison': {'assessment': 'progressing'}}]})
        self.assertFalse(self.tick())
        self.assertEqual(self.sent, [])

    def test_quota_and_no_progress_each_once_then_completion(self):
        ct.write_json(str(self.path.parent / 'monitor.json'), {'events': [
            {'kind': 'quota_pause'}, {'kind': 'checkpoint', 'comparison': {'assessment': 'no_observed_progress'}}]})
        for _ in range(4): self.assertFalse(self.tick())
        self.assertEqual(len(self.sent), 2)
        self.terminal()
        self.assertTrue(self.tick())
        self.assertEqual(len(self.sent), 3)

    def test_stop_accept_disable_and_superseded_round_are_silent(self):
        for phase in ('stopped', 'accepted'):
            self.job['phase'] = phase
            self.ctx.save(self.job)
            self.assertTrue(self.tick())
        self.job['phase'] = 'running'
        self.ctx.save(self.job)
        dn.arm(self.ctx, self.job['job_id'], False)
        self.assertTrue(self.tick())
        data = dn.read(self.path); data['enabled'] = True
        ct.write_json(str(self.path), data)
        self.job['current_round'] = 1
        self.job['rounds'].append({'run_token': 'new-token'})
        self.ctx.save(self.job)
        self.assertTrue(self.tick())
        self.assertEqual(self.sent, [])
        self.assertEqual(dn.read(self.path)['state'], 'superseded')

    @unittest.skipUnless(sys.platform == 'darwin', 'native detached watcher uses macOS')
    def test_concurrent_arm_has_one_watcher_and_disable_stops_it(self):
        self.job['rounds'][0]['worker'] = ct.capture_identity(os.getpid(), '')
        self.ctx.save(self.job)
        self.path.unlink()
        try:
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda _: dn.arm(self.ctx, self.job['job_id'], expected_round=0), range(2)))
            self.assertTrue(all(r['ok'] for r in results))
            data = dn.read(self.path)
            worker = data['worker']
            self.assertEqual(ct.identity_state(worker), 'alive')
            again = dn.arm(self.ctx, self.job['job_id'], expected_round=0)
            self.assertEqual(dn.read(self.path)['worker']['pid'], worker['pid'])
            self.assertIn(again['notification']['state'], ('starting', 'watching'))
            self.assertEqual(data['deliveries'], {})
        finally:
            dn.arm(self.ctx, self.job['job_id'], False)
            data = dn.read(self.path)
            deadline = time.monotonic() + 5
            while ct.identity_state(data.get('worker')) == 'alive' and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertIn(ct.identity_state(data.get('worker')), ct.STATE_GONE)

    def test_stale_subscription_cannot_attach_to_new_work(self):
        with patch.object(dn.sys, 'platform', 'darwin'), self.assertRaises(ct.CliError):
            dn.arm(self.ctx, self.job['job_id'], expected_round=1)

    def test_owner_and_generation_guard(self):
        with self.assertRaises(ct.CliError):
            dn.arm(ct.Context(self.tmp.name, owner='wrong-owner'), self.job['job_id'], False)
        self.terminal()
        self.assertTrue(dn.tick(self.ctx, self.job['job_id'], 0, 'foreign-generation', self.sender))
        self.assertEqual(self.sent, [])

    def test_crash_during_submission_never_retries_automatically(self):
        self.terminal()
        def crashed(*args): raise RuntimeError('simulated process exit')
        with self.assertRaises(RuntimeError):
            dn.tick(self.ctx, self.job['job_id'], 0, 'generation', crashed, reconcile=False)
        self.assertEqual(dn.read(self.path)['deliveries']['terminal']['state'], 'submitting')
        self.tick()
        self.assertEqual(self.sent, [])

    def test_delivery_failure_persisted_without_replaying(self):
        self.terminal()
        dn.tick(self.ctx, self.job['job_id'], 0, 'generation', lambda *a: {'state': 'failed'}, reconcile=False)
        self.assertEqual(dn.read(self.path)['deliveries']['terminal']['state'], 'failed')
        self.tick()
        self.assertEqual(self.sent, [])

    def test_disabling_during_os_call_does_not_reenable(self):
        self.terminal()
        def disable(*args):
            dn.arm(self.ctx, self.job['job_id'], False)
            return {'state': 'submitted'}
        dn.tick(self.ctx, self.job['job_id'], 0, 'generation', disable, reconcile=False)
        self.assertFalse(dn.read(self.path)['enabled'])
        self.assertEqual(dn.read(self.path)['state'], 'disabled')

    def test_timeout_not_reported_as_job_failure(self):
        self.assertTrue(self.tick(expired=True))
        self.assertIn('监控已到时', self.sent[0][0])
        self.assertEqual(self.ctx.load_owned(self.job['job_id'])['phase'], 'running')

    def test_native_delivery_uses_literal_arguments_and_bounded_timeout(self):
        hostile = '\" & do shell script \"touch /tmp/not-allowed\"'
        with patch.object(dn.sys, 'platform', 'darwin'), patch.object(dn.subprocess, 'run') as run:
            run.return_value.returncode = 0
            self.assertEqual(dn.send(hostile, 'body', 'subtitle')['state'], 'submitted')
            args, kwargs = run.call_args
            self.assertEqual(args[0][-3:], [hostile, 'body', 'subtitle'])
            self.assertNotIn(hostile, args[0][2])
            self.assertFalse(kwargs.get('shell', False))
            self.assertEqual(kwargs['timeout'], 10)
            run.side_effect = subprocess.TimeoutExpired('osascript', 10)
            self.assertEqual(dn.send('a', 'b', 'c')['state'], 'uncertain')

    def test_unsupported_platform_does_not_arm(self):
        with patch.object(dn.sys, 'platform', 'linux'), self.assertRaises(ct.CliError):
            dn.arm(self.ctx, self.job['job_id'])

    def test_spawn_failure_is_visible_and_does_not_affect_job(self):
        data = dn.read(self.path); data['enabled'] = False
        ct.write_json(str(self.path), data)
        with patch.object(dn.sys, 'platform', 'darwin'), patch.object(dn.subprocess, 'Popen', side_effect=OSError(11, 'fixture')):
            with self.assertRaises(ct.CliError): dn.arm(self.ctx, self.job['job_id'])
        self.assertEqual(dn.read(self.path)['reason'], 'watcher_spawn_failed')
        self.assertEqual(self.ctx.load_owned(self.job['job_id'])['phase'], 'running')


if __name__ == '__main__':
    unittest.main()
