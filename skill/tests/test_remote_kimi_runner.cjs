'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('crypto');
const fs = require('fs'), os = require('os'), path = require('path');
const {validateConfig, failureSafety, kimiScriptFromShim, kimiThinking, validateKimiProfile, gitRoot,
  kimiSessionDir, kimiWireRows, inspectKimiTurn, findKimiEvidence} = require('../scripts/remote_codex_runner.cjs');

const owner = '11111111-2222-4333-8444-555555555555';
const request = '7f3a0d8e-7d9f-4f75-8b4c-38c9bf844761';
const marker = '0123456789abcdef0123456789abcdef';
const session = 'session_0a0a0a0a-bbbb-4ccc-8ddd-eeeeeeeeeeee';
const readTools = ['Glob', 'Grep', 'Read', 'ReadMediaFile'];
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const config = {
  protocol: 1, owner, request_id: request, token: 'a'.repeat(64), action: 'dispatch',
  site: 'windows', host: 'windows-host', agent: 'kimi', request_digest: 'b'.repeat(64),
  prompt_sha256: 'c'.repeat(64), model: 'kimi-code/k3-256k', effort: 'max', kimi_tools: readTools,
  kimi_home: 'C:\\Users\\Example\\.kimi-code', run_marker: marker, kimi_profile_sha256: 'd'.repeat(64),
  session_id: null, baseline_bytes: null, baseline_sha256: null,
  timeout_seconds: 86400, keepalive_window_seconds: 300, allowed_roots: ['D:\\'], allow_non_git: false,
};
// Same shape as kimi_backend.agent_profile, including Python's JSON spacing.
const profile = tools => '---\nname: codex-delegated-kimi\ndescription: Authorized scoped delegated task\n'
  + `tools: ${JSON.stringify(tools).replace(/","/g, '", "')}\nsubagents: []\n---\n\${base_prompt}\nFollow the supplied task scope.\n`;

function turn(text = 'done', overrides = {}) {
  return [
    {type: 'profile.bind', agentId: 'main', activeToolNames: overrides.tools || readTools},
    {type: 'turn.prompt', agentId: 'main', input: [{type: 'text', text: `[delegation-run: ${overrides.marker || marker}]\nread it`}]},
    {type: 'llm.request', agentId: 'main', kind: 'loop', model: overrides.model || 'k3-256k',
      modelAlias: 'kimi-code/k3-256k', thinkingEffort: overrides.effort || 'max'},
    {type: 'context.append_loop_event', agentId: 'main', event: {type: 'step.begin', uuid: 'step-1'}},
    {type: 'context.append_loop_event', agentId: 'main',
      event: {type: 'content.part', stepUuid: 'step-1', part: {type: 'text', text}}},
    {type: 'context.append_loop_event', agentId: 'main', event: {type: 'step.end', uuid: 'step-1', finishReason: 'end_turn'}},
    {type: 'turn.ended', agentId: 'main', reason: overrides.reason || 'completed'},
  ];
}
const bytes = rows => Buffer.from(rows.map(row => JSON.stringify(row)).join('\n') + '\n');
const expected = (extra = {}) => ({marker, offset: 0, model: 'kimi-code/k3-256k', effort: 'max', tools: readTools, ...extra});

test('runner accepts only the bounded Kimi contract', () => {
  assert.equal(validateConfig({...config}, {promptRequired: true}).agent, 'kimi');
  for (const patch of [
    {kimi_tools: ['Read', 'Shell']}, {kimi_tools: []}, {kimi_tools: ['Read', 'Read']}, {model: 'k3;calc'},
    {model: 'kimi-code/k3 256k'}, {effort: 'xhigh'}, {run_marker: 'x'}, {kimi_profile_sha256: null},
    {session_id: 'session_bad', baseline_bytes: 1, baseline_sha256: 'e'.repeat(64)},
    {session_id: session, baseline_bytes: 0, baseline_sha256: 'e'.repeat(64)},
    {kimi_home: '\\\\server\\share'}, {agent: 'claude'},
  ]) assert.throws(() => validateConfig({...config, ...patch}, {promptRequired: true}), JSON.stringify(patch));
  const revision = {...config, session_id: session, baseline_bytes: 10, baseline_sha256: 'e'.repeat(64),
    kimi_profile_sha256: null};
  assert.equal(validateConfig(revision, {promptRequired: true}).session_id, session);
  assert.throws(() => validateConfig({...config, action: 'evidence'}, {promptRequired: false}), /bad_session/);
  assert.equal(validateConfig({...config, action: 'evidence', evidence_session: session},
    {promptRequired: false}).action, 'evidence');
  const codex = {...config, agent: 'codex', model: 'gpt-6-astra', effort: 'xhigh', sandbox: 'read-only',
    windows_sandbox: 'unelevated', action: 'evidence'};
  assert.throws(() => validateConfig(codex, {promptRequired: false}), /bad_action/);
});

