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

    def enable_wake(self):
        self.ctx = ct.Context(self.tmp.name, owner=str(uuid.uuid4()))
        self.job['owner'] = self.ctx.owner()
        self.ctx.save(self.job)
        data = dn.read(self.path)
        data.update(wake_codex=True, codex_bin='/fixture/codex')
        ct.write_json(str(self.path), data)
        self.queued = []

    def queuer(self, binary, owner, message):
        self.queued.append((binary, owner, message))
        return {'state': 'queued', 'queued_message_id': str(uuid.uuid4()), 'target': owner}

    def test_wake_terminal_once_to_original_owner_without_output(self):
        self.enable_wake()
        self.terminal()
        for _ in range(3): self.assertTrue(self.tick(queuer=self.queuer))
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.queued[0][1], self.job['owner'])
        self.assertIn(self.job['job_id'], self.queued[0][2])
        self.assertNotIn('SECRET', self.queued[0][2])
        self.assertEqual(dn.read(self.path)['wake']['state'], 'queued')
        self.assertEqual(self.ctx.load_owned(self.job['job_id'])['phase'], 'awaiting_review')

    def test_running_errors_progress_and_expiry_do_not_wake(self):
        self.enable_wake()
        ct.write_json(str(self.path.parent / 'monitor.json'), {'events': [
            {'kind': 'error'}, {'kind': 'quota_pause'},
            {'kind': 'checkpoint', 'comparison': {'assessment': 'no_observed_progress'}}]})
        for _ in range(4): self.tick(queuer=self.queuer)
        self.tick(expired=True, queuer=self.queuer)
        self.assertEqual(self.queued, [])

    def test_failure_wakes_for_inspection_without_automatic_retry(self):
        self.enable_wake()
        self.terminal('needs_attention', False)
        self.tick(queuer=self.queuer)
        self.assertEqual(len(self.queued), 1)
        self.assertIn('不绕过', self.queued[0][2])
        self.assertEqual(self.ctx.load_owned(self.job['job_id'])['phase'], 'needs_attention')

    def test_old_os_receipt_does_not_prevent_first_wake(self):
        self.enable_wake()
        self.terminal()
        data = dn.read(self.path); data['deliveries']['terminal'] = {'state': 'submitted'}
        ct.write_json(str(self.path), data)
        self.tick(queuer=self.queuer)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.queued), 1)

    def test_stopped_disabled_and_superseded_do_not_wake(self):
        self.enable_wake()
        for phase in ('stopped', 'accepted'):
            self.terminal(phase)
            self.tick(queuer=self.queuer)
        self.terminal()
        dn.arm(self.ctx, self.job['job_id'], False)
        self.tick(queuer=self.queuer)
        self.assertEqual(self.queued, [])
        data = dn.read(self.path); data['enabled'] = True
        ct.write_json(str(self.path), data)
        self.job['current_round'] = 1
        self.job['rounds'].append({'run_token': 'new-token'})
        self.ctx.save(self.job)
        self.tick(queuer=self.queuer)
        self.assertEqual(self.queued, [])

    def test_wake_claim_survives_crash_without_duplicate(self):
        self.enable_wake()
        self.terminal()
        def crash(*args): raise RuntimeError('after enqueue but before receipt')
        with self.assertRaises(RuntimeError): self.tick(queuer=crash)
        self.assertEqual(dn.read(self.path)['wake']['state'], 'submitting')
        self.tick(queuer=self.queuer)
        self.assertEqual(self.queued, [])

    def test_turning_off_during_desktop_notification_prevents_wake(self):
        self.enable_wake()
        self.terminal()
        def disable(*args):
            dn.arm(self.ctx, self.job['job_id'], False)
            return {'state': 'submitted'}
        dn.tick(self.ctx, self.job['job_id'], 0, 'generation', sender=disable,
                reconcile=False, queuer=self.queuer)
        self.assertEqual(self.queued, [])

    def test_queue_requires_matching_ack_and_literal_bounded_command(self):
        owner = str(uuid.uuid4()); message_id = str(uuid.uuid4())
        text = 'message " $(touch /tmp/never-execute)'
        with patch.object(dn.subprocess, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = 'Queued message ' + message_id + ' for thread ' + owner + '.\n'
            self.assertEqual(dn.queue_codex('/codex', owner, text)['state'], 'queued')
            args, kwargs = run.call_args
            self.assertEqual(args[0], ['/codex', 'queue', '--thread', owner, '--message', text])
            self.assertNotIn('shell', kwargs); self.assertEqual(kwargs['timeout'], 45)
            run.return_value.stdout = 'Queued message ' + message_id + ' for thread ' + str(uuid.uuid4()) + '.'
            self.assertEqual(dn.queue_codex('/codex', owner, text)['state'], 'uncertain')
            run.side_effect = subprocess.TimeoutExpired('codex', 45)
            self.assertEqual(dn.queue_codex('/codex', owner, text)['state'], 'uncertain')
        with self.assertRaises(ct.CliError): dn.queue_codex('/codex', 'task-title', text)

    def test_wake_failure_is_reported_and_not_retried(self):
        self.enable_wake()
        self.terminal()
        with patch.object(dn, 'send') as alert:
            self.tick(queuer=lambda *args: {'state': 'uncertain', 'reason': 'queue_timeout'})
            self.assertEqual(alert.call_count, 1)
        self.tick(queuer=self.queuer)
        self.assertEqual(self.queued, [])
        self.assertEqual(dn.read(self.path)['wake']['state'], 'uncertain')

    def test_concurrent_completion_ticks_queue_once(self):
        self.enable_wake()
        self.terminal()
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _: self.tick(queuer=self.queuer), range(2)))
        self.assertEqual(len(self.queued), 1)

    def test_enabling_wake_replaces_legacy_watcher_without_touching_job(self):
        self.enable_wake()
        data = dn.read(self.path)
        data.update(wake_codex=False, protocol=1, worker=ct.capture_identity(os.getpid(), ''))
        ct.write_json(str(self.path), data)
        with patch.object(dn, 'codex_preflight', return_value='/fixture/codex'), \
                patch.object(ct, 'identity_state', return_value='alive'), \
                patch.object(dn.subprocess, 'Popen', side_effect=OSError(11, 'fixture')) as spawn:
            with self.assertRaises(ct.CliError):
                dn.arm(self.ctx, self.job['job_id'], expected_round=0, wake_codex=True)
        self.assertEqual(spawn.call_count, 1)
        updated = dn.read(self.path)
        self.assertEqual(updated['protocol'], 2)
        self.assertNotEqual(updated['generation'], data['generation'])
        self.assertEqual(self.ctx.load_owned(self.job['job_id'])['phase'], 'running')

    def test_queue_inflight_does_not_pretend_watcher_finished(self):
        self.enable_wake()
        self.terminal()
        def queue(*args):
            self.assertEqual(dn.read(self.path)['state'], 'watching')
            return self.queuer(*args)
        self.tick(queuer=queue)
        self.assertEqual(dn.read(self.path)['state'], 'finished')

    def test_wake_preflight_rejects_missing_queue_capability(self):
        self.enable_wake()
        with patch.object(dn.shutil, 'which', return_value=None), self.assertRaises(ct.CliError):
            dn.codex_preflight(self.ctx.owner())
        with patch.object(dn.shutil, 'which', return_value='/codex'), patch.object(dn.subprocess, 'run') as run:
            run.return_value.returncode = 0; run.return_value.stdout = 'unrelated help'
            with self.assertRaises(ct.CliError): dn.codex_preflight(self.ctx.owner())
            run.return_value.stdout = 'queue --thread --message'
            self.assertEqual(dn.codex_preflight(self.ctx.owner()), '/codex')

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

    def test_unlimited_watcher_survives_old_wall_clock_cutoff(self):
        self.job['timeout'] = None
        self.ctx.save(self.job)
        data = dn.read(self.path)
        data['worker'] = {'pid': os.getpid()}
        ct.write_json(str(self.path), data)
        with patch.object(dn.time, 'monotonic', side_effect=[0, 2000, 1000000]), \
                patch.object(dn.time, 'sleep'), patch.object(dn, 'tick', side_effect=[False, True]) as tick:
            dn.watch(self.ctx, self.job['job_id'], 0, 'generation')
        self.assertEqual([c.kwargs['expired'] for c in tick.call_args_list], [False, False])

    def test_explicit_watcher_limit_is_still_honored(self):
        data = dn.read(self.path)
        data['worker'] = {'pid': os.getpid()}
        ct.write_json(str(self.path), data)
        with patch.object(dn.time, 'monotonic', side_effect=[0, 141]), \
                patch.object(dn, 'tick', return_value=True) as tick:
            dn.watch(self.ctx, self.job['job_id'], 0, 'generation')
        self.assertTrue(tick.call_args.kwargs['expired'])

    def test_timeout_policy_validation(self):
        for value in (None, 'unlimited'):
            self.assertIsNone(ct.validate_timeout(value))
        self.assertEqual(ct.validate_timeout('1800'), 1800)
        for value in (True, False, 1.5, 0, -1, 'garbage'):
            with self.assertRaises(ct.CliError): ct.validate_timeout(value)

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
