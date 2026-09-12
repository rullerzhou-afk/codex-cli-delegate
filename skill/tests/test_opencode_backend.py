import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import opencode_backend as oc
import claude_task as ct
from review_evidence import visible_responses, EvidenceError

class OpenCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.db=self.root/'native.db'
        self.c=sqlite3.connect(self.db)
        self.addCleanup(self.c.close)
        self.c.executescript('create table session(id text,directory text,parent_id text); create table message(id text,session_id text,time_created int,data text); create table part(message_id text,session_id text,time_created int,id text,data text);')
        self.c.execute('insert into session values(?,?,null)',('ses_test',str(self.root)))
        self.user={'role':'user'}
        self.assistant=dict(role='assistant',parentID='u',agent='codex-delegated-opencode',providerID='deepseek',modelID='deepseek-flash',variant='high',finish='stop',time={'completed':100})
        self.c.execute('insert into message values(?,?,?,?)',('u','ses_test',1,json.dumps(self.user)))
        self.c.execute('insert into message values(?,?,?,?)',('a','ses_test',2,json.dumps(self.assistant)))
        self.c.execute('insert into part values(?,?,?,?,?)',('u','ses_test',1,'p1',json.dumps(dict(type='text',text='[codex-delegate:token] task'))))
        self.c.execute('insert into part values(?,?,?,?,?)',('a','ses_test',2,'p2',json.dumps(dict(type='text',text='answer'))))
        self.c.commit()
        self.job=dict(cwd=str(self.root),session_id=None,opencode_db=str(self.db),model=oc.MODEL,effort=oc.EFFORT,opencode_version='1.18.30',opencode_tools=['read'],opencode_bin='/bin/test')
        self.record=dict(run_token='token',kind='initial',started_epoch=0)
        self.stdout=self.root/'stdout.ndjson'
        self.stream=[dict(type='text',sessionID='ses_test',part=dict(messageID='a',text='answer'))]
        self.hooks=[dict(hook='bound',token='token',sessionID='ses_test',cwd=str(self.root)),dict(hook='Stop',token='token',sessionID='ses_test')]
        self.save()
    def save(self):
        self.stdout.write_text(''.join(json.dumps(x)+'\n' for x in self.stream))
        (self.root/'hooks.ndjson').write_text(''.join(json.dumps(x)+'\n' for x in self.hooks))
    def verify(self): return oc.verify(self.job,self.record,str(self.stdout),0)
    def change_assistant(self, **kw):
        self.assistant.update(kw)
        self.c.execute('update message set data=? where id="a"',(json.dumps(self.assistant),));self.c.commit()
    def test_good(self): self.assertTrue(self.verify()['ok'])
    def test_idle_without_completed_message(self):
        self.change_assistant(finish='tool-calls');self.assertFalse(self.verify()['ok'])
    def test_missing_hook(self):
        self.hooks=[];self.save();self.assertFalse(self.verify()['ok'])
    def test_child_binding(self):
        self.c.execute('update session set parent_id="parent"');self.c.commit();self.assertFalse(self.verify()['ok'])
    def test_wrong_model(self):
        self.change_assistant(modelID='another');self.assertFalse(self.verify()['ok'])
    def test_wrong_effort(self):
        self.change_assistant(variant='low');self.assertFalse(self.verify()['ok'])
    def test_wrong_agent(self):
        self.change_assistant(agent='other');self.assertFalse(self.verify()['ok'])
    def test_wrong_parent(self):
        self.change_assistant(parentID='foreign');self.assertFalse(self.verify()['ok'])
    def test_wrong_cwd(self):
        self.c.execute('update session set directory="/"');self.c.commit();self.assertFalse(self.verify()['ok'])
    def test_foreign_turn(self):
        self.c.execute('insert into message values("foreign","ses_test",3,?)',(json.dumps(self.user),));self.c.commit();self.assertFalse(self.verify()['ok'])
    def test_resume_mismatch(self):
        self.job['session_id']='other';self.assertFalse(self.verify()['ok'])
    def test_report_mismatch(self):
        self.stream[0]['part']['text']='invented';self.save();self.assertFalse(self.verify()['ok'])
    def test_native_error(self):
        self.change_assistant(error={'name':'oops'});self.assertFalse(self.verify()['ok'])
    def test_nonzero(self): self.assertFalse(oc.verify(self.job,self.record,str(self.stdout),1)['ok'])
    def test_revision_baseline(self):
        self.job['session_id']='ses_test';self.record.update(kind='revision',baseline_status='ok',prior_assistant_uuids=['old'])
        self.assertFalse(self.verify()['ok'])
        self.c.execute('insert into message values("old","ses_test",0,?)',(json.dumps(self.assistant),));self.c.commit();self.assertTrue(self.verify()['ok'])
    def test_setup_preserves_environment_plugin_and_limits(self):
        prompt=self.root/'task';prompt.write_text('scope')
        with patch.object(oc, 'prepare', return_value=dict(opencode_version='1.19.0', opencode_compatibility={'cli_options':'checked'})), patch.dict(os.environ, {'OPENCODE_CONFIG_CONTENT':json.dumps({'plugin':['user-plugin'],'provider':{'existing':{}}})}):
            argv,env,p=oc.setup(self.job,self.record,False,str(prompt),self.root)
        self.assertEqual(self.record['opencode_version'], '1.19.0')
        self.assertEqual(self.job['opencode_version'], '1.19.0')
        cfg=json.loads(env['OPENCODE_CONFIG_CONTENT'])
        self.assertIn('user-plugin',cfg['plugin']);self.assertIn('existing',cfg['provider'])
        self.assertEqual(env['PWD'],str(self.root));self.assertEqual(argv[argv.index('--dir')+1],str(self.root))
        self.assertNotIn('--auto',argv);self.assertEqual(cfg['permission']['*'],'deny');self.assertEqual(cfg['permission']['external_directory'],'deny')
        self.assertIn('[codex-delegate:token]',Path(p).read_text())
    def prepare_version(self, version, flags=None):
        flags = oc.REQUIRED_FLAGS if flags is None else flags
        help_text = 'Options:\n' + '\n'.join('--' + f + ' description' for f in flags)
        with patch.object(oc.sys, 'platform', 'darwin'), \
                patch.object(oc.subprocess, 'check_output', side_effect=[version, help_text]):
            return oc.prepare('/bin/test', ['read'], [])

    def test_compatible_new_older_and_prerelease_versions_are_not_blocked(self):
        for version in ('1.18.30', '1.19.0', '2.0.0-beta.1', '1.18.29'):
            with self.subTest(version=version):
                info=self.prepare_version(version)
                self.assertEqual(info['opencode_version'],version)
                self.assertEqual(info['opencode_compatibility']['cli_options'],'checked')
                self.assertEqual(info['opencode_compatibility']['matches_reference'],version=='1.18.30')

    def test_missing_required_option_is_actionable(self):
        for flag in oc.REQUIRED_FLAGS:
            with self.subTest(flag=flag), self.assertRaises(ct.CliError) as got:
                self.prepare_version('1.19.0', [f for f in oc.REQUIRED_FLAGS if f != flag])
            self.assertEqual(got.exception.code, 'opencode_incompatible')
            self.assertIn('--'+flag, got.exception.message)

    def test_new_version_still_requires_native_evidence(self):
        self.job['opencode_version']='1.19.0'
        self.assertTrue(self.verify()['ok'])
        self.assertEqual(self.verify()['cli_version'],'1.19.0')
        self.c.execute('alter table message rename column data to changed_data');self.c.commit()
        self.assertFalse(self.verify()['ok'])
        self.assertIn('native_evidence_invalid',str(self.verify()['reasons']))

    def test_probe_failure_has_clear_error(self):
        import subprocess
        with patch.object(oc.subprocess,'check_output',side_effect=subprocess.TimeoutExpired('opencode',10)):
            with self.assertRaises(ct.CliError) as got:oc.probe('/bin/test','--version')
        self.assertEqual(got.exception.code,'opencode_probe')

    def test_probe_is_bounded_and_does_not_execute_shell(self):
        with patch.object(oc.subprocess,'check_output',return_value='1.19.0') as call:
            oc.probe('/path with spaces/opencode','--version')
        self.assertEqual(call.call_args.args[0],['/path with spaces/opencode','--version'])
        self.assertEqual(call.call_args.kwargs['timeout'],10)
        import subprocess
        self.assertEqual(call.call_args.kwargs['stderr'],subprocess.STDOUT)
        self.assertFalse(call.call_args.kwargs.get('shell',False))

    def test_export_formal_text_only(self):
        data=(json.dumps(self.stream[0])+'\n'+json.dumps(dict(type='reasoning',sessionID='ses_test',part=dict(text='private')))+'\n').encode()
        self.assertEqual([r['text'] for r in visible_responses(data,'opencode','ses_test')],['answer'])
        with self.assertRaises(EvidenceError):visible_responses(data,'opencode','other')
    def test_monitor_foreign_events(self):
        m=oc.OpenCodeMonitor(self.root,self.job,self.record,ct.write_json)
        before=m.progress_count
        m.stream(dict(type='text',sessionID='child'))
        m.hook(dict(token='wrong',sessionID='ses_test',hook='StopFailure'))
        self.assertEqual(before,m.progress_count);self.assertIsNone(m.latest_error)

if __name__=='__main__': unittest.main()
