"""Offline behavior tests. Only test-owned files; no model or network calls."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

SCRIPT = Path(os.environ.get('DELEGATE_SCRIPT', Path(__file__).resolve().parents[1] / 'scripts/claude_task.py'))
sys.path.insert(0, str(SCRIPT.parent))
import claude_task as c
import review_evidence as evidence


def uid(): return str(uuid.uuid4())
def ndjson(rows): return b''.join((json.dumps(x) + '\n').encode() for x in rows)
def sha(data): return hashlib.sha256(data).hexdigest()


class Revalidation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / 'cwd'; self.cwd.mkdir()
        self.config = self.root / 'claude'
        self.env = patch.dict(os.environ, CLAUDE_CONFIG_DIR=str(self.config)); self.env.start()
        self.addCleanup(self.env.stop)
        self.ctx = c.Context(str(self.root / 'state'), owner='owner-local')
        self.jid, self.sid = uid(), uid()
        self.jroot = Path(self.ctx.job_dir(self.jid)); self.jroot.mkdir()
        self.transcript = self.config / 'projects' / c.encode_project_path(str(self.cwd)) / (self.sid + '.jsonl')
        self.transcript.parent.mkdir(parents=True)
        self.real = dict(type='assistant', uuid=uid(), sessionId=self.sid, effort='max', version='2.1.261',
                         message=dict(id='msg_model_1', role='assistant', model=c.MODEL, content=[dict(type='text',text='review complete')]))
        self.placeholder = dict(type='assistant',uuid=uid(),sessionId=self.sid,version='2.1.261',
            isApiErrorMessage=False,isSidechain=False,entrypoint='sdk-cli',message=dict(
                id=uid(),model='<synthetic>',role='assistant',type='message',stop_reason='stop_sequence',stop_sequence='',
                usage=dict(input_tokens=0,output_tokens=0,cache_creation_input_tokens=0,cache_read_input_tokens=0),
                content=[dict(type='text',text='No response requested.')]))
        self.stream_rows = [dict(type='system',subtype='init',session_id=self.sid),
                            dict(type='assistant',session_id=self.sid,message=copy.deepcopy(self.real['message'])),
                            dict(type='result',session_id=self.sid,subtype='success',is_error=False,result='review complete')]
        self.stdout = self.jroot / 'stdout.ndjson'
        self.stdout.write_bytes(ndjson(self.stream_rows))
        (self.jroot / 'prompt.md').write_text('Read the candidate and report findings.\n')
        self.record = dict(round=0,kind='revision',status='failed',finalized=True,exit_code=0,finished_at='2026-09-08T01:00:00Z',
            timed_out=False,run_token='test-run',prompt='prompt.md',prompt_sha256=sha((self.jroot/'prompt.md').read_bytes()),
            evidence=dict(stdout='stdout.ndjson'),evidence_sha256=dict(stdout=sha(self.stdout.read_bytes())),
            baseline_status='missing',prior_assistant_uuids=[],worker={'pid':999999991},claude={'pid':999999992},
            verification=dict(ok=False,model_verified=True,effort_verified=False,session_ok=True,
                reasons=['effort_missing_on_1_entries','transcript_model_mismatch:<synthetic>,claude-opus-5']))
        self.job = dict(job_id=self.jid,owner='owner-local',backend='claude',session_id=self.sid,cwd=str(self.cwd),
            model=c.MODEL,effort=c.EFFORT,current_round=0,phase='failed',revisions_used=3,max_revisions=3,
            process_state='exited',stop_requested=False,rounds=[self.record],reservation_key='path:'+str(self.cwd))
        self.ctx.save(self.job)
        self.rows=[self.placeholder,self.real];self.save_transcript()
        self.args=SimpleNamespace(job=self.jid,expected_round=0,expected_stream_sha256=sha(self.stdout.read_bytes()))

    def save_transcript(self): self.transcript.write_bytes(ndjson(self.rows))
    def verify(self):
        self.save_transcript();self.stdout.write_bytes(ndjson(self.stream_rows))
        return c.verify_round(self.job,str(self.stdout),0,[], 'missing')
    def revalidate(self):
        with patch.object(c,'identity_state',return_value='exited'):
            return c.cmd_revalidate(self.ctx,self.args)
    def assert_refused_unchanged(self,code,action=None):
        before=Path(self.ctx.job_file(self.jid)).read_bytes()
        with self.assertRaises(c.CliError) as err: (action or self.revalidate)()
        self.assertEqual(err.exception.code,code)
        self.assertEqual(before,Path(self.ctx.job_file(self.jid)).read_bytes())

    def test_exact_placeholder_plus_bound_model_passes_and_records_marker(self):
        v=self.verify();self.assertTrue(v['ok'],v)
        self.assertEqual((v['new_assistant_entries'],v['new_model_entries']),(2,1))
        self.assertEqual(v['ignored_cli_placeholders'][0]['uuid'],self.placeholder['uuid'])
        self.assertEqual(v['ignored_cli_placeholders'][0]['record_sha256'],sha(ndjson([self.placeholder])))

    def test_ordinary_real_stream_keeps_existing_contract(self):
        self.rows=[self.real];self.assertTrue(self.verify()['ok'])

    def test_api_errors_and_near_match_markers_never_excluded(self):
        mutations = [
            lambda p:p.update(isApiErrorMessage=True),lambda p:p.pop('isApiErrorMessage'),
            lambda p:p.update(error='rate_limit'),lambda p:p.update(apiError='unknown'),
            lambda p:p.update(entrypoint='unknown'),lambda p:p.update(isSidechain=True),
            lambda p:p.update(sessionId=uid()),lambda p:p.update(effort='max'),
            lambda p:p['message']['content'][0].update(text='No response requested. '),
            lambda p:p['message']['content'].append(dict(type='text',text='extra response')),
            lambda p:p['message']['usage'].update(output_tokens=1),
            lambda p:p['message']['usage'].update(input_tokens=False),
            lambda p:p['message']['usage'].update(server_tool_use=dict(web_search_requests=1)),
            lambda p:p['message'].update(id='missing-id'),lambda p:p['message'].update(stop_reason='end_turn'),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                p=copy.deepcopy(self.placeholder);mutate(p);self.rows=[p,self.real]
                v=self.verify();self.assertFalse(v['ok'],v);self.assertFalse(v['ignored_cli_placeholders'])

    def test_missing_effort_downgrade_wrong_model_and_session_fail(self):
        for field,value in [('effort',None),('effort','high'),('sessionId',uid()),('model','claude-sonnet-4')]:
            with self.subTest(field=field,value=value):
                real=copy.deepcopy(self.real)
                (real['message'] if field=='model' else real)[field]=value
                self.rows=[self.placeholder,real]
                self.assertFalse(self.verify()['ok'])

    def test_stream_synthetic_and_synthetic_only_never_success(self):
        self.stream_rows.insert(1,dict(type='assistant',session_id=self.sid,message=self.placeholder['message']))
        self.assertFalse(self.verify()['ok'])
        self.rows=[self.placeholder];self.stream_rows.pop(2)
        self.assertFalse(self.verify()['ok'])

    def test_binding_demands_exact_current_content_id_session_both_directions(self):
        original=copy.deepcopy(self.stream_rows)
        for change in ('content','id','session','extra_transcript','missing_id'):
            with self.subTest(change=change):
                self.stream_rows=copy.deepcopy(original);self.rows=[self.placeholder,self.real]
                if change=='content':self.stream_rows[1]['message']['content'][0]['text']='different'
                if change=='id':self.stream_rows[1]['message']['id']='msg_other'
                if change=='session':self.stream_rows[1]['session_id']=uid()
                if change=='missing_id':self.stream_rows[1]['message'].pop('id')
                if change=='extra_transcript':
                    extra=copy.deepcopy(self.real);extra['uuid']=uid();extra['message']['id']='msg_extra';self.rows.append(extra)
                self.assertFalse(self.verify()['ok'])

    def test_result_success_signals_remain_required(self):
        original=copy.deepcopy(self.stream_rows)
        for change in ('error','missing_error','session','multiple','missing','subtype'):
            with self.subTest(change=change):
                self.stream_rows=copy.deepcopy(original)
                if change=='error':self.stream_rows[-1]['is_error']=True
                if change=='missing_error':self.stream_rows[-1].pop('is_error')
                if change=='session':self.stream_rows[-1]['session_id']=uid()
                if change=='multiple':self.stream_rows.append(copy.deepcopy(self.stream_rows[-1]))
                if change=='missing':self.stream_rows.pop()
                if change=='subtype':self.stream_rows[-1]['subtype']='error'
                self.assertFalse(self.verify()['ok'])
        self.stream_rows=original;self.verify()
        self.assertFalse(c.verify_round(self.job,str(self.stdout),1,[],'missing')['ok'])

    def test_prior_model_cannot_supply_current_binding(self):
        v=c.verify_round(self.job,str(self.stdout),0,[self.real['uuid']],'ok')
        self.assertFalse(v['ok'])
        v=c.verify_round(self.job,str(self.stdout),0,[],'ambiguous');self.assertFalse(v['ok'])

    def test_revalidation_retains_original_failure_and_never_invokes_model(self):
        before=copy.deepcopy(self.record);stream=self.stdout.read_bytes();transcript=self.transcript.read_bytes()
        with patch.object(c,'build_claude_argv',side_effect=AssertionError('model invocation')), \
             patch.object(c,'launch_transaction',side_effect=AssertionError('worker invocation')):
            v=self.revalidate()
        self.assertTrue(v['ok'],v);self.assertFalse(v['model_invoked'])
        after=self.ctx.load(self.jid);r=after['rounds'][0]
        self.assertEqual(after['phase'],'awaiting_review');self.assertNotIn('accepted',after)
        self.assertEqual(after['revisions_used'],3);self.assertEqual(after['current_round'],0)
        self.assertEqual(r['revalidations'][0]['previous_verification'],before['verification'])
        self.assertEqual(r['revalidations'][0]['previous_status'],'failed')
        for key in ('exit_code','finished_at','prior_assistant_uuids','baseline_status','evidence_sha256','prompt_sha256'):
            self.assertEqual(r[key],before[key])
        self.assertEqual(self.stdout.read_bytes(),stream);self.assertEqual(self.transcript.read_bytes(),transcript)
        self.assert_refused_unchanged('bad_phase')

    def test_revalidation_rejects_non_owner_stale_round_and_wrong_expected_hash(self):
        badctx=c.Context(self.ctx.state_dir,owner='other-owner')
        self.assert_refused_unchanged('forbidden',lambda:c.cmd_revalidate(badctx,self.args))
        self.args.expected_round=1;self.assert_refused_unchanged('stale_round')
        self.args.expected_round=0;self.args.expected_stream_sha256='0'*64;self.assert_refused_unchanged('evidence_changed')

    def test_revalidation_rejects_ineligible_exit_and_failure_states(self):
        mutations=[lambda j:j.update(backend='kimi'),lambda j:j.update(phase='accepted'),
            lambda j:j.update(stop_requested=True),lambda j:j['rounds'][0].update(exit_code=1),
            lambda j:j['rounds'][0].update(exit_code=False),lambda j:j['rounds'][0].update(timed_out=True),
            lambda j:j['rounds'][0].update(finalized=False),lambda j:j['rounds'][0].update(finished_at=None),
            lambda j:j['rounds'][0]['verification'].update(model_verified=False),
            lambda j:j['rounds'][0]['verification'].update(session_ok=False),
            lambda j:j['rounds'][0]['verification'].update(reasons=['result_is_error:True'])]
        for mutate in mutations:
            j=copy.deepcopy(self.job);mutate(j);self.ctx.save(j)
            self.assert_refused_unchanged('bad_phase' if j['backend']=='kimi' or j['phase']!='failed' else 'not_revalidatable')

    def test_revalidation_rejects_unknown_live_and_unverifiable_processes(self):
        for state in ('unknown','alive','mismatch','unverifiable'):
            with self.subTest(state=state),patch.object(c,'identity_state',return_value=state):
                self.assert_refused_unchanged('processes_not_gone',lambda:c.cmd_revalidate(self.ctx,self.args))

    def test_process_state_change_between_probes_never_clears_uncertainty(self):
        with patch.object(c,'identity_state',side_effect=['unknown','unknown','mismatch','exited']):
            self.assert_refused_unchanged('processes_not_gone',lambda:c.cmd_revalidate(self.ctx,self.args))

    def test_revalidation_rejects_other_directory_reservation(self):
        other=copy.deepcopy(self.job);other.update(job_id=uid(),owner='other-owner',phase='running')
        Path(self.ctx.job_dir(other['job_id'])).mkdir();self.ctx.save(other)
        self.assert_refused_unchanged('cwd_busy')

    def test_original_completion_seal_and_prompt_are_mandatory(self):
        original=self.stdout.read_bytes();self.stdout.write_bytes(original+b'\n')
        self.assert_refused_unchanged('evidence_changed');self.stdout.write_bytes(original)
        j=copy.deepcopy(self.job);j['rounds'][0].pop('evidence_sha256');self.ctx.save(j)
        self.assert_refused_unchanged('evidence_changed');self.ctx.save(self.job)
        (self.jroot/'prompt.md').write_text('changed');self.assert_refused_unchanged('evidence_changed')

    def test_transcript_change_during_revalidation_refused(self):
        original=c.verify_round
        def drift(*args,**kwargs):
            result=original(*args,**kwargs)
            with self.transcript.open('ab') as f:f.write(b'\n')
            return result
        with patch.object(c,'verify_round',side_effect=drift):self.assert_refused_unchanged('evidence_changed')

    def test_failed_recheck_preserves_old_failure_and_appends_diagnostic(self):
        self.real['effort']='high';self.save_transcript();v=self.revalidate()
        self.assertFalse(v['ok']);j=self.ctx.load(self.jid)
        self.assertEqual(j['phase'],'failed');self.assertEqual(j['rounds'][0]['verification'],self.record['verification'])
        self.assertEqual(len(j['rounds'][0]['revalidations']),1)

    def test_edited_transcript_without_marker_cannot_promote_old_failure(self):
        self.rows=[self.real];self.save_transcript();self.assert_refused_unchanged('not_revalidatable')

    def test_new_evidence_required_and_old_export_preserved(self):
        olddir=self.root/'old-export'
        old=evidence.export(self.ctx,self.job,0,olddir,'candidate-sha','now')
        oldbytes=(olddir/'provenance.json').read_bytes()
        self.revalidate();j=self.ctx.load(self.jid)
        evidence.verify(olddir,expected_sha256=old['provenance_sha256'])
        with self.assertRaises(evidence.EvidenceError):evidence.verify(olddir,job=j,round_index=0,source_root=self.jroot)
        newdir=self.root/'new-export';evidence.export(self.ctx,j,0,newdir,'candidate-sha','now')
        evidence.verify(newdir,job=j,round_index=0,source_root=self.jroot)
        notes=self.root/'review.md';notes.write_text('Independent test review completed.\n')
        args=SimpleNamespace(job=self.jid,notes_file=str(notes),evidence_dir=str(newdir))
        with patch.object(c,'identity_state',return_value='exited'):
            result=c.cmd_accept(self.ctx,args)
        self.assertEqual(result['phase'],'accepted');self.assertEqual(oldbytes,(olddir/'provenance.json').read_bytes())

    def test_accept_rejects_transcript_changed_after_revalidation(self):
        self.revalidate();self.transcript.write_bytes(self.transcript.read_bytes()+b'\n')
        notes=self.root/'review.md';notes.write_text('review')
        with patch.object(c,'identity_state',return_value='exited'):
            self.assert_refused_unchanged('evidence_changed',lambda:c.cmd_accept(self.ctx,SimpleNamespace(job=self.jid,notes_file=str(notes),evidence_dir=None)))


if __name__ == '__main__':unittest.main()