test('Kimi write risk follows the chosen tools, since Kimi has no sandbox', () => {
  assert.equal(failureSafety(config).partial_write_risk, false);
  assert.equal(failureSafety({...config, kimi_tools: ['Read', 'WebSearch', 'FetchURL']}).partial_write_risk, false);
  for (const tool of ['Write', 'Edit', 'Bash']) {
    assert.equal(failureSafety({...config, kimi_tools: [...readTools, tool]}).partial_write_risk, true, tool);
  }
});

test('npm shim resolves to the Kimi entry script, never through cmd.exe', () => {
  const shim = 'C:\\Users\\R\\AppData\\Roaming\\npm\\kimi.cmd';
  const source = '"%_prog%"  "%dp0%\\node_modules\\@moonshot-ai\\kimi-code\\dist\\main.mjs" %*';
  assert.equal(kimiScriptFromShim(shim, source),
    'C:\\Users\\R\\AppData\\Roaming\\npm\\node_modules\\@moonshot-ai\\kimi-code\\dist\\main.mjs');
  assert.equal(kimiScriptFromShim(shim, '"%dp0%\\node_modules\\other\\main.mjs" %*'), null);
});

test('thinking must already select the requested effort', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'kimi-home-'));
  const write = text => fs.writeFileSync(path.join(home, 'config.toml'), text);
  try {
    write('default_model = "kimi-code/k3-256k"\n[thinking]\nenabled = true\neffort = "max"\n[models.x]\neffort = "low"\n');
    assert.deepEqual(kimiThinking(home), {enabled: true, effort: 'max'});
    write('\uFEFF[thinking]\r\nenabled = true # on\r\neffort = "max"\r\n');
    assert.deepEqual(kimiThinking(home), {enabled: true, effort: 'max'});
    write('[thinking]\nenabled = true\neffort = "low"\n');
    assert.equal(kimiThinking(home).effort, 'low');
    write('[other]\nenabled = true\neffort = "max"\n');
    assert.deepEqual(kimiThinking(home), {enabled: false, effort: null});
  } finally { fs.rmSync(home, {recursive: true, force: true}); }
});

test('agent profile must match its digest and the requested tools', () => {
  const text = profile(readTools);
  const ok = {...config, kimi_profile_sha256: sha(Buffer.from(text))};
  assert.equal(validateKimiProfile(text, ok), text);
  assert.throws(() => validateKimiProfile(text, config), /kimi_profile_hash_mismatch/);
  const wider = profile([...readTools, 'Bash']);
  assert.throws(() => validateKimiProfile(wider, {...config, kimi_profile_sha256: sha(Buffer.from(wider))}),
    /kimi_profile_tools_mismatch/);
});

test('git root is found from nested directories only when one exists', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'kimi-git-'));
  try {
    fs.mkdirSync(path.join(dir, 'repo', 'src', 'deep'), {recursive: true});
    assert.equal(gitRoot(path.join(dir, 'repo', 'src')), null);
    fs.mkdirSync(path.join(dir, 'repo', '.git'));
    assert.equal(gitRoot(path.join(dir, 'repo', 'src', 'deep')), path.join(dir, 'repo'));
  } finally { fs.rmSync(dir, {recursive: true, force: true}); }
});

function sessionHome(rows, {cwd = 'D:\\work\\repo', state = {}} = {}) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'kimi-home-'));
  const dir = path.join(home, 'sessions', 'wd_repo_000000000000', session);
  fs.mkdirSync(path.join(dir, 'agents', 'main'), {recursive: true});
  fs.writeFileSync(path.join(dir, 'agents', 'main', 'wire.jsonl'), bytes(rows));
  fs.writeFileSync(path.join(dir, 'state.json'),
    JSON.stringify({id: session, cwd, lastTurnReason: 'completed', ...state}));
  fs.writeFileSync(path.join(home, 'session_index.jsonl'),
    JSON.stringify({sessionId: session, sessionDir: dir.replace(/\\/g, '/'), workDir: cwd}) + '\n');
  return {home, dir};
}

