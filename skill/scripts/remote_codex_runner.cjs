'use strict';

// Windows-only Codex/Kimi carrier. The SSH connection is the process lifetime:
// closing it terminates this runner and its agent child. Variable task data is
// read from stdin as base64 JSON; PowerShell never receives a prompt or cwd.
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { spawn, spawnSync } = require('child_process');

const PROTOCOL = 1;
const HEARTBEAT_MS = 5000;
const MAX_PACKET = 2 * 1024 * 1024;
const MAX_PROMPT = 1024 * 1024;
const MAX_LINE = 4 * 1024 * 1024;
const MAX_STREAM = 256 * 1024 * 1024;
const MAX_STDERR = 1024 * 1024;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const HEX64_RE = /^[0-9a-f]{64}$/;
const KIMI_TOOLS = ['Read', 'ReadMediaFile', 'Glob', 'Grep', 'Write', 'Edit', 'Bash', 'WebSearch', 'FetchURL', 'TodoList'];
const KIMI_NO_LOCAL_WRITES = new Set(['Read', 'ReadMediaFile', 'Glob', 'Grep', 'WebSearch', 'FetchURL', 'TodoList']);
const KIMI_SESSION_RE = /^session_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
// Kimi takes the task only as -p, and CreateProcess caps a whole command line
// at 32767 UTF-16 units, so the task plus marker stays well below that.
const MAX_KIMI_PROMPT = 24000;
const MAX_KIMI_WIRE = 128 * 1024 * 1024;
const EVIDENCE_CHUNK = 1024 * 1024;
const sleepSync = ms => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const now = () => new Date().toISOString();

function failureSafety(config) {
  // Kimi has no sandbox: any tool that can change local files counts as a write.
  const writes = config.agent === 'kimi'
    ? (config.kimi_tools || []).some(tool => !KIMI_NO_LOCAL_WRITES.has(tool))
    : config.sandbox === 'workspace-write';
  return writes
    ? { partial_write_risk: true, recovery: 'inspect_remote_worktree_before_any_new_request' }
    : { partial_write_risk: false };
}

function emit(config, kind, body = {}) {
  process.stdout.write(JSON.stringify({
    protocol: PROTOCOL,
    token: config.token,
    request_id: config.request_id,
    kind,
    ...body,
  }) + '\n');
}

function fail(code, message) {
  const error = new Error(message || code);
  error.code = code;
  throw error;
}

