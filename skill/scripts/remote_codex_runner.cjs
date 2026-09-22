'use strict';

// Windows-only Codex carrier. The SSH connection is the process lifetime:
// closing it terminates this runner and its Codex child. Variable task data is
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
const sleepSync = ms => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const now = () => new Date().toISOString();

function failureSafety(config) {
  return config.sandbox === 'workspace-write'
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
  if (config.agent !== 'codex') fail('unsupported_remote_agent');
  if (!['dispatch', 'receipt', 'reclaim'].includes(config.action)) fail('bad_action');
  if (typeof config.site !== 'string' || !config.site || config.site.length > 121) fail('bad_site');
  if (typeof config.host !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$/.test(config.host)) fail('bad_host');
  if (!HEX64_RE.test(config.request_digest || '')) fail('bad_request_digest');
  if (promptRequired && !HEX64_RE.test(config.prompt_sha256 || '')) fail('bad_prompt_hash');
  if (!/^[A-Za-z0-9._-]{1,100}$/.test(config.model || '')) fail('bad_model');
  if (!['low', 'medium', 'high', 'xhigh', 'max', 'ultra'].includes(config.effort)) fail('bad_effort');
  if (!['read-only', 'workspace-write'].includes(config.sandbox)) fail('bad_sandbox');
  if (!['elevated', 'unelevated'].includes(config.windows_sandbox)) fail('bad_windows_sandbox');
  if (!Number.isInteger(config.timeout_seconds) || config.timeout_seconds < 30 || config.timeout_seconds > 86400) fail('bad_timeout');
  if (!Number.isInteger(config.keepalive_window_seconds) || config.keepalive_window_seconds < 60
      || config.keepalive_window_seconds > 3600) fail('bad_keepalive_window');
  if (!Array.isArray(config.allowed_roots) || config.allowed_roots.length === 0) fail('bad_cwd_roots');
  if (typeof config.allow_non_git !== 'boolean') fail('bad_allow_non_git');
  if (config.action === 'reclaim' && !HEX64_RE.test(config.inspection_sha256 || '')) fail('bad_inspection_hash');
  return config;
}

