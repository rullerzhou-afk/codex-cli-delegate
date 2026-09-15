"""Independent deterministic tests of failure paths; no real processes signalled."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import sys
sys.path.insert(0, str(Path(os.environ['DELEGATE_SCRIPT']).resolve().parent))
# Canonical import: recovery/identity moved into extracted modules that resolve
# claude_task by its canonical name, so an under-test copy would miss patches.
import claude_task as ct  # noqa: E402


class Guards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = ct.Context(self.tmp.name, owner='owner-a', claude_bin='/bin/false')
        self.job_id = str(uuid.uuid4())
        self.job = {'job_id': self.job_id, 'owner': 'owner-a', 'phase': 'running',
                    'current_round': 0, 'stop_requested': False,
                    'rounds': [{'round': 0, 'finalized': False, 'worker': {'pid': 99999999},
                                'claude': None, 'claude_launch_pending': False}]}
        Path(self.ctx.job_dir(self.job_id)).mkdir(parents=True)
        self.ctx.save(self.job)

    def test_unverifiable_stop_keeps_reservation(self):
        with patch.object(ct, 'identity_state', side_effect=lambda record: 'unverifiable' if record else 'unknown'), \
             patch.object(ct, 'terminate_recorded', return_value={'state': 'unverifiable', 'signalled': False}):
            result = ct.cmd_stop(self.ctx, SimpleNamespace(job=self.job_id))
        self.assertFalse(result['stopped'])
        self.assertFalse(result['reservation_released'])
        self.assertEqual(result['phase'], 'needs_attention')

    def test_unregistered_child_is_not_cleared_by_stop_flag(self):
        self.job['rounds'][0]['claude_launch_pending'] = True
        self.ctx.save(self.job)
        with patch.object(ct, 'identity_state', side_effect=lambda record: 'exited' if record else 'unknown'), \
             patch.object(ct, 'terminate_recorded', return_value={'state': 'exited', 'signalled': False}):
            result = ct.cmd_stop(self.ctx, SimpleNamespace(job=self.job_id))
        self.assertFalse(result['stopped'])
        self.assertFalse(result['reservation_released'])
        self.assertIn('unregistered_launch', json.dumps(result['remaining']))

    def test_duplicate_worker_cannot_poison_current_round(self):
        args = SimpleNamespace(job=self.job_id, round=0)
        with patch.object(ct, 'worker_run', side_effect=ct.CliError('claim_consumed', 'already launched')), \
             patch.object(ct, 'finalize_worker_failure') as finalize:
            result = ct.cmd_worker(self.ctx, args)
        self.assertFalse(result['ok'])
        finalize.assert_not_called()
        self.assertEqual(self.ctx.load(self.job_id)['phase'], 'running')

    def test_recovery_refuses_a_possible_live_process_before_dispatch(self):
        self.job['phase'] = 'needs_attention'
        self.ctx.save(self.job)
        prompt = Path(self.tmp.name) / 'recover.txt'; prompt.write_text('Continue after inspection')
        with patch.object(ct, 'identity_state', side_effect=lambda record: 'alive' if record else 'unknown'), \
             patch.object(ct, 'launch_transaction') as launch:
            with self.assertRaises(ct.CliError) as error:
                ct.cmd_revise(self.ctx, SimpleNamespace(job=self.job_id, prompt_file=str(prompt), recover=True))
        self.assertEqual(error.exception.code, 'live_process')
        launch.assert_not_called()

    def test_wait_with_timeout_terminates_only_the_recorded_process(self):
        class FakeProc:
            def __init__(self):
                self.calls = 0
            def wait(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired('fake', timeout)
                return 0

        class FakeMonitor:
            def tick(self, complete=False):
                pass

        events = []
        record = {'pid': 4242, 'run_token': 't', 'identity_verified': True}
        clock = iter([0, 5])
        with patch.object(ct.delegate_process, 'terminate_recorded',
                          return_value={'state': 'exited', 'signalled': True}) as term, \
             patch.object(ct.delegate_process.time, 'monotonic',
                          side_effect=lambda: next(clock, 10 ** 9)):
            exit_code, timed_out = ct.delegate_process.wait_with_timeout(
                FakeProc(), 1, FakeMonitor(), record,
                on_event=lambda name, **fields: events.append(name))
        self.assertTrue(timed_out)
        term.assert_called_once_with(record)
        self.assertEqual(exit_code, 0)
        self.assertIn('timeout', events)

    def test_stop_transition_phases_and_accepted_semantics(self):
        blocker = [{'round': 0, 'process': 'worker', 'state': 'unverifiable'}]
        accepted = {'phase': 'accepted', 'owner': 'o', 'rounds': [{'round': 0, 'finalized': True}]}
        ct.delegate_recovery.stop_transition(accepted, blocker, [])
        self.assertEqual(accepted['phase'], 'accepted')
        self.assertNotIn('accepted', ct.RESERVING_PHASES)
        self.assertEqual(accepted['process_state'], 'stop_incomplete')
        self.assertNotIn('reservation retained', accepted['attention']['detail'])
        self.assertFalse(accepted['stopped']['complete'])

        reserving = {'phase': 'awaiting_review', 'owner': 'o', 'rounds': [{'round': 0, 'finalized': True}]}
        ct.delegate_recovery.stop_transition(reserving, blocker, [])
        self.assertEqual(reserving['phase'], 'needs_attention')
        self.assertIn('reservation retained', reserving['attention']['detail'])

        running = {'phase': 'running', 'owner': 'o', 'rounds': [{'round': 0, 'finalized': False, 'status': 'running'}]}
        ct.delegate_recovery.stop_transition(running, [], [{'process': 'worker'}])
        self.assertEqual(running['phase'], 'stopped')
        self.assertEqual(running['process_state'], 'stopped')
        self.assertTrue(running['rounds'][0]['finalized'])
        self.assertTrue(running['stopped']['complete'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
