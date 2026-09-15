import copy, hashlib, importlib, json, os, sys, tempfile, unittest
from unittest.mock import patch
from pathlib import Path
SCRIPT=Path(os.environ.get('DELEGATE_SCRIPT', 'work/skill-kimi/claude-delegate/scripts/claude_task.py')).resolve()
sys.path.insert(0,str(SCRIPT.parent))
import claude_task as ct
import kimi_backend as k

class KimiContract(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name).resolve(); self.home=self.root/'home'; self.cwd=self.root/'project'; self.cwd.mkdir()
  self.sid='session_11111111-2222-4333-8444-555555555555'; self.d=self.home/'sessions'/'workspace'/self.sid; (self.d/'agents/main').mkdir(parents=True)
  self.job=dict(kimi_home=str(self.home),cwd=str(self.cwd),session_id=None,kimi_tools=['Read'],kimi_bin='/test/kimi')
  self.record=dict(run_token='codexdel-test-r000-a',kind='initial',prior_assistant_uuids=[],baseline_status='empty',started_epoch=0)
  self.out=self.root/'stdout.ndjson'
  (self.home/'session_index.jsonl').write_text(json.dumps(dict(sessionId=self.sid,sessionDir=str(self.d),workDir=str(self.cwd)))+'\n')
  (self.d/'state.json').write_text(json.dumps(dict(id=self.sid,cwd=str(self.cwd),lastTurnReason='completed')))
  self.native=[dict(type='profile.bind',agentId='main',activeToolNames=['Read']),dict(type='turn.prompt',agentId='main',input=[dict(type='text',text='[delegation-run: codexdel-test-r000-a]\nTask')]),dict(type='llm.request',agentId='main',kind='loop',model=k.RAW_MODEL,modelAlias=k.MODEL,thinkingEffort='max'),dict(type='context.append_loop_event',agentId='main',event=dict(type='content.part',stepUuid='step1',part=dict(type='text',text='Done'))),dict(type='context.append_loop_event',agentId='main',event=dict(type='step.end',uuid='step1',finishReason='end_turn')),dict(type='turn.ended',agentId='main',reason='completed')]
  self.stream=[dict(role='meta',type='system.version',version='0.39.0'),dict(role='assistant',content='Done'),dict(role='meta',type='session.resume_hint',session_id=self.sid)]
  self.save()
 def tearDown(self): self.tmp.cleanup()
 def save(self):
  k.wire_path(self.d).write_text(''.join(json.dumps(r)+'\n' for r in self.native));self.out.write_text(''.join(json.dumps(r)+'\n' for r in self.stream))
 def verify(self,code=0): return k.verify(self.job,self.record,str(self.out),code)
 def test_success(self): self.assertTrue(self.verify()['ok'])
 def test_missing_hint_model_effort_completion_and_report_fail_closed(self):
  for case in ('hint','model','effort','end','report','exit'):
   with self.subTest(case=case):
    native=copy.deepcopy(self.native);stream=copy.deepcopy(self.stream)
    if case=='hint': self.stream[-1]['session_id']='session_99999999-2222-4333-8444-555555555555'
    if case=='model': self.native[2]['model']='different'
    if case=='effort': self.native[2]['thinkingEffort']='high'
    if case=='end': self.native[-1]['reason']='failed'
    if case=='report': self.stream[1]['content']='Forged success'
    self.save();self.assertFalse(self.verify(1 if case=='exit' else 0)['ok']);self.native=native;self.stream=stream
 def test_wrong_owner_cwd_and_duplicate_session_not_adopted(self):
  self.job['cwd']=str(self.root/'other');self.assertFalse(self.verify()['ok'])
 def test_prior_native_requests_do_not_prove_current_round(self):
  self.native=[self.native[0],self.native[2],self.native[1]]+self.native[3:];self.save();self.assertFalse(self.verify()['ok'])
 def test_extra_turn_rejected(self):
  self.native.append(dict(type='turn.prompt',agentId='main',input=[]));self.save();self.assertFalse(self.verify()['ok'])
 def test_revision_prefix_mutation_rejected(self):
  self.job['session_id']=self.sid; prior,status=k.baseline(self.job)
  self.record.update(kind='revision',prior_assistant_uuids=prior,baseline_status=status)
  self.assertFalse(self.verify()['ok']) # old prompt is not a new turn
  self.native.append(copy.deepcopy(self.native[1])); self.save(); self.native[0]['activeToolNames']=['Bash'];self.save()
  self.assertFalse(self.verify()['ok'])
 def test_argv_exact_resume_and_tool_profile(self):
  prompt=self.root/'prompt.md';prompt.write_text('Task'); self.job['session_id']=self.sid
  args=k.argv(self.job,self.record,True,prompt,self.root)
  self.assertIn('--session',args);self.assertEqual(args[args.index('--session')+1],self.sid);self.assertNotIn('--agent-file',args)
  self.assertIn(self.record['run_token'],args[args.index('-p')+1]);self.assertNotIn('--auto',args)
 def test_monitor_error_once_progress_and_no_thinking(self):
  # Move the native files away so synthetic feed stays isolated.
  (self.home/'session_index.jsonl').unlink()
  monitor=k.KimiMonitor(self.root,self.job,self.record,ct.write_json,clock=lambda:0)
  monitor.native_row(self.native[1])
  call=lambda ident:dict(type='context.append_loop_event',agentId='main',event=dict(type='tool.call',toolCallId=ident,name='Read'))
  result=lambda ident,bad:dict(type='context.append_loop_event',agentId='main',event=dict(type='tool.result',toolCallId=ident,result=dict(isError=bad,output='secret-content error')))
  monitor.native_row(call('a'));monitor.native_row(result('a',True));monitor.native_row(call('b'));monitor.native_row(result('b',True))
  self.assertEqual(len(monitor.events),1);self.assertEqual(monitor.events[0]['error']['tool'],'Read')
  monitor.native_row(call('c'));monitor.native_row(result('c',False));self.assertFalse(monitor.tools)
  monitor.native_row(dict(type='usage.record',agentId='main',usageScope='turn',usage=dict(inputOther=10,inputCacheRead=20,output=30)))
  shot=monitor.snapshot();self.assertEqual(shot['input_tokens'],30);self.assertEqual(shot['output_tokens'],30)
  self.assertNotIn('secret-content',json.dumps(shot)+json.dumps(monitor.events))
 def test_unknown_tokens_and_checkpoints(self):
  (self.home/'session_index.jsonl').unlink();clock=[0];m=k.KimiMonitor(self.root,self.job,self.record,ct.write_json,clock=lambda:clock[0])
  self.assertIsNone(m.snapshot()['output_tokens']);clock[0]=600;m.tick();clock[0]=900;m.tick();self.assertEqual([e['minute'] for e in m.events],[10,15]);self.assertIsNone(m.events[-1]['comparison']['output_tokens_delta'])
 def test_foreign_agent_and_old_turn_usage_ignored(self):
  (self.home/'session_index.jsonl').unlink();m=k.KimiMonitor(self.root,self.job,self.record,ct.write_json,clock=lambda:0)
  u=dict(type='usage.record',agentId='main',usageScope='turn',usage=dict(output=99));m.native_row(u);self.assertEqual(m.usage_count,0)
  m.native_row(self.native[1]);u['agentId']='child';m.native_row(u);self.assertEqual(m.usage_count,0)
 def test_claude_permission_rules_rejected(self):
  with patch.object(k.sys,'platform','darwin'), self.assertRaises(ct.CliError) as got:k.prepare('/bin/true',[],['Bash(python*)'])
  self.assertEqual(got.exception.code,'kimi_permissions')

