'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs'), os = require('os'), path = require('path'), cp = require('child_process');
const {Parser, sha} = require('../scripts/remote_codex_agent.cjs');
const c = {session:'session-a', turn:'turn-a', cwd:'D:\\test', token:'test-token'};
const r = (type, payload) => ({timestamp: new Date().toISOString(), type, payload});
const meta = r('session_meta', {id:c.session, cwd:c.cwd, cli_version:'0.153.4'});
const start = r('event_msg', {type:'task_started', turn_id:c.turn});
const context = r('turn_context', {turn_id:c.turn, cwd:c.cwd, model:'test-model', effort:'xhigh'});
const final = r('event_msg', {type:'item_completed', thread_id:c.session, turn_id:c.turn,
  item:{type:'AgentMessage', phase:'final_answer', content:[{type:'text', text:'完成 ✓'}]}});
const done = r('event_msg', {type:'task_complete', turn_id:c.turn, last_agent_message:'完成 ✓'});
function parser(records=[meta,start,context]) {const p=new Parser(c); records.forEach(x=>p.consume(x)); return p;}
test('terminal requires exact turn, context and formal text', () => {
  const p=parser(); p.consume(final); assert.equal(p.status,'running'); p.consume(done);
  assert.equal(p.status,'awaiting_review'); assert.equal(p.snapshot().final_sha256,sha('完成 ✓'));
});
test('foreign session and cwd are rejected', () => {
  assert.throws(()=>parser([r('session_meta',{id:'foreign',cwd:c.cwd})]),/mismatch/);
  assert.throws(()=>parser([r('session_meta',{id:c.session,cwd:'D:\\other'})]),/mismatch/);
  assert.throws(()=>parser([start]),/mismatch/);
});
test('old turn cannot finish target turn', () => {
  const p=parser(); p.consume(r('event_msg',{...done.payload,turn_id:'old'})); assert.equal(p.status,'running');
});
test('missing terminal ID and hooks are not completion', () => {
  const p=parser(); p.consume(r('event_msg',{type:'task_complete',last_agent_message:'fake'}));
  p.consume(r('event_msg',{type:'Stop',turn_id:c.turn})); assert.equal(p.status,'running');
});
test('duplicate completion cannot overwrite result', () => {
  const p=parser(); p.consume(done); p.consume(r('event_msg',{...done.payload,last_agent_message:'other'}));
  assert.equal(p.final,'完成 ✓');
});
test('later turn supersedes unfinished target', () => {
  const p=parser(); p.consume(r('event_msg',{type:'task_started',turn_id:'new'})); p.consume(done);
  assert.equal(p.status,'superseded');
});
test('no reasoning or tool payload in normalized state', () => {
  const p=parser(); p.consume(r('response_item',{type:'reasoning',summary:'secret-thinking'}));
  p.consume(r('event_msg',{type:'item_completed',item:{type:'CommandExecution',stdout:'secret-output'}}));
  assert.ok(!JSON.stringify(p.snapshot()).includes('secret'));
});
test('aborted is not review and missing model cannot pass', () => {
  const p=parser(); p.consume(r('event_msg',{type:'turn_aborted',turn_id:c.turn})); assert.equal(p.status,'interrupted');
  const q=parser([meta,start]); q.consume(done); assert.equal(q.status,'incomplete_evidence');
});
test('conflicting final text rejected', () => {
  const p=parser(); p.consume(final);
  assert.throws(()=>p.consume(r('event_msg',{...done.payload,last_agent_message:'contradiction'})),/mismatch/);
});
test('partial record is withheld; altered replay and overlarge record fail', () => {
  const d=fs.mkdtempSync(path.join(os.tmpdir(),'remote-codex-test-')), log=path.join(d,'rollout.jsonl');
  const run = extra => {
    const a=cp.spawnSync(process.execPath,[path.join(__dirname,'../scripts/remote_codex_agent.cjs'),
      Buffer.from(JSON.stringify({...c,log,once:true,...extra})).toString('base64')],{encoding:'utf8'});
    return a.stdout.trim().split('\n').map(x=>JSON.parse(x));
  };
  try {
    const prefix=[meta,start,context].map(x=>JSON.stringify(x)+'\n').join('');
    fs.writeFileSync(log,prefix+JSON.stringify(done).slice(0,40));
    const state=run({})[0]; assert.equal(state.status,'running'); assert.equal(state.source.bytes,Buffer.byteLength(prefix));
    fs.writeFileSync(log,prefix+JSON.stringify(done)+'\n');
    assert.equal(run({checkpoint:state.source})[0].status,'awaiting_review');
    fs.writeFileSync(log,prefix.replace('test-model','evil-model')+JSON.stringify(done)+'\n');
    assert.equal(run({checkpoint:state.source})[0].error,'source_prefix_changed');
    fs.writeFileSync(log,prefix+'a'.repeat(4*1024*1024+1));
    assert.equal(run({})[0].error,'record_size_limit');
  } finally {fs.rmSync(d,{recursive:true,force:true});}
});