function atomicWrite(file, data) {
  const temp = `${file}.tmp-${process.pid}-${crypto.randomBytes(5).toString('hex')}`;
  const bytes = Buffer.from(JSON.stringify(data, null, 2) + '\n', 'utf8');
  let fd;
  try {
    fd = fs.openSync(temp, 'wx', 0o600);
    fs.writeFileSync(fd, bytes);
    fs.fsyncSync(fd);
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
  let last;
  for (let attempt = 0; attempt < 6; attempt += 1) {
    try {
      fs.renameSync(temp, file);
      return;
    } catch (error) {
      last = error;
      if (!['EBUSY', 'EPERM', 'EACCES'].includes(error && error.code)) break;
      sleepSync(25 * (attempt + 1));
    }
  }
  try { fs.unlinkSync(temp); } catch {}
  const error = new Error(`atomic_rename_failed:${last && last.code || 'unknown'}`);
  error.code = 'atomic_rename_failed';
  throw error;
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function validateWindowsPath(raw, label) {
  if (typeof raw !== 'string' || !/^[A-Za-z]:[\\/]/.test(raw) || /^\\\\/.test(raw)) {
    fail(`bad_${label}`, `${label} must be an absolute local-drive path`);
  }
  if (/\0/.test(raw) || /:[^\\/]/.test(raw.slice(2))) fail(`bad_${label}`, `${label} contains ADS syntax`);
  const parts = raw.slice(3).split(/[\\/]+/).filter(Boolean);
  if (parts.some(part => part === '..' || /[ .]$/.test(part) || /~\d(?:\.|$)/i.test(part))) {
    fail(`bad_${label}`, `${label} contains a refused path segment`);
  }
  return path.win32.resolve(raw);
}

function canonicalDirectory(raw, label) {
  const resolved = validateWindowsPath(raw, label);
  let stat;
  try { stat = fs.statSync(resolved); } catch { fail(`bad_${label}`, `${label} is unavailable`); }
  if (!stat.isDirectory()) fail(`bad_${label}`, `${label} is not a directory`);
  return fs.realpathSync.native ? fs.realpathSync.native(resolved) : fs.realpathSync(resolved);
}

function isWithin(root, candidate) {
  const base = path.win32.resolve(root).replace(/[\\/]+$/, '').toLowerCase();
  const value = path.win32.resolve(candidate).replace(/[\\/]+$/, '').toLowerCase();
  return value === base || value.startsWith(base + '\\');
}

function validateConfig(config, { promptRequired }) {
  if (!config || config.protocol !== PROTOCOL) fail('bad_protocol');
  if (!UUID_RE.test(config.owner || '') || !UUID_RE.test(config.request_id || '')) fail('bad_identity');
  if (!/^[0-9a-f]{64}$/.test(config.token || '')) fail('bad_token');
  if (!['codex', 'kimi'].includes(config.agent)) fail('unsupported_remote_agent');
  if (!['dispatch', 'receipt', 'reclaim', 'evidence'].includes(config.action)) fail('bad_action');
  if (typeof config.site !== 'string' || !config.site || config.site.length > 121) fail('bad_site');
  if (typeof config.host !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$/.test(config.host)) fail('bad_host');
  if (!HEX64_RE.test(config.request_digest || '')) fail('bad_request_digest');
  if (promptRequired && !HEX64_RE.test(config.prompt_sha256 || '')) fail('bad_prompt_hash');
  if (config.agent === 'kimi') validateKimiConfig(config);
  else {
    if (config.action === 'evidence') fail('bad_action');
    if (!/^[A-Za-z0-9._-]{1,100}$/.test(config.model || '')) fail('bad_model');
    if (!['low', 'medium', 'high', 'xhigh', 'max', 'ultra'].includes(config.effort)) fail('bad_effort');
    if (!['read-only', 'workspace-write'].includes(config.sandbox)) fail('bad_sandbox');
    if (!['elevated', 'unelevated'].includes(config.windows_sandbox)) fail('bad_windows_sandbox');
    for (const key of ['add_dirs', 'add_dir_roots']) {
      const value = config[key] === undefined ? [] : config[key];
      if (!Array.isArray(value) || value.length > 4) fail(`bad_${key}`);
      value.forEach(dir => validateWindowsPath(dir, key));
    }
    // Extra writable directories only widen a workspace-write sandbox.
    if ((config.add_dirs || []).length && config.sandbox !== 'workspace-write') fail('add_dirs_need_workspace_write');
  }
  if (!Number.isInteger(config.timeout_seconds) || config.timeout_seconds < 30 || config.timeout_seconds > 86400) fail('bad_timeout');
  if (!Number.isInteger(config.keepalive_window_seconds) || config.keepalive_window_seconds < 60
      || config.keepalive_window_seconds > 3600) fail('bad_keepalive_window');
  if (!Array.isArray(config.allowed_roots) || config.allowed_roots.length === 0) fail('bad_cwd_roots');
  if (typeof config.allow_non_git !== 'boolean') fail('bad_allow_non_git');
  if (config.action === 'reclaim' && !HEX64_RE.test(config.inspection_sha256 || '')) fail('bad_inspection_hash');
  return config;
}

function validateKimiConfig(config) {
  if (!/^[A-Za-z0-9._-]{1,60}\/[A-Za-z0-9._-]{1,60}$/.test(config.model || '')) fail('bad_model');
  if (!['low', 'medium', 'high', 'max'].includes(config.effort)) fail('bad_effort');
  const tools = config.kimi_tools;
  if (!Array.isArray(tools) || !tools.length || tools.some(tool => !KIMI_TOOLS.includes(tool))
      || new Set(tools).size !== tools.length) fail('bad_kimi_tools');
  validateWindowsPath(config.kimi_home, 'kimi_home');
  if (!/^[0-9a-f]{32}$/.test(config.run_marker || '')) fail('bad_run_marker');
  if (config.session_id !== null && config.session_id !== undefined) {
    if (!KIMI_SESSION_RE.test(config.session_id)) fail('bad_session');
    if (!Number.isInteger(config.baseline_bytes) || config.baseline_bytes <= 0
        || !HEX64_RE.test(config.baseline_sha256 || '')) fail('bad_baseline');
  } else if (config.action === 'dispatch' && !HEX64_RE.test(config.kimi_profile_sha256 || '')) {
    fail('bad_kimi_profile');
  }
  if (config.action === 'evidence' && !KIMI_SESSION_RE.test(config.evidence_session || '')) fail('bad_session');
}

function where(name) {
  const result = spawnSync('where.exe', [name], { encoding: 'utf8', windowsHide: true, timeout: 5000 });
  if (result.status !== 0) return [];
  return String(result.stdout || '').split(/\r?\n/).map(value => value.trim()).filter(Boolean);
}

function resolveCodex() {
  const native = where('codex.exe').find(candidate => {
    try { return fs.statSync(candidate).isFile(); } catch { return false; }
  });
  if (native) return { command: native, prefix: [], source: 'native-exe', codex_path: native };

  const shims = [...where('codex.cmd'), ...where('codex.ps1')];
  for (const shim of shims) {
    let source;
    try { source = fs.readFileSync(shim, 'utf8').slice(0, 64 * 1024); } catch { continue; }
    if (!/node_modules[\\/]@openai[\\/]codex[\\/]bin[\\/]codex\.js/i.test(source)) continue;
    const script = path.win32.join(path.win32.dirname(shim), 'node_modules', '@openai', 'codex', 'bin', 'codex.js');
    if (!fs.existsSync(script)) continue;
    const node = where('node.exe')[0];
    if (!node || !fs.existsSync(node)) continue;
    return { command: node, prefix: [script], source: 'npm-shim-direct', codex_path: script };
  }
  fail('codex_unavailable', 'no safe Codex executable or recognized npm shim');
}

function cliVersion(invocation, code, timeout = 10000) {
  const result = spawnSync(invocation.command, [...invocation.prefix, '--version'], {
    encoding: 'utf8', windowsHide: true, timeout,
  });
  const value = String(result.stdout || result.stderr || '').trim().split(/\r?\n/)[0];
  if (result.status !== 0 || !value) fail(code);
  return value;
}

function codexVersion(invocation) {
  return cliVersion(invocation, 'codex_probe_failed');
}

function kimiScriptFromShim(shim, source) {
  if (!/node_modules[\\/]@moonshot-ai[\\/]kimi-code[\\/]dist[\\/]main\.mjs/i.test(source)) return null;
  return path.win32.join(path.win32.dirname(shim), 'node_modules', '@moonshot-ai', 'kimi-code', 'dist', 'main.mjs');
}

function resolveKimi() {
  const native = where('kimi.exe').find(candidate => {
    try { return fs.statSync(candidate).isFile(); } catch { return false; }
  });
  if (native) return { command: native, prefix: [], source: 'native-exe', kimi_path: native };
  for (const shim of [...where('kimi.cmd'), ...where('kimi.ps1')]) {
    let source;
    try { source = fs.readFileSync(shim, 'utf8').slice(0, 64 * 1024); } catch { continue; }
    const script = kimiScriptFromShim(shim, source);
    if (!script || !fs.existsSync(script)) continue;
    const node = where('node.exe')[0];
    if (!node || !fs.existsSync(node)) continue;
    // Launch node directly: a .cmd shim would route -p through cmd.exe, which
    // caps a command line at 8191 characters.
    return { command: node, prefix: [script], source: 'npm-shim-direct', kimi_path: script };
  }
  fail('kimi_unavailable', 'no safe Kimi executable or recognized npm shim');
}

// Kimi has no per-run effort option; like the macOS adapter, run only when the
// existing [thinking] section already selects the requested effort.
function kimiThinking(home) {
  let text;
  try { text = fs.readFileSync(path.join(home, 'config.toml'), 'utf8'); }
  catch { fail('kimi_config_unavailable'); }
  const lines = text.replace(/^﻿/, '').split(/\r?\n/);
  const start = lines.findIndex(line => /^\[thinking\]\s*$/.test(line));
  if (start < 0) return { enabled: false, effort: null };
  const section = [];
  for (const line of lines.slice(start + 1)) {
    if (line.startsWith('[')) break;
    section.push(line);
  }
  const effort = section.map(line => /^effort\s*=\s*["']([A-Za-z]+)["']\s*(?:#.*)?$/.exec(line)).find(Boolean);
  return { enabled: section.some(line => /^enabled\s*=\s*true\s*(?:#.*)?$/.test(line)),
    effort: effort ? effort[1] : null };
}

function validateKimiProfile(profile, config) {
  if (typeof profile !== 'string' || !profile || Buffer.byteLength(profile) > 16 * 1024) fail('bad_kimi_profile');
  if (sha(Buffer.from(profile, 'utf8')) !== config.kimi_profile_sha256) fail('kimi_profile_hash_mismatch');
  const lines = profile.split('\n');
  const toolsLine = lines.find(line => line.startsWith('tools: '));
  let tools = null;
  try { tools = JSON.parse(toolsLine.slice('tools: '.length)); } catch { fail('bad_kimi_profile'); }
  if (!Array.isArray(tools) || JSON.stringify([...tools].sort()) !== JSON.stringify([...config.kimi_tools].sort())) {
    fail('kimi_profile_tools_mismatch');
  }
  if (!lines.includes('subagents: []')) fail('bad_kimi_profile');
  return profile;
}

function gitRoot(cwd) {
  for (let dir = cwd; ;) {
    if (fs.existsSync(path.join(dir, '.git'))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

function sameWindowsPath(a, b) {
  const norm = value => path.win32.resolve(String(value || '')).replace(/[\\/]+$/, '').toLowerCase();
  return norm(a) === norm(b);
}

// Locate one session by exact id and cwd through Kimi's own index, never by recency.
function kimiSessionDir(home, cwd, session) {
  const index = path.join(home, 'session_index.jsonl');
  let stat;
  try { stat = fs.statSync(index); } catch { fail('kimi_session_missing'); }
  if (stat.size > 64 * 1024 * 1024) fail('kimi_index_size_limit');
  const sessions = path.join(home, 'sessions');
  const found = new Map();
  for (const line of fs.readFileSync(index, 'utf8').split(/\r?\n/)) {
    if (!line.trim()) continue;
    let row;
    try { row = JSON.parse(line); } catch { continue; }
    if (!row || row.sessionId !== session || !sameWindowsPath(row.workDir, cwd)) continue;
    const dir = path.resolve(String(row.sessionDir || ''));
    if (path.basename(dir) !== session || !isWithin(sessions, dir) || sameWindowsPath(sessions, dir)) continue;
    found.set(dir.toLowerCase(), { dir, row });
  }
  if (found.size !== 1) fail(found.size ? 'kimi_session_ambiguous' : 'kimi_session_missing');
  return [...found.values()][0];
}

function readKimiWire(file) {
  let stat;
  try { stat = fs.statSync(file); } catch { fail('kimi_wire_unavailable'); }
  if (!stat.isFile() || stat.size <= 0) fail('kimi_wire_unavailable');
  if (stat.size > MAX_KIMI_WIRE) fail('kimi_wire_size_limit');
  return fs.readFileSync(file);
}

function kimiWireRows(buffer, offset = 0) {
  if (offset > buffer.length || (offset > 0 && buffer[offset - 1] !== 10)) fail('kimi_baseline_boundary');
  const rows = [];
  for (let start = offset; start < buffer.length;) {
    const end = buffer.indexOf(10, start);
    if (end < 0) fail('kimi_record_partial');
    const line = buffer.subarray(start, end);
    if (line.length > MAX_LINE) fail('kimi_record_size_limit');
    let row;
    try { row = JSON.parse(line.toString('utf8')); } catch { fail('kimi_record_invalid'); }
    if (!row || typeof row !== 'object' || Array.isArray(row)) fail('kimi_record_invalid');
    rows.push(row);
    start = end + 1;
  }
  return rows;
}

function isKimiPrompt(row, marker) {
  return row.type === 'turn.prompt' && row.agentId === 'main' && Array.isArray(row.input)
    && row.input.some(part => part && part.type === 'text' && typeof part.text === 'string'
      && part.text.startsWith(`[delegation-run: ${marker}]\n`));
}

// First parse on Windows. The Mac repeats every check in Python on fetched bytes.
function inspectKimiTurn(buffer, expected) {
  const whole = kimiWireRows(buffer);
  const native = expected.offset ? kimiWireRows(buffer, expected.offset) : whole;
  const starts = native.flatMap((row, index) => (isKimiPrompt(row, expected.marker) ? [index] : []));
  if (starts.length !== 1) fail(starts.length ? 'kimi_prompt_duplicate' : 'kimi_prompt_missing');
  const active = native.slice(starts[0]).filter(row => row.agentId === 'main');
  if (active.filter(row => row.type === 'turn.prompt').length !== 1) fail('kimi_foreign_turn');
  const requests = active.filter(row => row.type === 'llm.request' && row.kind === 'loop');
  const raw = expected.model.slice(expected.model.indexOf('/') + 1);
  if (requests.some(row => row.modelAlias !== expected.model || row.model !== raw)) fail('kimi_model_mismatch');
  if (requests.some(row => row.thinkingEffort !== expected.effort)) fail('kimi_effort_mismatch');
  const binds = whole.filter(row => row.type === 'profile.bind' && row.agentId === 'main');
  const bound = binds.length ? [...(binds[binds.length - 1].activeToolNames || [])].sort() : [];
  if (JSON.stringify(bound) !== JSON.stringify([...expected.tools].sort())) fail('kimi_tool_profile_mismatch');
  const endings = active.filter(row => row.type === 'turn.ended');
  if (endings.length > 1 || (endings.length === 1 && endings[0].reason !== 'completed')) fail('kimi_turn_not_completed');
  const events = active.filter(row => row.type === 'context.append_loop_event').map(row => row.event || {});
  const steps = events.filter(event => event.type === 'step.end');
  const last = steps.length ? steps[steps.length - 1] : null;
  const finalText = last ? events.filter(event => event.type === 'content.part' && event.stepUuid === last.uuid
    && event.part && event.part.type === 'text').map(event => event.part.text || '').join('') : '';
  return {
    complete: requests.length > 0 && endings.length === 1 && !!last && last.finishReason === 'end_turn',
    requests: requests.length, tools: bound, final_text: finalText,
  };
}

function findKimiEvidence(config, cwd, home, session, finalText) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      const { dir } = kimiSessionDir(home, cwd, session);
      const stateBytes = fs.readFileSync(path.join(dir, 'state.json'));
      const state = JSON.parse(stateBytes.toString('utf8'));
      if (state.id !== session || !sameWindowsPath(state.cwd, cwd)) fail('kimi_state_mismatch');
      const wire = readKimiWire(path.join(dir, 'agents', 'main', 'wire.jsonl'));
      const offset = config.session_id ? config.baseline_bytes : 0;
      if (offset && (wire.length < offset || sha(wire.subarray(0, offset)) !== config.baseline_sha256)) {
        fail('kimi_baseline_changed');
      }
      const turn = inspectKimiTurn(wire, { marker: config.run_marker, offset, model: config.model,
        effort: config.effort, tools: config.kimi_tools });
      if (turn.complete && state.lastTurnReason === 'completed') {
        if (!finalText || turn.final_text !== finalText) fail('kimi_final_text_mismatch');
        return { session, session_dir: dir, wire_bytes: wire.length, wire_sha256: sha(wire),
          state_sha256: sha(stateBytes), kimi_tools: turn.tools, llm_requests: turn.requests,
          final_text_sha256: sha(Buffer.from(finalText, 'utf8')) };
      }
    } catch (error) {
      const transient = error && (error.name === 'SyntaxError' || ['kimi_record_partial', 'kimi_session_missing',
        'kimi_prompt_missing', 'kimi_wire_unavailable', 'ENOENT'].includes(error.code));
      if (!transient) throw error;
    }
    sleepSync(250);
  }
  fail('kimi_evidence_incomplete');
}

function walkSessionLogs(root, session, out, budget = { count: 0 }) {
  if (budget.count > 50000) fail('session_scan_limit');
  let entries;
  try { entries = fs.readdirSync(root, { withFileTypes: true }); } catch { return; }
  for (const entry of entries) {
    budget.count += 1;
    if (budget.count > 50000) fail('session_scan_limit');
    const full = path.join(root, entry.name);
    if (entry.isDirectory()) walkSessionLogs(full, session, out, budget);
    else if (entry.isFile() && entry.name.endsWith(`${session}.jsonl`)) out.push(full);
  }
}

function forEachRolloutRecord(log, consume) {
  const stat = fs.statSync(log);
  if (!stat.isFile() || stat.size <= 0) fail('session_log_unavailable');
  if (stat.size > MAX_STREAM) fail('session_log_size_limit');
  const fd = fs.openSync(log, 'r');
  let offset = 0, pending = Buffer.alloc(0);
  try {
    while (offset < stat.size) {
      const chunk = Buffer.alloc(Math.min(65536, stat.size - offset));
      const count = fs.readSync(fd, chunk, 0, chunk.length, offset);
      if (!count) break;
      offset += count;
      pending = Buffer.concat([pending, chunk.subarray(0, count)]);
      if (pending.length > MAX_LINE * 2) fail('session_record_size_limit');
      let end;
      while ((end = pending.indexOf(10)) >= 0) {
        const line = pending.subarray(0, end); pending = pending.subarray(end + 1);
        if (line.length > MAX_LINE) fail('session_record_size_limit');
        if (!line.toString('utf8').trim()) continue;
        let record;
        try { record = JSON.parse(line.toString('utf8')); }
        catch { fail('session_record_invalid'); }
        consume(record);
      }
    }
    if (pending.toString('utf8').trim()) fail('session_record_partial');
  } finally { fs.closeSync(fd); }
}

function inspectRollout(log, expected) {
  let meta = false, startedCount = 0;
  const turns = new Set(), contexts = new Map(), completions = new Map();
  forEachRolloutRecord(log, record => {
    const payload = record.payload || {};
    if (record.type === 'session_meta') {
      if (meta) fail('duplicate_session_meta');
      const id = payload.id || payload.session_id;
      if (id !== expected.session) fail('session_log_mismatch');
      if (path.win32.resolve(payload.cwd || '').toLowerCase() !== path.win32.resolve(expected.cwd).toLowerCase()) {
        fail('session_cwd_mismatch');
      }
      meta = true;
    }
    if (record.type === 'event_msg' && payload.type === 'task_started') {
      if (!payload.turn_id) fail('missing_turn_identity');
      startedCount += 1;
      turns.add(payload.turn_id);
    }
    if (record.type === 'turn_context' && payload.turn_id) {
      const policy = payload.sandbox_policy || {};
      const value = {
        model: payload.model || null,
        effort: payload.effort || null,
        sandbox: policy.type || null,
        approval_policy: payload.approval_policy || null,
        network_access: typeof policy.network_access === 'boolean' ? policy.network_access : null,
        writable_roots: Array.isArray(policy.writable_roots) ? policy.writable_roots.map(String) : [],
      };
      const prior = contexts.get(payload.turn_id);
      if (prior && JSON.stringify(prior) !== JSON.stringify(value)) fail('session_context_ambiguous');
      contexts.set(payload.turn_id, value);
    }
    if (record.type === 'event_msg' && payload.type === 'task_complete' && payload.turn_id) {
      if (completions.has(payload.turn_id)) fail('session_completion_ambiguous');
      completions.set(payload.turn_id,
        typeof payload.last_agent_message === 'string' ? payload.last_agent_message : '');
    }
  });
  if (turns.size !== 1 || startedCount !== 1) fail('session_turn_ambiguous');
  const turn = [...turns][0], context = contexts.get(turn) || {};
  const complete = completions.has(turn), finalText = completions.get(turn) || '';
  return { meta, turn, model: context.model || null, effort: context.effort || null,
    sandbox: context.sandbox || null, approval_policy: context.approval_policy || null,
    network_access: context.network_access === undefined ? null : context.network_access,
    writable_roots: context.writable_roots || [], complete,
    final_text_sha256: finalText ? sha(Buffer.from(finalText)) : null };
}

function sameRoots(actual, expected) {
  const key = dirs => dirs.map(dir => path.win32.resolve(String(dir)).replace(/[\\/]+$/, '').toLowerCase()).sort();
  return JSON.stringify(key(actual || [])) === JSON.stringify(key(expected || []));
}

function findEvidence(config, session, addDirs = []) {
  const root = path.win32.join(config.codex_home, 'sessions');
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const matches = [];
    walkSessionLogs(root, session, matches);
    if (matches.length > 1) fail('session_log_ambiguous');
    if (matches.length === 1) {
      try {
        const evidence = inspectRollout(matches[0], { session, cwd: config.cwd });
        if (evidence.model && evidence.model !== config.model) fail('session_model_mismatch');
        if (evidence.effort && evidence.effort !== config.effort) fail('session_effort_mismatch');
        if (evidence.sandbox && evidence.sandbox !== config.sandbox) fail('session_sandbox_mismatch');
        if (evidence.approval_policy && evidence.approval_policy !== 'never') fail('session_approval_mismatch');
        if (evidence.network_access === true) fail('session_network_mismatch');
        if (evidence.turn && !sameRoots(evidence.writable_roots, addDirs)) fail('session_writable_roots_mismatch');
        if (evidence.turn && evidence.model && evidence.effort && evidence.sandbox
            && evidence.approval_policy && evidence.complete) {
          return { log: matches[0], ...evidence };
        }
      } catch (error) {
        if (!error || error.code !== 'session_record_partial') throw error;
      }
    }
    sleepSync(250);
  }
  fail('session_evidence_incomplete');
}

function parseProcessProbe(result, expectedStart = null) {
  if (!result || result.error || result.signal || result.status === null) return { state: 'probe_failed' };
  if (result.status === 3) return { state: 'gone' };
  if (result.status !== 0) return { state: 'probe_failed' };
  let value;
  try { value = JSON.parse(String(result.stdout || '').trim()); }
  catch { return { state: 'probe_failed' }; }
  if (!value || value.state !== 'alive' || typeof value.start_time !== 'string' || !value.start_time) {
    return { state: 'probe_failed' };
  }
  if (expectedStart && value.start_time !== expectedStart) return { state: 'gone', reason: 'identity_mismatch' };
  return { state: 'alive', start_time: value.start_time };
}

function processIdentity(pid, expectedStart = null) {
  if (!Number.isInteger(pid) || pid <= 0) return { state: 'probe_failed' };
  const command = `$ErrorActionPreference='Stop'; try { `
    + `$p=Get-Process -Id ${pid} -ErrorAction Stop; `
    + `[pscustomobject]@{state='alive';start_time=$p.StartTime.ToUniversalTime().ToString('o')} | ConvertTo-Json -Compress; exit 0 `
    + `} catch { if ($_.FullyQualifiedErrorId -like 'NoProcessFoundForGivenId*') { `
    + `[pscustomobject]@{state='gone'} | ConvertTo-Json -Compress; exit 3 }; exit 4 }`;
  const result = spawnSync('powershell.exe', ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command], {
    encoding: 'utf8', windowsHide: true, timeout: 5000,
  });
  return parseProcessProbe(result, expectedStart);
}

function jobPaths(config) {
  const root = path.win32.join(os.homedir(), '.codex', 'remote-delegate', 'tasks');
  return {
    root,
    job: path.win32.join(root, 'jobs', config.request_id),
    request: path.win32.join(root, 'jobs', config.request_id, 'request.json'),
    receipt: path.win32.join(root, 'jobs', config.request_id, 'receipt.json'),
    stdout: path.win32.join(root, 'jobs', config.request_id, 'stdout.ndjson'),
    stderr: path.win32.join(root, 'jobs', config.request_id, 'stderr.log'),
    locks: path.win32.join(root, 'locks'),
  };
}

function releaseLock(lock, requestId) {
  try {
    const owner = readJson(path.join(lock, 'owner.json'));
    if (owner.request_id !== requestId) return;
    fs.unlinkSync(path.join(lock, 'owner.json'));
    fs.rmdirSync(lock);
  } catch {}
}

function emitReceipt(config, receipt) {
  emit(config, 'receipt', { receipt });
}

function reconcileRunningReceipt(receipt, age, processState, classifiedAt = now()) {
  const windowMs = Number(receipt.keepalive_window_seconds || 300) * 1000;
  if (processState.state === 'gone' && Number.isFinite(age) && age >= windowMs) {
    return { ...receipt, status: 'killed_by_carrier_loss', classified_at: classifiedAt,
      partial_write_risk: true, recovery: 'inspect_remote_worktree_before_any_new_request' };
  }
  return { ...receipt, process_state: processState.state,
    reconciliation: processState.state === 'alive' || age < windowMs ? 'pending' : 'unknown' };
}

function queryReceipt(config) {
  const paths = jobPaths(config);
  if (!fs.existsSync(paths.receipt) || !fs.existsSync(paths.request)) fail('receipt_not_found');
  const request = readJson(paths.request);
  if (request.owner !== config.owner || request.request_digest !== config.request_digest) fail('request_conflict');
  const receipt = readJson(paths.receipt);
  if (receipt.status === 'running' || receipt.status === 'starting') {
    const age = Date.now() - Date.parse(receipt.heartbeat_at || receipt.started_at || 0);
    const processState = processIdentity(receipt.process_pid, receipt.process_start_time);
    const next = reconcileRunningReceipt(receipt, age, processState);
    if (next.status === 'killed_by_carrier_loss') {
      atomicWrite(paths.receipt, next);
      emitReceipt(config, next);
      return;
    }
    emitReceipt(config, next);
    return;
  }
  emitReceipt(config, receipt);
}

function reclaimLock(config) {
  const paths = jobPaths(config);
  if (!fs.existsSync(paths.receipt) || !fs.existsSync(paths.request)) fail('receipt_not_found');
  const request = readJson(paths.request), receipt = readJson(paths.receipt);
  if (request.owner !== config.owner || request.request_digest !== config.request_digest) fail('request_conflict');
  if (receipt.status !== 'killed_by_carrier_loss') fail('reclaim_status_not_eligible');
  const cwd = canonicalDirectory(request.cwd, 'cwd');
  const lock = path.win32.join(paths.locks, sha(Buffer.from(cwd.toLowerCase())));
  const ownerFile = path.join(lock, 'owner.json');
  if (!fs.existsSync(ownerFile)) fail('lock_not_found');
  const owner = readJson(ownerFile);
  const age = Date.now() - Date.parse(owner.heartbeat_at || receipt.heartbeat_at || 0);
  const processState = processIdentity(owner.process_pid, owner.process_start_time);
  validateReclaimEvidence(config, receipt, owner, cwd, age, processState);
  fs.unlinkSync(ownerFile);
  fs.rmdirSync(lock);
  const next = { ...receipt, lock_reclaimed: true, lock_reclaimed_at: now(),
    inspection_sha256: config.inspection_sha256 };
  atomicWrite(paths.receipt, next);
  emitReceipt(config, next);
}

function validateReclaimEvidence(config, receipt, owner, cwd, age, processState) {
  if (owner.request_id !== config.request_id || String(owner.cwd || '').toLowerCase() !== cwd.toLowerCase()) {
    fail('lock_owner_mismatch');
  }
  const windowMs = Number(receipt.keepalive_window_seconds || 300) * 1000;
  if (!Number.isFinite(age) || age < windowMs) fail('lock_not_stale');
  if (processState.state !== 'gone') {
    fail(processState.state === 'alive' ? 'process_still_alive' : 'process_probe_failed');
  }
  return true;
}

// Realpaths close junction and symlink escapes that a string check on the Mac cannot see.
function codexAddDirs(config) {
  const roots = (config.add_dir_roots || []).map(value => canonicalDirectory(value, 'add_dir_root'));
  return (config.add_dirs || []).map(value => {
    const dir = canonicalDirectory(value, 'add_dir');
    if (!roots.some(root => isWithin(root, dir))) fail('add_dir_not_allowed');
    return dir;
  });
}

function prepareCodex(config, cwd, home, prompt) {
  const addDirs = codexAddDirs(config);
  const invocation = resolveCodex();
  const version = codexVersion(invocation);
  const env = { ...process.env, CODEX_HOME: home };
  delete env.CODEX_SQLITE_HOME;
  const args = [...invocation.prefix, 'exec', '--ignore-user-config', '--ignore-rules',
    '-m', config.model, '-c', `model_reasoning_effort="${config.effort}"`,
    '-c', `sandbox_mode="${config.sandbox}"`, '-c', 'approval_policy="never"',
    '-c', `windows.sandbox="${config.windows_sandbox}"`,
    '--sandbox', config.sandbox, '--json', '-C', cwd,
    ...addDirs.flatMap(dir => ['--add-dir', dir]),
    ...(config.allow_non_git ? ['--skip-git-repo-check'] : []), '-'];
  return {
    command: invocation.command, args, env, stdin: prompt, frame: 'codex', version,
    fields: { codex_path: invocation.codex_path, codex_invocation: invocation.source, add_dirs: addDirs },
    observe(event, run) {
      if (event.type === 'thread.started' && UUID_RE.test(event.thread_id || '')) run.session = event.thread_id;
      if (event.type === 'turn.completed') run.turnCompleted = true;
    },
    ready: (code, run) => code === 0 && run.turnCompleted && !!run.session,
    evidence: run => findEvidence(config, run.session, addDirs),
    exitError: code => `codex_exit_${code === null ? 'unknown' : code}`,
  };
}

function prepareKimi(config, cwd, home, prompt, profile, paths) {
  const text = prompt.toString('utf8');
  if (!text.startsWith(`[delegation-run: ${config.run_marker}]\n`)) fail('kimi_marker_missing');
  if (text.length > MAX_KIMI_PROMPT || text.includes('\0')) fail('kimi_prompt_too_long');
  // Kimi has no git check of its own; keep the policy's git-only rule here.
  if (!config.allow_non_git && !gitRoot(cwd)) fail('not_a_git_repository');
  const thinking = kimiThinking(home);
  if (!thinking.enabled || thinking.effort !== config.effort) fail('kimi_effort_config');
  const skills = path.join(paths.job, 'empty-skills');
  fs.mkdirSync(skills, { recursive: true, mode: 0o700 });
  let binding;
  if (config.session_id) {
    // Continue only the exact history the Mac verified. A session that was
    // continued elsewhere since then is refused rather than adopted.
    const { dir } = kimiSessionDir(home, cwd, config.session_id);
    const wire = readKimiWire(path.join(dir, 'agents', 'main', 'wire.jsonl'));
    if (wire.length !== config.baseline_bytes || sha(wire) !== config.baseline_sha256) fail('kimi_session_changed');
    binding = ['--session', config.session_id];
  } else {
    const file = path.join(paths.job, 'agent.md');
    fs.writeFileSync(file, validateKimiProfile(profile, config), { encoding: 'utf8', mode: 0o600, flag: 'wx' });
    binding = ['--agent-file', file];
  }
  const invocation = resolveKimi();
  const version = cliVersion(invocation, 'kimi_probe_failed', 30000);
  const args = [...invocation.prefix, '-m', config.model, '--output-format', 'stream-json',
    '--skills-dir', skills, '-p', text, ...binding];
  return {
    command: invocation.command, args, env: { ...process.env, KIMI_CODE_HOME: home }, stdin: null,
    frame: 'kimi', version, fields: { kimi_path: invocation.kimi_path, kimi_invocation: invocation.source },
    observe(event, run) {
      if (event.type === 'session.resume_hint') {
        if (!KIMI_SESSION_RE.test(event.session_id || '') || (run.session && run.session !== event.session_id)) {
          run.hintConflict = true;
        } else run.session = event.session_id;
      }
      if (event.role === 'assistant' && typeof event.content === 'string') run.finalText = event.content;
    },
    ready: (code, run) => code === 0 && !!run.session && !run.hintConflict
      && (!config.session_id || run.session === config.session_id),
    evidence: run => findKimiEvidence(config, cwd, home, run.session, run.finalText || ''),
    exitError: code => `kimi_exit_${code === null ? 'unknown' : code}`,
  };
}

// Raw native bytes for the Mac's independent Python parse, in bounded chunks.
function emitKimiEvidence(config) {
  const paths = jobPaths(config);
  if (!fs.existsSync(paths.receipt) || !fs.existsSync(paths.request)) fail('receipt_not_found');
  const request = readJson(paths.request), receipt = readJson(paths.receipt);
  if (request.owner !== config.owner || request.request_digest !== config.request_digest) fail('request_conflict');
  if (request.agent !== 'kimi' || receipt.session !== config.evidence_session) fail('evidence_session_mismatch');
  const { dir, row } = kimiSessionDir(request.kimi_home, request.cwd, config.evidence_session);
  const stateBytes = fs.readFileSync(path.join(dir, 'state.json'));
  const wire = readKimiWire(path.join(dir, 'agents', 'main', 'wire.jsonl'));
  const digest = sha(wire);
  emit(config, 'evidence_meta', { session: config.evidence_session, session_dir: dir, index: row,
    state_b64: stateBytes.toString('base64'), wire_bytes: wire.length, wire_sha256: digest });
  for (let offset = 0; offset < wire.length; offset += EVIDENCE_CHUNK) {
    emit(config, 'evidence_chunk', { offset,
      data_b64: wire.subarray(offset, Math.min(offset + EVIDENCE_CHUNK, wire.length)).toString('base64') });
  }
  emit(config, 'evidence_end', { wire_bytes: wire.length, wire_sha256: digest });
}

function dispatch(config, prompt, profile = null) {
  const paths = jobPaths(config);
  fs.mkdirSync(path.win32.join(paths.root, 'jobs'), { recursive: true, mode: 0o700 });
  fs.mkdirSync(paths.locks, { recursive: true, mode: 0o700 });

  const kimi = config.agent === 'kimi';
  const cwd = canonicalDirectory(config.cwd, 'cwd');
  const home = kimi ? canonicalDirectory(config.kimi_home, 'kimi_home')
    : canonicalDirectory(config.codex_home, 'codex_home');
  const roots = config.allowed_roots.map(value => canonicalDirectory(value, 'cwd_root'));
  if (!roots.some(root => isWithin(root, cwd))) fail('cwd_not_allowed');
  if (sha(prompt) !== config.prompt_sha256) fail('prompt_hash_mismatch');
  if (prompt.length === 0 || prompt.length > MAX_PROMPT) fail('bad_prompt_size');

  let created = false;
  try { fs.mkdirSync(paths.job, { mode: 0o700 }); created = true; }
  catch (error) { if (!error || error.code !== 'EEXIST') throw error; }
  if (!created) {
    const request = readJson(paths.request);
    if (request.owner !== config.owner || request.request_digest !== config.request_digest) fail('request_conflict');
    emitReceipt(config, readJson(paths.receipt));
    return;
  }

  const request = {
    protocol: PROTOCOL, owner: config.owner, request_id: config.request_id,
    request_digest: config.request_digest, site: config.site, host: config.host, agent: config.agent, cwd,
    ...(kimi ? { kimi_home: home, kimi_tools: config.kimi_tools, session_id: config.session_id || null,
      run_marker: config.run_marker }
      : { codex_home: home, sandbox: config.sandbox, windows_sandbox: config.windows_sandbox,
        add_dirs: config.add_dirs || [] }),
    model: config.model, effort: config.effort, timeout_seconds: config.timeout_seconds,
    prompt_sha256: config.prompt_sha256, accepted_at: now(),
  };
  atomicWrite(paths.request, request);
  let receipt = { ...request, status: 'accepted', keepalive_window_seconds: config.keepalive_window_seconds,
    heartbeat_at: now(), partial_write_risk: false };
  atomicWrite(paths.receipt, receipt);

  const lock = path.win32.join(paths.locks, sha(Buffer.from(cwd.toLowerCase())));
  try { fs.mkdirSync(lock, { mode: 0o700 }); }
  catch (error) {
    receipt = { ...receipt, status: 'cwd_busy', failed_at: now(), recovery: 'manual_lock_inspection_required' };
    atomicWrite(paths.receipt, receipt); emitReceipt(config, receipt); return;
  }
  atomicWrite(path.join(lock, 'owner.json'), {
    host: config.host, request_id: config.request_id, cwd, started_at: now(), heartbeat_at: now(),
  });

  let launch;
  try {
    launch = kimi ? prepareKimi(config, cwd, home, prompt, profile, paths) : prepareCodex(config, cwd, home, prompt);
  } catch (error) {
    receipt = { ...receipt, status: 'failed', error: error.code || error.message, failed_at: now() };
    atomicWrite(paths.receipt, receipt); releaseLock(lock, config.request_id); emitReceipt(config, receipt); return;
  }
  const child = spawn(launch.command, launch.args, { cwd, env: launch.env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  child.once('error', () => {});
  const startProbe = processIdentity(child.pid);
  if (startProbe.state !== 'alive') {
    if (Number.isInteger(child.pid)) spawnSync('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], {
      windowsHide: true, timeout: 15000, encoding: 'utf8',
    });
    for (const stream of [child.stdin, child.stdout, child.stderr]) {
      try { stream.destroy(); } catch {}
    }
    receipt = { ...receipt, status: 'failed', error: 'process_identity_unavailable', failed_at: now(),
      ...failureSafety(config) };
    atomicWrite(paths.receipt, receipt);
    releaseLock(lock, config.request_id);
    emitReceipt(config, receipt);
    return;
  }
  const processStart = startProbe.start_time;
  receipt = { ...receipt, status: 'starting', process_pid: child.pid, started_at: now(), heartbeat_at: now(),
    process_start_time: processStart, cli_version: launch.version, ...launch.fields };
  atomicWrite(paths.receipt, receipt);
  atomicWrite(path.join(lock, 'owner.json'), {
    host: config.host, request_id: config.request_id, cwd, process_pid: child.pid,
    process_start_time: processStart, started_at: receipt.started_at, heartbeat_at: receipt.heartbeat_at,
  });
  emit(config, 'state', { status: 'starting', process_pid: child.pid, cli_version: launch.version });

  const stdoutFd = fs.openSync(paths.stdout, 'wx', 0o600);
  const stderrFd = fs.openSync(paths.stderr, 'wx', 0o600);
  const stdoutDigest = crypto.createHash('sha256');
  const stderrDigest = crypto.createHash('sha256');
  const run = { session: null };
  let stdoutBytes = 0, stderrBytes = 0, pending = Buffer.alloc(0);
  let timedOut = false, settled = false, executionFailure = null;
  const heartbeat = setInterval(() => {
    if (executionFailure) return;
    const at = now();
    receipt = { ...receipt, status: 'running', session: run.session, heartbeat_at: at };
    try {
      atomicWrite(paths.receipt, receipt);
      atomicWrite(path.join(lock, 'owner.json'), {
        host: config.host, request_id: config.request_id, cwd, process_pid: child.pid,
        process_start_time: processStart, started_at: receipt.started_at, heartbeat_at: at,
      });
    } catch (error) {
      emit(config, 'error', { error: error.code || error.message });
    }
    emit(config, 'heartbeat', { status: 'running', session: run.session, observed_at: at });
  }, HEARTBEAT_MS);

  const timeout = setTimeout(() => {
    if (settled) return;
    timedOut = true;
    spawnSync('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], {
      windowsHide: true, timeout: 15000, encoding: 'utf8',
    });
  }, config.timeout_seconds * 1000);

  const terminateForFailure = error => {
    if (executionFailure || settled) return;
    executionFailure = error && (error.code || error.message) || 'runner_stream_failure';
    clearInterval(heartbeat);
    receipt = { ...receipt, status: 'failed', error: executionFailure, failed_at: now(),
      ...failureSafety(config) };
    try { atomicWrite(paths.receipt, receipt); } catch {}
    spawnSync('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], {
      windowsHide: true, timeout: 15000, encoding: 'utf8',
    });
  };

  child.stdout.on('data', chunk => {
    try {
      stdoutBytes += chunk.length;
      if (stdoutBytes > MAX_STREAM) fail('stream_size_limit');
      fs.writeSync(stdoutFd, chunk); stdoutDigest.update(chunk); pending = Buffer.concat([pending, chunk]);
      if (pending.length > MAX_LINE * 2) fail('stream_buffer_limit');
      let end;
      while ((end = pending.indexOf(10)) >= 0) {
        const line = pending.subarray(0, end); pending = pending.subarray(end + 1);
        if (line.length > MAX_LINE) fail('stream_line_limit');
        if (!line.length) continue;
        emit(config, launch.frame, { line_b64: line.toString('base64') });
        try { launch.observe(JSON.parse(line.toString('utf8')), run); } catch {}
      }
    } catch (error) {
      terminateForFailure(error);
    }
  });
  child.stderr.on('data', chunk => {
    try {
      stderrDigest.update(chunk);
      if (stderrBytes < MAX_STDERR) {
        const part = chunk.subarray(0, MAX_STDERR - stderrBytes);
        fs.writeSync(stderrFd, part); stderrBytes += part.length;
      }
    } catch (error) { terminateForFailure(error); }
  });

  const finish = code => {
    if (settled) return;
    settled = true; clearInterval(heartbeat); clearTimeout(timeout);
    try { fs.closeSync(stdoutFd); } catch {}
    try { fs.closeSync(stderrFd); } catch {}
    let evidence = null, evidenceError = executionFailure;
    if (!executionFailure && pending.length) evidenceError = 'stream_record_partial';
    if (!timedOut && !evidenceError && launch.ready(code, run)) {
      try { evidence = launch.evidence(run); }
      catch (error) { evidenceError = error.code || error.message; }
    }
    const status = timedOut ? 'timed_out'
      : (!evidenceError && launch.ready(code, run) && evidence ? 'completed_claimed' : 'failed');
    if (!evidenceError && status === 'failed') evidenceError = launch.exitError(code);
    receipt = { ...receipt, status, exit_code: code, timed_out: timedOut, session: run.session,
      stdout_bytes: stdoutBytes, stdout_sha256: stdoutDigest.digest('hex'),
      stderr_bytes: stderrBytes, stderr_sha256: stderrDigest.digest('hex'),
      finished_at: now(), ...(evidence || {}), ...(evidenceError ? { error: evidenceError } : {}),
      ...(status === 'completed_claimed' ? { partial_write_risk: false } : failureSafety(config)) };
    try {
      atomicWrite(paths.receipt, receipt);
      releaseLock(lock, config.request_id);
      emitReceipt(config, receipt);
    } catch (error) {
      emit(config, 'error', { error: error.code || error.message, status: 'unknown' });
      process.exitCode = 5;
      return;
    }
    process.exitCode = status === 'completed_claimed' ? 0 : 4;
  };
  child.on('error', error => {
    if (settled) return;
    settled = true; clearInterval(heartbeat); clearTimeout(timeout);
    try { fs.closeSync(stdoutFd); } catch {}
    try { fs.closeSync(stderrFd); } catch {}
    receipt = { ...receipt, status: 'failed', error: error.code || error.message, failed_at: now(),
      ...failureSafety(config) };
    try { atomicWrite(paths.receipt, receipt); releaseLock(lock, config.request_id); emitReceipt(config, receipt); }
    finally { process.exitCode = 3; }
  });
  child.on('exit', code => finish(code));
  child.stdin.on('error', error => terminateForFailure(Object.assign(error, { code: error.code || 'stdin_write_failed' })));
  if (launch.stdin) child.stdin.end(launch.stdin);
  else child.stdin.end();
}

function main(packet) {
  const outer = JSON.parse(Buffer.from(packet.trim(), 'base64').toString('utf8'));
  const config = validateConfig(outer.config, { promptRequired: outer.config.action === 'dispatch' });
  if (config.action === 'receipt') return queryReceipt(config);
  if (config.action === 'reclaim') return reclaimLock(config);
  if (config.action === 'evidence') return emitKimiEvidence(config);
  const prompt = Buffer.from(outer.prompt_b64 || '', 'base64');
  const profile = outer.kimi_profile_b64 ? Buffer.from(outer.kimi_profile_b64, 'base64').toString('utf8') : null;
  return dispatch(config, prompt, profile);
}

if (require.main === module) {
  let packet = '';
  process.stdin.setEncoding('ascii');
  process.stdin.on('data', chunk => {
    packet += chunk;
    if (packet.length > MAX_PACKET) {
      process.stderr.write('packet_size_limit\n');
      process.exit(2);
    }
  });
  process.stdin.on('end', () => {
    try { main(packet); }
    catch (error) {
      let config = { token: null, request_id: null };
      try { config = JSON.parse(Buffer.from(packet.trim(), 'base64').toString('utf8')).config || config; } catch {}
      emit(config, 'error', { error: error.code || error.message || 'runner_error' });
      process.exitCode = 2;
    }
  });
}

module.exports = {
  atomicWrite, validateConfig, validateWindowsPath, isWithin, inspectRollout, parseProcessProbe, failureSafety,
  queryReceipt, reclaimLock, reconcileRunningReceipt, validateReclaimEvidence,
  kimiScriptFromShim, kimiThinking, validateKimiProfile, gitRoot, kimiSessionDir, kimiWireRows, inspectKimiTurn,
  findKimiEvidence, MAX_KIMI_PROMPT, sameRoots, codexAddDirs,
};
