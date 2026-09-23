'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs'), os = require('os'), path = require('path');
const {validateConfig, inspectRollout, sameRoots} = require('../scripts/remote_codex_runner.cjs');
const {Parser} = require('../scripts/remote_codex_agent.cjs');

const skills = 'C:\\Users\\Example\\.codex\\skills';
const config = {
  protocol: 1, owner: '11111111-2222-4333-8444-555555555555', request_id: '7f3a0d8e-7d9f-4f75-8b4c-38c9bf844761',
  token: 'a'.repeat(64), action: 'dispatch', site: 'windows', host: 'windows-host', agent: 'codex',
  request_digest: 'b'.repeat(64), prompt_sha256: 'c'.repeat(64), model: 'gpt-6-sol', effort: 'xhigh',
  sandbox: 'workspace-write', windows_sandbox: 'unelevated', timeout_seconds: 900, keepalive_window_seconds: 300,
  allowed_roots: ['D:\\'], allow_non_git: false, add_dirs: [skills], add_dir_roots: [skills],
};

test('extra writable directories need workspace-write and valid paths', () => {
  assert.deepEqual(validateConfig({...config}, {promptRequired: true}).add_dirs, [skills]);
  assert.equal(validateConfig({...config, add_dirs: undefined, add_dir_roots: undefined},
    {promptRequired: true}).agent, 'codex');
  for (const patch of [
    {sandbox: 'read-only'}, {add_dirs: ['\\\\server\\share']}, {add_dirs: skills},
    {add_dirs: [skills, skills, skills, skills, skills]}, {add_dir_roots: ['C:\\a:stream']},
  ]) assert.throws(() => validateConfig({...config, ...patch}, {promptRequired: true}), JSON.stringify(patch));
});

test('rollout evidence reports network access and writable roots', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'remote-rollout-'));
  const file = path.join(dir, 'rollout.jsonl');
  const records = policy => [
    {type: 'session_meta', payload: {id: 'session-a', cwd: 'D:\\work\\repo'}},
    {type: 'event_msg', payload: {type: 'task_started', turn_id: 'turn-a'}},
    {type: 'turn_context', payload: {turn_id: 'turn-a', model: 'gpt-6-sol', effort: 'xhigh',
      sandbox_policy: policy, approval_policy: 'never'}},
    {type: 'event_msg', payload: {type: 'task_complete', turn_id: 'turn-a', last_agent_message: 'done'}},
  ];
  const inspect = policy => {
    fs.writeFileSync(file, records(policy).map(value => JSON.stringify(value)).join('\n') + '\n');
    return inspectRollout(file, {session: 'session-a', cwd: 'D:\\work\\repo'});
  };
  try {
    const wider = inspect({type: 'workspace-write', network_access: false, writable_roots: [skills]});
    assert.deepEqual([wider.network_access, wider.writable_roots], [false, [skills]]);
    const plain = inspect({type: 'workspace-write'});
    assert.deepEqual([plain.network_access, plain.writable_roots], [null, []]);
  } finally { fs.rmSync(dir, {recursive: true, force: true}); }
});

test('root comparison ignores only case and trailing separators', () => {
  assert.equal(sameRoots([skills + '\\'], [skills.toUpperCase()]), true);
  assert.equal(sameRoots([], []), true);
  assert.equal(sameRoots([skills], []), false);
  assert.equal(sameRoots([skills, 'C:\\Users\\Example'], [skills]), false);
});

test('observer reports the turn sandbox scope for the second parse', () => {
  const c = {session: 'session-a', turn: 'turn-a', cwd: 'D:\\work', token: 't'};
  const r = (type, payload) => ({timestamp: new Date().toISOString(), type, payload});
  const p = new Parser(c);
  [r('session_meta', {id: 'session-a', cwd: 'D:\\work'}), r('event_msg', {type: 'task_started', turn_id: 'turn-a'}),
    r('turn_context', {turn_id: 'turn-a', cwd: 'D:\\work', model: 'gpt-6-sol', effort: 'xhigh',
      approval_policy: 'never', sandbox_policy: {type: 'workspace-write', network_access: false, writable_roots: [skills]}}),
  ].forEach(record => p.consume(record));
  const shot = p.snapshot();
  assert.deepEqual([shot.network_access, shot.writable_roots], [false, [skills]]);
});
