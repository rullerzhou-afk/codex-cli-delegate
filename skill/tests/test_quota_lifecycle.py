"""End-to-end launch guards with a local fake CLI. Never calls a real model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

# CI marker: launches detached fake-CLI workers and observes local process
# identity, so it runs in the macOS process-identity CI job.
PROCESS_IDENTITY = True

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/claude_task.py'
FAKE=r'''#!/usr/bin/env python3
import json,os,re,sys,time,uuid
from pathlib import Path
if '--version' in sys.argv:print('2.1.261 (simulated)');raise SystemExit(0)
def arg(n):return sys.argv[sys.argv.index(n)+1] if n in sys.argv else None
task=json.load(sys.stdin);sid=arg('--resume') or arg('--session-id')
home=Path(os.environ['CLAUDE_CONFIG_DIR']);home.mkdir(exist_ok=True,parents=True)
with (home/'ledger.jsonl').open('a') as f:f.write(json.dumps({'session':sid,'argv':sys.argv[1:]})+'\n')
def emit(x):print(json.dumps(dict(session_id=sid,**x)),flush=True)
emit(dict(type='system',subtype='init',model=arg('--model')))
if 'quota' in task:
 emit(dict(type='rate_limit_event',rate_limit_info=dict(status='allowed',unifiedWindows=dict(
  five_hour=dict(utilization=task['quota'],resetsAt=time.time()+7200),
  seven_day=dict(utilization=.2,resetsAt=time.time()+604800)))))
time.sleep(task.get('delay',.1))
message=dict(role='assistant',model=arg('--model'),content=[dict(type='text',text='FAKE_COMPLETE')])
p=home/'projects'/re.sub(r'[^a-zA-Z0-9]','-',os.getcwd())/(sid+'.jsonl');p.parent.mkdir(parents=True,exist_ok=True)
with p.open('a') as f:f.write(json.dumps(dict(type='assistant',uuid=str(uuid.uuid4()),sessionId=sid,effort='max',version='2.1.261',message=message))+'\n')
emit(dict(type='assistant',message=message))
emit(dict(type='result',subtype='success',is_error=False,result='FAKE_COMPLETE',permission_denials=[]))
'''

class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.state=self.root/'state';self.cwd=self.root/'project';self.cwd.mkdir()
        self.fake=self.root/'fake.py';self.fake.write_text(FAKE);self.fake.chmod(0o755)
        self.env=dict(os.environ,CLAUDE_CONFIG_DIR=str(self.root/'claude'),PYTHONDONTWRITEBYTECODE='1')
        self.jobs=[]
    def tearDown(self):
        for j in self.jobs:
            try:self.call('stop',j)
            except Exception:pass
        self.temp.cleanup()
    def prompt(self,**data):
        p=self.root/(str(uuid.uuid4())+'.json');p.write_text(json.dumps(data));return str(p)
    def call(self,*args,ok=True):
        p=subprocess.run([sys.executable,str(SCRIPT),'--state-dir',str(self.state),'--owner','test-owner',
                          '--claude-bin',str(self.fake),*args],env=self.env,capture_output=True,text=True,timeout=30)
        value=json.loads(p.stdout)
        self.assertEqual(p.returncode==0,ok,value);return value
    def start(self,*extra,cwd=None,**data):
        out=self.call('start','--cwd',str(cwd or self.cwd),'--prompt-file',self.prompt(**data),'--timeout','20',*extra)
        self.jobs.append(out['job_id']);return out
    def wait(self,j):
        out=self.call('wait',j,'--seconds','15');self.assertEqual(out['phase'],'awaiting_review',out);return out
    def ledger(self):
        p=self.root/'claude/ledger.jsonl';return [json.loads(v) for v in p.read_text().splitlines()] if p.exists() else []
    def test_default_can_complete_five_revisions_same_session(self):
        job=self.start();self.wait(job['job_id']);self.assertIsNone(job['max_revisions'])
        for n in range(5):
            r=self.call('revise',job['job_id'],'--prompt-file',self.prompt());self.wait(job['job_id'])
            self.assertEqual(r['revisions_used'],n+1);self.assertIsNone(r['max_revisions'])
        self.assertEqual(len(self.ledger()),6)
        self.assertEqual({v['session'] for v in self.ledger()},{job['session_id']})
    def test_legacy_cap_can_be_removed_without_new_session(self):
        job=self.start('--max-revisions','0');self.wait(job['job_id'])
        blocked=self.call('revise',job['job_id'],'--prompt-file',self.prompt(),ok=False)
        self.assertEqual(blocked['error'],'revision_limit')
        out=self.call('revise',job['job_id'],'--max-revisions','unlimited','--prompt-file',self.prompt())
        self.wait(job['job_id']);self.assertEqual(out['session_id'],job['session_id'])
    def test_90_alert_does_not_interrupt_round_but_blocks_both_new_entrypoints(self):
        job=self.start(quota=.9,delay=3)
        notice=self.call('await-event',job['job_id'],'--after=-1:0')
        self.assertEqual(notice['event']['kind'],'quota_pause');self.assertEqual(notice['phase'],'running')
        done=self.wait(job['job_id']);self.assertEqual(done['quota']['state'],'paused')
        before=(self.state/'jobs'/job['job_id']/'job.json').read_bytes()
        refused=self.call('revise',job['job_id'],'--prompt-file',self.prompt(),ok=False)
        self.assertEqual(refused['error'],'quota_paused')
        self.assertEqual((self.state/'jobs'/job['job_id']/'job.json').read_bytes(),before)
        other=self.root/'other';other.mkdir()
        refused=self.call('start','--cwd',str(other),'--prompt-file',self.prompt(),ok=False)
        self.assertEqual(refused['error'],'quota_paused');self.assertEqual(len(self.ledger()),1)
        notes=self.root/'accept.md';notes.write_text('Independently checked fixture completion and unchanged invocation count.')
        self.assertEqual(self.call('accept',job['job_id'],'--notes-file',str(notes))['phase'],'accepted')
    def test_missing_or_outside_input_never_invokes_model(self):
        external=self.root/'handoff.md';external.write_text('reference')
        for file in (external,self.cwd/'missing'):
            result=self.call('start','--cwd',str(self.cwd),'--prompt-file',self.prompt(),'--require-file',str(file),ok=False)
            self.assertIn(result['error'],('required_file_outside_scope','required_file_unreadable'))
        self.assertEqual(self.ledger(),[])
        job=self.start('--read-dir',str(self.root),'--require-file',str(external));self.wait(job['job_id'])
        self.assertEqual(job['required_files'][0]['path'],str(external))
        argv=self.ledger()[0]['argv'];self.assertIn('--add-dir',argv);self.assertIn('--append-system-prompt',argv)

if __name__=='__main__':unittest.main()
