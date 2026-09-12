"""Independent deterministic tests of failure paths; no real processes signalled."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

spec = importlib.util.spec_from_file_location('delegate_under_test', os.environ['DELEGATE_SCRIPT'])
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)


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


if __name__ == '__main__':
    unittest.main(verbosity=2)