function resolveCodex() {
  const where = name => {
    const result = spawnSync('where.exe', [name], { encoding: 'utf8', windowsHide: true, timeout: 5000 });
    if (result.status !== 0) return [];
    return String(result.stdout || '').split(/\r?\n/).map(value => value.trim()).filter(Boolean);
  };
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

function codexVersion(invocation) {
  const result = spawnSync(invocation.command, [...invocation.prefix, '--version'], {
    encoding: 'utf8', windowsHide: true, timeout: 10000,
  });
  const value = String(result.stdout || result.stderr || '').trim().split(/\r?\n/)[0];
  if (result.status !== 0 || !value) fail('codex_probe_failed');
  return value;
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
      const value = {
        model: payload.model || null,
        effort: payload.effort || null,
        sandbox: payload.sandbox_policy && payload.sandbox_policy.type || null,
        approval_policy: payload.approval_policy || null,
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
    sandbox: context.sandbox || null, approval_policy: context.approval_policy || null, complete,
    final_text_sha256: finalText ? sha(Buffer.from(finalText)) : null };
}

function findEvidence(config, session) {
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

function dispatch(config, prompt) {
  const paths = jobPaths(config);
  fs.mkdirSync(path.win32.join(paths.root, 'jobs'), { recursive: true, mode: 0o700 });
  fs.mkdirSync(paths.locks, { recursive: true, mode: 0o700 });

  const cwd = canonicalDirectory(config.cwd, 'cwd');
  const codexHome = canonicalDirectory(config.codex_home, 'codex_home');
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
    request_digest: config.request_digest, site: config.site, host: config.host, agent: 'codex',
    cwd, codex_home: codexHome, model: config.model, effort: config.effort,
    sandbox: config.sandbox, windows_sandbox: config.windows_sandbox,
    timeout_seconds: config.timeout_seconds,
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

  let invocation;
  let version;
  try {
    invocation = resolveCodex();
    version = codexVersion(invocation);
  } catch (error) {
    receipt = { ...receipt, status: 'failed', error: error.code || error.message, failed_at: now() };
    atomicWrite(paths.receipt, receipt); releaseLock(lock, config.request_id); emitReceipt(config, receipt); return;
  }
  const env = { ...process.env, CODEX_HOME: codexHome };
  delete env.CODEX_SQLITE_HOME;
  const args = [...invocation.prefix, 'exec', '--ignore-user-config', '--ignore-rules',
    '-m', config.model, '-c', `model_reasoning_effort="${config.effort}"`,
    '-c', `sandbox_mode="${config.sandbox}"`, '-c', 'approval_policy="never"',
    '-c', `windows.sandbox="${config.windows_sandbox}"`,
    '--sandbox', config.sandbox, '--json', '-C', cwd,
    ...(config.allow_non_git ? ['--skip-git-repo-check'] : []), '-'];
  const child = spawn(invocation.command, args, { cwd, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
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
    process_start_time: processStart, cli_version: version, codex_path: invocation.codex_path,
    codex_invocation: invocation.source };
  atomicWrite(paths.receipt, receipt);
  atomicWrite(path.join(lock, 'owner.json'), {
    host: config.host, request_id: config.request_id, cwd, process_pid: child.pid,
    process_start_time: processStart, started_at: receipt.started_at, heartbeat_at: receipt.heartbeat_at,
  });
  emit(config, 'state', { status: 'starting', process_pid: child.pid, cli_version: version });

  const stdoutFd = fs.openSync(paths.stdout, 'wx', 0o600);
  const stderrFd = fs.openSync(paths.stderr, 'wx', 0o600);
  const stdoutDigest = crypto.createHash('sha256');
  const stderrDigest = crypto.createHash('sha256');
  let stdoutBytes = 0, stderrBytes = 0, pending = Buffer.alloc(0), session = null;
  let turnCompleted = false, timedOut = false, settled = false, executionFailure = null;
  const heartbeat = setInterval(() => {
    if (executionFailure) return;
    const at = now();
    receipt = { ...receipt, status: 'running', session, heartbeat_at: at };
    try {
      atomicWrite(paths.receipt, receipt);
      atomicWrite(path.join(lock, 'owner.json'), {
        host: config.host, request_id: config.request_id, cwd, process_pid: child.pid,
        process_start_time: processStart, started_at: receipt.started_at, heartbeat_at: at,
      });
    } catch (error) {
      emit(config, 'error', { error: error.code || error.message });
    }
    emit(config, 'heartbeat', { status: 'running', session, observed_at: at });
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
        emit(config, 'codex', { line_b64: line.toString('base64') });
        try {
          const event = JSON.parse(line.toString('utf8'));
          if (event.type === 'thread.started' && UUID_RE.test(event.thread_id || '')) session = event.thread_id;
          if (event.type === 'turn.completed') turnCompleted = true;
        } catch {}
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
    if (!timedOut && !evidenceError && code === 0 && turnCompleted && session) {
      try { evidence = findEvidence(config, session); }
      catch (error) { evidenceError = error.code || error.message; }
    }
    const status = timedOut ? 'timed_out'
      : (!evidenceError && code === 0 && turnCompleted && session && evidence ? 'completed_claimed' : 'failed');
    if (!evidenceError && status === 'failed') evidenceError = `codex_exit_${code === null ? 'unknown' : code}`;
    receipt = { ...receipt, status, exit_code: code, timed_out: timedOut, session,
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
  child.stdin.end(prompt);
}

function main(packet) {
  const outer = JSON.parse(Buffer.from(packet.trim(), 'base64').toString('utf8'));
  const config = validateConfig(outer.config, { promptRequired: outer.config.action === 'dispatch' });
  if (config.action === 'receipt') return queryReceipt(config);
  if (config.action === 'reclaim') return reclaimLock(config);
  const prompt = Buffer.from(outer.prompt_b64 || '', 'base64');
  return dispatch(config, prompt);
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
};
