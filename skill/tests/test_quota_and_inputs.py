"""Offline policy/identity tests; synthetic usage never touches the real cache."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import claude_task as ct
import claude_quota as q
from claude_events import Monitor, hook_settings

def event(session='s', five=.89, week=.6, reset=20000):
    return dict(type='rate_limit_event', session_id=session, rate_limit_info=dict(status='allowed',
                unifiedWindows=dict(five_hour=dict(utilization=five, resetsAt=reset),
                                    seven_day=dict(utilization=week, resetsAt=reset+10000))))

class Quota(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.home=str(self.root/'profile')
    def put(self, e, at=10000, kind='native_stream_live'):
        q.store(self.root,q.parse_event(e,'s'),dict(kind=kind),observed_at=at,home=self.home)
    def test_real_unified_shape_fraction_and_no_rounding(self):
        v=q.parse_event(event(five=.896),'s')
        self.assertAlmostEqual(v['five_hour']['used_percent'],89.6)
        self.put(event(five=.896));self.assertEqual(q.status(self.root,self.home,10001)['state'],'available')
    def test_each_relevant_window_can_pause(self):
        self.put(event(week=.90));s=q.status(self.root,self.home,10001)
        self.assertEqual(s['blocking_windows'],['seven_day']);self.assertEqual(s['action'],'finish_current_round_then_pause')
    def test_malformed_missing_foreign_and_context_usage_not_zero(self):
        self.assertEqual(q.parse_event(event('foreign'),'s'),{})
        self.assertEqual(q.parse_event(dict(type='result',session_id='s',usage={'input_tokens':999999}),'s'),{})
        for x in (None,True,'0.90',float('nan'),-1,1.1):
            self.assertNotIn('five_hour',q.parse_event(event(five=x),'s'))
        self.assertEqual(q.status(self.root,self.home,10001)['state'],'unknown')
    def test_unknown_top_level_rejected_is_hold(self):
        e=dict(type='rate_limit_event',session_id='s',rate_limit_info=dict(status='rejected',rateLimitType='five_hour',resetsAt=20000))
        self.put(e);self.assertEqual(q.status(self.root,self.home,10001)['state'],'paused')
    def test_stale_high_holds_until_reset_not_zero(self):
        self.put(event(five=.91))
        self.assertEqual(q.status(self.root,self.home,12000)['state'],'paused')
        s=q.status(self.root,self.home,20001)
        self.assertEqual(s['state'],'unknown');self.assertTrue(s['windows']['five_hour']['reset_passed'])
        self.assertEqual(s['windows']['five_hour']['used_percent'],91)
    def test_replayed_older_window_and_lower_import_cannot_clear_hold(self):
        self.put(event(five=.91))
        self.put(event(five=.4),at=11000,kind='native_stream_import')
        self.put(event(five=.2,reset=19000),at=12000)
        self.assertEqual(q.status(self.root,self.home,12001)['state'],'paused')
        self.put(event(five=.3,reset=40000),at=13000)
        self.assertEqual(q.status(self.root,self.home,13001)['state'],'available')
    def test_profiles_isolated(self):
        self.put(event(five=.95))
        self.assertEqual(q.status(self.root,self.home+'-other',10001)['state'],'unknown')
    def test_warning_dedup_current_round_continues_and_gate_blocks(self):
        # The event handler records an alert; it never signals a process.
        observer=q.Observer(self.root,self.home,'s')
        monitor=Monitor(self.root,'s',0,lambda p,v:None,clock=lambda:10000,quota_observer=observer)
        with patch.object(q.time,'time',return_value=10000):
            monitor.stream(event(five=.95));monitor.stream(event(five=.95))
        self.assertEqual(len([v for v in monitor.events if v['kind']=='quota_pause']),1)
        self.assertEqual(monitor.quota['state'],'paused')
        ctx=type('Context',(),{'state_dir':str(self.root)})()
        with patch.object(q.time,'time',return_value=10001),self.assertRaises(ct.CliError) as err:
            ct.quota_gate(ctx,self.home)
        self.assertEqual(err.exception.code,'quota_paused')
    def test_partial_bucket_does_not_erase_other_window(self):
        self.put(event(week=.92))
        e=dict(type='rate_limit_event',session_id='s',rate_limit_info=dict(status='allowed',rateLimitType='five_hour',utilization=.1,resetsAt=20000))
        self.put(e,at=10002)
        self.assertEqual(q.status(self.root,self.home,10003)['blocking_windows'],['seven_day'])
    def test_import_reads_latest_observation_of_every_window(self):
        job_root=self.root/'jobs'/'fixture';job_root.mkdir(parents=True)
        job_root.joinpath('job.json').write_text(json.dumps(dict(session_id='s', claude_config_dir=self.home,
            rounds=[dict(evidence=dict(stdout='stream.ndjson'))])))
        old=event(five=.95, week=.92, reset=4000000000)
        recent=dict(type='rate_limit_event',session_id='s',rate_limit_info=dict(status='allowed',
            rateLimitType='five_hour',utilization=.2,resetsAt=4000000000))
        job_root.joinpath('stream.ndjson').write_text(json.dumps(old)+'\n'+json.dumps(recent)+'\n')
        imported=q.refresh_from_jobs(str(self.root),self.home)
        self.assertEqual(imported['blocking_windows'],['seven_day'])
        self.assertEqual(imported['windows']['five_hour']['used_percent'],20)

    def test_config_environment_preserves_auth_lookup_semantics(self):
        default=ct.job_environment(dict(claude_config_env=None),{'CLAUDE_CONFIG_DIR':'/wrong','OTHER':'kept'})
        self.assertNotIn('CLAUDE_CONFIG_DIR',default)
        self.assertEqual(default['OTHER'],'kept')
        explicit=ct.job_environment(dict(claude_config_env=self.home),{})
        self.assertEqual(explicit['CLAUDE_CONFIG_DIR'],self.home)

class InputsAndRevisions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.cwd=self.root/'checkout';self.cwd.mkdir()
        self.refs=self.root/'reference files';self.refs.mkdir();self.file=self.refs/'handoff.md';self.file.write_text('完整输入\n')
    def test_unlimited_default_and_optional_finite_cap(self):
        self.assertIsNone(ct.DEFAULT_MAX_REVISIONS)
        self.assertIsNone(ct.validate_max_revisions('unlimited'))
        self.assertEqual(ct.validate_max_revisions(0),0)
        self.assertEqual(ct.validate_max_revisions(500),500)
        with self.assertRaises(ct.CliError):ct.validate_max_revisions(-1)
    def test_required_external_file_fails_before_launch_without_scope(self):
        with self.assertRaises(ct.CliError) as err:ct.read_access(str(self.cwd),[],[str(self.file)])
        self.assertEqual(err.exception.code,'required_file_outside_scope')
        roots,manifest=ct.read_access(str(self.cwd),[str(self.refs)],[str(self.file)])
        self.assertEqual(roots,[str(self.refs)]);self.assertEqual(manifest[0]['bytes'],self.file.stat().st_size)
    def test_missing_file_and_symlink_outside_scope_rejected(self):
        with self.assertRaises(ct.CliError):ct.read_access(str(self.cwd),[],[str(self.cwd/'missing')])
        (self.cwd/'linked').symlink_to(self.file)
        with self.assertRaises(ct.CliError):ct.read_access(str(self.cwd),[],[str(self.cwd/'linked')])
    def test_long_comma_rules_are_kept_as_json_array_entries(self):
        rule = 'Bash(python3 /' + 'long/path/' * 30 + 'file,one.py*)'
        self.assertEqual(ct.validate_allow_rules([rule]), [rule])
        job = dict(cwd=str(self.cwd), allow_tools=[rule], claude_bin='claude', session_id='s', job_id='j')
        settings = hook_settings('script', 'state', job, dict(round=0, run_token='t'))
        self.assertEqual(settings['permissions']['allow'][-1], rule)
        argv = ct.build_claude_argv(job, 'token', False)
        self.assertEqual(argv[argv.index('--allowed-tools') + 1], 'Read,Glob,Grep')
        for invalid in ('Bash(*)', 'Bash(*python*)', 'Bash(python3\x00x)', 'Bash(python3\nother)'):
            with self.assertRaises(ct.CliError):
                ct.validate_allow_rules([invalid])

    def test_additional_directories_do_not_get_blanket_edit_allow(self):
        job=dict(cwd=str(self.cwd),read_dirs=[str(self.refs)],claude_bin='claude',session_id='s',allow_tools=[],job_id='j')
        argv=ct.build_claude_argv(job,'token',False)
        self.assertIn('--restricted',argv);self.assertEqual(argv[argv.index('--add-dir')+1],str(self.refs))
        rules=argv[argv.index('--allowed-tools')+1].split(',')
        self.assertNotIn('Write',rules);self.assertNotIn('Edit',rules)
        settings=hook_settings('script','state',job,dict(round=0,run_token='t'))
        self.assertEqual(settings['permissions']['allow'],['Edit(/'+str(self.cwd)+'/**)'])

if __name__=='__main__':unittest.main()
