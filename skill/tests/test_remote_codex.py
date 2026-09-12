import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path

spec=importlib.util.spec_from_file_location('remote_codex',Path(__file__).resolve().parents[1]/'scripts/remote_codex.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class RemoteContract(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.d=Path(self.tmp.name)
        self.job=dict(token='secret',session='session',turn='turn',log='C:\\log.jsonl')
        self.state=dict(status='running',cursor=1)
        self.frame=dict(protocol=1,kind='state',**self.job,status='running')
    def tearDown(self): self.tmp.cleanup()
    def test_foreign_route(self):
        for k in ('session','turn','token'):
            with self.assertRaises(ValueError): m.apply_frame(self.d,self.job,self.state,{**self.frame,k:'wrong'})
    def test_replay_dedup(self):
        s=m.apply_frame(self.d,self.job,self.state,self.frame)
        self.assertEqual(s['cursor'],1)
    def test_incomplete_completion(self):
        with self.assertRaises(ValueError): m.apply_frame(self.d,self.job,self.state,{**self.frame,'status':'awaiting_review'})
    def test_full_unicode_result_hash(self):
        text='完整正式原文 ✓\n'*500
        f={**self.frame,'status':'awaiting_review','final_text':text,'final_sha256':hashlib.sha256(text.encode()).hexdigest(),'model':'model','effort':'xhigh',
           'cli_version':'0.153.4','source':dict(path=self.job['log'],bytes=100,sha256='a'*64)}
        s=m.apply_frame(self.d,self.job,self.state,f)
        self.assertEqual((self.d/'final.md').read_text(),text); self.assertEqual(s['cursor'],2)
        with self.assertRaises(ValueError): m.apply_frame(self.d,self.job,self.state,{**f,'final_sha256':'bad'})
    def test_silence_is_stalled_not_approval_or_complete(self):
        s=m.apply_frame(self.d,self.job,self.state,{**self.frame,'activity':'2000-01-01T00:00:00Z'})
        self.assertEqual(s['status'],'stalled')
    def test_shell_input_is_literal(self):
        self.assertEqual(m.ps_string("C:\\odd's name"),"'C:\\odd''s name'")
        for host in ('-oProxyCommand=x','host;whoami','user@host','$(cmd)'):
            with self.assertRaises(ValueError): m.ssh_argv(host,'test')

if __name__=='__main__': unittest.main()