test('sessions are located by exact id and cwd through the index', () => {
  const {home, dir} = sessionHome(turn());
  try {
    assert.equal(kimiSessionDir(home, 'd:\\WORK\\REPO', session).dir, dir);
    assert.throws(() => kimiSessionDir(home, 'D:\\Other', session), /kimi_session_missing/);
    const index = path.join(home, 'session_index.jsonl');
    fs.appendFileSync(index, fs.readFileSync(index));
    assert.equal(kimiSessionDir(home, 'D:\\work\\repo', session).dir, dir);
    const other = path.join(home, 'sessions', 'wd_other', session);
    fs.mkdirSync(other, {recursive: true});
    fs.appendFileSync(index, JSON.stringify({sessionId: session, sessionDir: other, workDir: 'D:\\work\\repo'}) + '\n');
    assert.throws(() => kimiSessionDir(home, 'D:\\work\\repo', session), /kimi_session_ambiguous/);
    fs.writeFileSync(index, JSON.stringify({sessionId: session, sessionDir: path.join(os.tmpdir(), session),
      workDir: 'D:\\work\\repo'}) + '\n');
    assert.throws(() => kimiSessionDir(home, 'D:\\work\\repo', session), /kimi_session_missing/);
  } finally { fs.rmSync(home, {recursive: true, force: true}); }
});

test('native turn requires one marker, fixed profile, bound tools and a clean ending', () => {
  const good = inspectKimiTurn(bytes(turn('答复 ✅')), expected());
  assert.deepEqual({complete: good.complete, text: good.final_text, requests: good.requests},
    {complete: true, text: '答复 ✅', requests: 1});
  const cases = [
    [turn('x', {model: 'k3'}), /kimi_model_mismatch/],
    [turn('x', {effort: 'high'}), /kimi_effort_mismatch/],
    [turn('x', {tools: [...readTools, 'Bash']}), /kimi_tool_profile_mismatch/],
    [turn('x', {marker: 'f'.repeat(32)}), /kimi_prompt_missing/],
    [[...turn(), ...turn().slice(1)], /kimi_prompt_duplicate/],
    [[...turn(), {type: 'turn.prompt', agentId: 'main', input: [{type: 'text', text: 'user typed more'}]}], /kimi_foreign_turn/],
    [turn('x', {reason: 'error'}), /kimi_turn_not_completed/],
  ];
  for (const [rows, error] of cases) assert.throws(() => inspectKimiTurn(bytes(rows), expected()), error);
  assert.equal(inspectKimiTurn(bytes(turn().slice(0, -1)), expected()).complete, false);
  assert.throws(() => kimiWireRows(Buffer.from('{"type":"x"}')), /kimi_record_partial/);
});

test('a revision is parsed after its baseline, which must end on a record boundary', () => {
  const first = bytes(turn('first round', {marker: 'e'.repeat(32)}));
  const second = Buffer.concat([first, bytes(turn('second round').slice(1))]);
  const result = inspectKimiTurn(second, expected({offset: first.length}));
  assert.equal(result.final_text, 'second round');
  assert.throws(() => inspectKimiTurn(second, expected({offset: first.length - 3})), /kimi_baseline_boundary/);
});

test('evidence binds session files, final text and the revision baseline', () => {
  const {home, dir} = sessionHome(turn('done'));
  try {
    const found = findKimiEvidence(config, 'D:\\work\\repo', home, session, 'done');
    const wire = fs.readFileSync(path.join(dir, 'agents', 'main', 'wire.jsonl'));
    assert.equal(found.session_dir, dir);
    assert.equal(found.wire_sha256, sha(wire));
    assert.equal(found.final_text_sha256, sha(Buffer.from('done')));
    assert.throws(() => findKimiEvidence(config, 'D:\\work\\repo', home, session, 'different'), /kimi_final_text_mismatch/);
    const revision = {...config, session_id: session, baseline_bytes: 5, baseline_sha256: 'e'.repeat(64)};
    assert.throws(() => findKimiEvidence(revision, 'D:\\work\\repo', home, session, 'done'), /kimi_baseline_changed/);
  } finally { fs.rmSync(home, {recursive: true, force: true}); }
  const moved = sessionHome(turn('done'), {state: {cwd: 'D:\\Elsewhere'}});
  try {
    assert.throws(() => findKimiEvidence(config, 'D:\\work\\repo', moved.home, session, 'done'), /kimi_state_mismatch/);
  } finally { fs.rmSync(moved.home, {recursive: true, force: true}); }
});
