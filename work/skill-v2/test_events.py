import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

SCRIPTS = Path(os.environ['DELEGATE_SCRIPT']).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import claude_events as ce
import claude_task as ct


class Events(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.t = 10000.0
        self.monitor = ce.Monitor(self.root, 'session', self.t, ct.write_json, lambda: self.t)

    def stream(self, event):
        self.monitor.stream(dict(type='stream_event', session_id='session', event=event))

    def test_ten_and_fifteen_are_once_and_do_not_repeat(self):
        m = self.monitor
        self.t += 599; m.tick(); self.assertEqual(m.events, [])
        self.t += 1; m.tick(); self.assertEqual([e['minute'] for e in m.events], [10])
        self.t += 299; m.tick(); self.assertEqual(len(m.events), 1)
        self.t += 1; m.tick(); self.assertEqual([e['minute'] for e in m.events], [10, 15])
        self.assertEqual(m.events[-1]['comparison']['assessment'], 'no_observed_progress')
        self.assertIsNone(m.events[-1]['comparison']['output_tokens_delta'])
        self.t += 3600; m.tick(); self.assertEqual(len(m.events), 2)

    def test_no_checkpoint_on_completion_boundary(self):
        self.t += 900; self.monitor.tick(complete=True)
        self.assertEqual(self.monitor.events, [])

    def test_token_placeholders_deduplicated_and_partial_usage_cumulative(self):
        self.stream({'type':'message_start','message':{'id':'m1','usage':{'input_tokens':10,'output_tokens':9}}})
        for _ in range(3):
            self.monitor.stream({'type':'assistant','message':{'id':'m1','usage':{'input_tokens':10,'output_tokens':9}}})
        self.assertEqual(self.monitor.snapshot()['input_tokens'], 10)
        self.assertIsNone(self.monitor.snapshot()['output_tokens'])
        self.stream({'type':'message_delta','usage':{'output_tokens':50}})
        self.stream({'type':'message_delta','usage':{'output_tokens':60}})
        self.stream({'type':'message_delta','usage':{'output_tokens':60}})
        self.assertEqual(self.monitor.snapshot()['output_tokens'], 60)
        self.stream({'type':'message_start','message':{'id':'m2','usage':{'input_tokens':20,'cache_read_input_tokens':100}}})
        self.stream({'type':'message_delta','usage':{'output_tokens':7}})
        self.assertEqual(self.monitor.snapshot()['input_tokens'], 130)
        self.assertEqual(self.monitor.snapshot()['output_tokens'], 67)
        self.monitor.stream({'type':'result','usage':{'output_tokens':80}})
        self.assertEqual(self.monitor.snapshot()['output_tokens'], 80)

    def test_stream_content_progress_even_when_token_counter_missing(self):
        self.t += 600; self.monitor.tick()
        self.t += 100; self.stream({'type':'content_block_delta','delta':{'type':'thinking_delta','thinking':'PRIVATE CONTENT'}})
        self.t += 200; self.monitor.tick()
        self.assertEqual(self.monitor.events[-1]['comparison']['assessment'], 'progressing')
        self.assertNotIn('PRIVATE CONTENT', (self.root/'monitor.json').read_text())

    def test_long_tool_without_tokens_is_investigation_not_stuck(self):
        m = self.monitor
        m.stream({'type':'assistant','message':{'content':[{'type':'tool_use','id':'t1','name':'Bash'}]}})
        self.t += 600; m.tick()
        self.t += 300
        m.stream({'type':'tool_progress','tool_use_id':'t1','tool_name':'Bash','elapsed_time_seconds':900})
        m.tick()
        self.assertEqual(m.events[-1]['comparison']['assessment'], 'waiting_for_tool')

    def test_error_burst_then_recovery_and_fatal_escalation(self):
        m = self.monitor
        m.hook({'hook':'PostToolUseFailure','tool':'Read','error':'ENOENT'})
        for _ in range(10):
            m.hook({'hook':'PostToolUseFailure','tool':'Read','error':'ENOENT'})
        m.stream({'type':'user','message':{'content':[{'type':'tool_result','is_error':True}]}})
        self.assertEqual(len(m.events), 1)
        self.assertEqual(m.latest_error['message'], 'ENOENT')
        m.stream({'type':'user','message':{'content':[{'type':'tool_result','is_error':False}]}})
        m.hook({'hook':'PostToolUseFailure','tool':'Read','error':'ENOENT'})
        self.assertEqual(len(m.events), 1)
        m.hook({'hook':'StopFailure','error':'authentication_failed'})
        self.assertEqual(len(m.events), 2)
        self.assertTrue(m.events[-1]['error']['fatal'])

    def test_stop_and_reminder_never_claim_completion(self):
        for event in ('Stop', 'Notification', 'Stop', 'Notification'):
            self.monitor.hook({'hook':event})
        self.assertEqual(self.monitor.events, [])
        self.monitor.hook({'hook':'PostToolUseFailure','error':'cancel','is_interrupt':True})
        self.assertEqual(self.monitor.events, [])

    def test_wrong_session_and_partial_lines_do_not_become_progress(self):
        m = self.monitor
        m.stream({'type':'assistant','session_id':'foreign','message':{}})
        self.assertEqual(m.progress_count, 0)
        path = self.root/'stdout.ndjson'
        line = json.dumps({'type':'assistant','session_id':'session','message':{}})
        path.write_text(line[:15]); m.tick(); self.assertEqual(m.progress_count, 0)
        with path.open('a') as out: out.write(line[15:]+'\n')
        m.tick(); self.assertEqual(m.progress_count, 1)
        m.tick(); self.assertEqual(m.progress_count, 1)

    def test_late_start_cannot_invent_five_minute_comparison(self):
        self.t += 1000; self.monitor.tick()
        self.assertEqual(self.monitor.events[-1]['comparison']['assessment'], 'unknown')
        next_round = ce.Monitor(self.root, 'session', self.t, ct.write_json, lambda:self.t)
        self.assertEqual(next_round.events, [])


class Delivery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ctx = ct.Context(str(self.root/'state'), owner='owner')
        self.job_id = str(uuid.uuid4())
        self.session = str(uuid.uuid4())
        self.rdir = Path(ct.round_dir(self.ctx.job_dir(self.job_id), 0))
        self.rdir.mkdir(parents=True)
        self.job = {'job_id':self.job_id,'owner':'owner','session_id':self.session,
                    'cwd':str(self.root),'phase':'running','current_round':0,
                    'rounds':[{'round':0,'run_token':'unique-token','finalized':False}]}
        self.ctx.save(self.job)

    def test_real_receiver_routes_only_matching_round_session_and_token(self):
        base = dict(session_id=self.session,cwd=str(self.root),hook_event_name='PostToolUseFailure',
                    tool_name='Read',error='missing')
        command = [sys.executable,str(SCRIPTS/'claude_events.py'),'--state-dir',self.ctx.state_dir,
                   '--job',self.job_id,'--round','0','--token','unique-token']
        variants = [dict(base,session_id='foreign'), dict(base,cwd='/'), dict(base,agent_id='subagent'),base]
        for payload in variants:
            p = subprocess.run(command,input=json.dumps(payload),text=True,capture_output=True,timeout=10)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout, '')
        lines=(self.rdir/'hooks.ndjson').read_text().splitlines()
        self.assertEqual(len(lines), 1)
        subprocess.run(command[:-1]+['stale-token'],input=json.dumps(base),text=True,check=True)
        self.assertEqual(len((self.rdir/'hooks.ndjson').read_text().splitlines()), 1)

    def test_failure_reminder_filters_and_late_completion_receiver(self):
        command = [sys.executable,str(SCRIPTS/'claude_events.py'),'--state-dir',self.ctx.state_dir,
                   '--job',self.job_id,'--round','0','--token','unique-token']
        def send(**fields):
            payload=dict(session_id=self.session,cwd=str(self.root),**fields)
            subprocess.run(command,input=json.dumps(payload),text=True,check=True)
        send(hook_event_name='StopFailure',error='rate_limit')
        send(hook_event_name='Notification',notification_type='permission_prompt')
        send(hook_event_name='Notification',notification_type='idle_prompt')
        self.job['rounds'][0]['finalized']=True; self.ctx.save(self.job)
        send(hook_event_name='Stop')
        received=[json.loads(x)['hook'] for x in (self.rdir/'hooks.ndjson').read_text().splitlines()]
        self.assertEqual(received,['StopFailure','Notification'])

    def test_cursor_skips_handled_error_and_completion_supersedes_queue(self):
        monitor={'events':[{'seq':1,'kind':'error'},{'seq':2,'kind':'checkpoint','minute':10}],
                 'progress':{'output_tokens':100}}
        ct.write_json(str(self.rdir/'monitor.json'),monitor)
        with patch.object(ct,'reconcile',side_effect=lambda ctx,j:(j,False)):
            result=ct.cmd_await_event(self.ctx,SimpleNamespace(job=self.job_id,after='0:1'))
            self.assertEqual(result['event']['kind'],'checkpoint')
            self.assertEqual(result['cursor'],'0:2')
            self.job['phase']='awaiting_review'; self.ctx.save(self.job)
            result=ct.cmd_await_event(self.ctx,SimpleNamespace(job=self.job_id,after='0:0'))
            self.assertEqual(result['event']['kind'],'settled')
            repeat=ct.cmd_await_event(self.ctx,SimpleNamespace(job=self.job_id,after=result['cursor']))
            self.assertIsNone(repeat['event'])

    def test_foreign_owner_and_future_cursor_rejected(self):
        with patch.object(ct,'reconcile',side_effect=lambda ctx,j:(j,False)):
            with self.assertRaises(ct.CliError):
                ct.cmd_await_event(self.ctx,SimpleNamespace(job=self.job_id,after='1:0'))
            foreign=ct.Context(self.ctx.state_dir,owner='foreign')
            with self.assertRaises(ct.CliError):
                ct.cmd_await_event(foreign,SimpleNamespace(job=self.job_id,after='0:0'))


if __name__=='__main__':
    unittest.main(verbosity=2)