class KimiIdentity(unittest.TestCase):
 def test_reused_pid_and_unverifiable_never_signal(self):
  from unittest.mock import patch
  saved=dict(birth_us=12345678901,uid=501,ppid=10,pgid=42,executable='/tmp/kimi')
  record=dict(pid=42,kernel=saved,identity_verified=True,identity_method='darwin_proc')
  with patch.object(k,'kernel_identity',return_value=dict(saved,ppid=1)):
   self.assertEqual(k.identity_state(record),'alive')
  for key,value in [('birth_us',12345678902),('uid',502),('pgid',1),('executable','/tmp/other')]:
   with patch.object(k,'kernel_identity',return_value=dict(saved,**{key:value})):
    self.assertEqual(k.identity_state(record),'mismatch')
  with patch.object(k,'kernel_identity',return_value=None):
   self.assertEqual(k.identity_state(record),'unverifiable')
 def test_capture_requires_direct_child_and_expected_binary(self):
  from unittest.mock import patch
  ident=dict(birth_us=123,uid=os.getuid(),ppid=os.getpid(),pgid=42,executable='/test/kimi')
  with patch.object(k,'kernel_identity',return_value=ident):
   self.assertTrue(k.capture_identity(42,'token','/test/kimi')['identity_verified'])
  with patch.object(k,'kernel_identity',return_value=dict(ident,ppid=1)), patch.object(k.time,'monotonic',side_effect=[0,3]):
   self.assertFalse(k.capture_identity(42,'token','/test/kimi')['identity_verified'])

if __name__=='__main__':unittest.main()
