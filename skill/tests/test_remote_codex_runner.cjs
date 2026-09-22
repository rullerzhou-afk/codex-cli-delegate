'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs'), os = require('os'), path = require('path');
const {atomicWrite, validateConfig, validateWindowsPath, isWithin, inspectRollout, parseProcessProbe,
  failureSafety, reconcileRunningReceipt, validateReclaimEvidence} =
  require('../scripts/remote_codex_runner.cjs');

const owner = '11111111-2222-4333-8444-555555555555';
const request = '7f3a0d8e-7d9f-4f75-8b4c-38c9bf844761';
const config = {
  protocol: 1, owner, request_id: request, token: 'a'.repeat(64), action: 'dispatch',
  site: 'windows', host: 'windows-host', agent: 'codex', request_digest: 'b'.repeat(64),
  prompt_sha256: 'c'.repeat(64), model: 'gpt-6-astra', effort: 'xhigh',
  sandbox: 'workspace-write', windows_sandbox: 'unelevated',
  timeout_seconds: 900, keepalive_window_seconds: 300,
  allowed_roots: ['D:\\work'], allow_non_git: false,
};

test('runner accepts only the bounded Codex carrier contract', () => {
  assert.equal(validateConfig({...config}, {promptRequired:true}).agent, 'codex');
  for (const patch of [
    {agent:'claude'}, {host:'host;whoami'}, {model:'x;evil'}, {effort:'off'},
    {sandbox:'danger-full-access'}, {windows_sandbox:'off'}, {timeout_seconds:0},
    {allowed_roots:[]}, {allow_non_git:'yes'},
  ]) assert.throws(() => validateConfig({...config, ...patch}, {promptRequired:true}));
  assert.throws(() => validateConfig({...config, action:'reclaim'}, {promptRequired:false}), /inspection/);
  assert.equal(validateConfig({...config, action:'reclaim', inspection_sha256:'d'.repeat(64)},
    {promptRequired:false}).action, 'reclaim');
});

test('process probe is tri-state and never treats probe failure as gone', () => {
  assert.deepEqual(parseProcessProbe({status:3, stdout:'{"state":"gone"}'}), {state:'gone'});
  assert.deepEqual(parseProcessProbe({status:4, stdout:''}), {state:'probe_failed'});
  assert.deepEqual(parseProcessProbe({status:0, stdout:'not-json'}), {state:'probe_failed'});
  assert.deepEqual(parseProcessProbe({status:0, stdout:'{"state":"alive","start_time":"t"}'}, 't'),
    {state:'alive', start_time:'t'});
  assert.deepEqual(parseProcessProbe({status:0, stdout:'{"state":"alive","start_time":"other"}'}, 't'),
    {state:'gone', reason:'identity_mismatch'});
});

test('workspace-write failures and timeouts always carry partial-write recovery', () => {
  assert.deepEqual(failureSafety(config), {partial_write_risk:true,
    recovery:'inspect_remote_worktree_before_any_new_request'});
  assert.deepEqual(failureSafety({...config, sandbox:'read-only'}), {partial_write_risk:false});
});

test('carrier loss becomes terminal only after the window and positive gone proof', () => {
  const receipt = {status:'running', keepalive_window_seconds:300};
  assert.equal(reconcileRunningReceipt(receipt, 301000, {state:'probe_failed'}).status, 'running');
  assert.equal(reconcileRunningReceipt(receipt, 301000, {state:'probe_failed'}).reconciliation, 'unknown');
  assert.equal(reconcileRunningReceipt(receipt, 299000, {state:'gone'}).status, 'running');
  const killed = reconcileRunningReceipt(receipt, 301000, {state:'gone'}, 'fixed');
  assert.equal(killed.status, 'killed_by_carrier_loss');
  assert.equal(killed.partial_write_risk, true); assert.equal(killed.classified_at, 'fixed');
});

test('lock reclaim requires owner, stale heartbeat and positive gone proof', () => {
  const receipt = {status:'killed_by_carrier_loss', keepalive_window_seconds:300};
  const ownerRecord = {request_id:request, cwd:'D:\\work'};
  assert.equal(validateReclaimEvidence(config, receipt, ownerRecord, 'D:\\work', 301000, {state:'gone'}), true);
  assert.throws(() => validateReclaimEvidence(config, receipt, {...ownerRecord, request_id:owner},
    'D:\\work', 301000, {state:'gone'}), /owner/);
  assert.throws(() => validateReclaimEvidence(config, receipt, ownerRecord,
    'D:\\work', 299000, {state:'gone'}), /stale/);
  assert.throws(() => validateReclaimEvidence(config, receipt, ownerRecord,
    'D:\\work', 301000, {state:'probe_failed'}), /probe/);
});

test('Windows path validation rejects escape, UNC, ADS and short names', () => {
  assert.equal(validateWindowsPath('D:\\work\\repo', 'cwd'), 'D:\\work\\repo');
  for (const value of ['\\\\server\\share', 'D:\\work\\..\\Windows',
    'D:\\work\\PROGRA~1', 'D:\\work\\trail. ', 'D:\\work:file']) {
    assert.throws(() => validateWindowsPath(value, 'cwd'), value);
  }
  assert.equal(isWithin('D:\\work', 'd:\\WORK\\repo'), true);
  assert.equal(isWithin('D:\\work', 'D:\\worker'), false);
});

test('atomic receipt replacement leaves valid JSON', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'remote-runner-'));
  const file = path.join(dir, 'receipt.json');
  try {
    atomicWrite(file, {status:'accepted'});
    atomicWrite(file, {status:'running', unicode:'test ✅'});
    assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')), {status:'running', unicode:'test ✅'});
    assert.equal(fs.readdirSync(dir).length, 1);
  } finally { fs.rmSync(dir, {recursive:true, force:true}); }
});

test('rollout evidence requires exact identity, model, effort and completion', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'remote-rollout-'));
  const file = path.join(dir, 'rollout.jsonl');
  const records = [
    {type:'session_meta', payload:{id:'session-a', cwd:'D:\\work\\repo'}},
    {type:'event_msg', payload:{type:'task_started', turn_id:'turn-a'}},
    {type:'turn_context', payload:{turn_id:'turn-a', model:'gpt-6-astra', effort:'xhigh',
      sandbox_policy:{type:'workspace-write'}, approval_policy:'never'}},
    {type:'event_msg', payload:{type:'task_complete', turn_id:'turn-a', last_agent_message:'done'}},
  ];
  try {
    fs.writeFileSync(file, records.map(value => JSON.stringify(value)).join('\n') + '\n');
    const evidence = inspectRollout(file, {session:'session-a', cwd:'D:\\work\\repo'});
    assert.equal(evidence.complete, true); assert.equal(evidence.effort, 'xhigh');
    assert.equal(evidence.sandbox, 'workspace-write'); assert.equal(evidence.approval_policy, 'never');
    fs.writeFileSync(file, [...records, records[1]].map(value => JSON.stringify(value)).join('\n') + '\n');
    assert.throws(() => inspectRollout(file, {session:'session-a', cwd:'D:\\work\\repo'}), /ambiguous/);
    const missing = [...records]; missing[2] = {type:'turn_context', payload:{turn_id:'turn-a', model:'gpt-6-astra'}};
    fs.writeFileSync(file, missing.map(value => JSON.stringify(value)).join('\n') + '\n');
    assert.equal(inspectRollout(file, {session:'session-a', cwd:'D:\\work\\repo'}).effort, null);
  } finally { fs.rmSync(dir, {recursive:true, force:true}); }
});
