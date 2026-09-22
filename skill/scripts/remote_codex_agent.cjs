'use strict';
// Read-only Windows observer. Only normalized state and formal final text leave the host.
const fs = require('fs');
const crypto = require('crypto');
const MAX_LINE = 4 * 1024 * 1024;
const MAX_FILE = 256 * 1024 * 1024;
const sha = b => crypto.createHash('sha256').update(b).digest('hex');
const norm = s => String(s).replace(/\\/g, '/').replace(/\/$/, '').toLowerCase();
function textOf(content) {
  if (typeof content === 'string') return content;
  return Array.isArray(content) ? content.map(x => typeof x.text === 'string' ? x.text : '').join('') : '';
}
class Parser {
  constructor(config) {
    this.c = config; this.meta = false; this.active = null; this.started = false;
    this.model = null; this.effort = null; this.sandbox = null; this.approval = null;
    this.startedCount = 0; this.final = ''; this.status = 'unknown';
    this.activity = null; this.terminal = false; this.version = null;
  }
  consume(record) {
    const p = record.payload || {};
    if (!this.meta) {
      if (record.type !== 'session_meta' || p.id !== this.c.session || norm(p.cwd) !== norm(this.c.cwd))
        throw Error('session_or_cwd_mismatch');
      this.meta = true; this.version = p.cli_version || null; return;
    }
    if (this.terminal) return;
    if (record.type === 'session_meta') throw Error('duplicate_session_meta');
    if (p.thread_id && p.thread_id !== this.c.session) return;
    if (record.type === 'event_msg' && p.type === 'task_started') {
      if (!p.turn_id) throw Error('missing_turn_identity');
      this.startedCount += 1;
      if (this.c.single_turn && this.startedCount !== 1) throw Error('ambiguous_turn_identity');
      this.active = p.turn_id;
      if (this.active === this.c.turn) { this.started = true; this.status = 'running'; }
      else if (this.started) { this.status = 'superseded'; this.terminal = true; }
    }
    if (!this.started || this.active !== this.c.turn) return;
    if ((p.turn_id && p.turn_id !== this.c.turn) || (p.thread_id && p.thread_id !== this.c.session)) return;
    if (record.timestamp && Number.isFinite(Date.parse(record.timestamp))) this.activity = record.timestamp;
    if (record.type === 'turn_context' && p.turn_id === this.c.turn) {
      if (norm(p.cwd) !== norm(this.c.cwd)) throw Error('turn_cwd_mismatch');
      this.model = p.model || null; this.effort = p.effort || null;
      this.sandbox = p.sandbox_policy && p.sandbox_policy.type || null;
      this.approval = p.approval_policy || null;
    }
    if (record.type === 'event_msg') {
      const i = p.item || {};
      if (p.type === 'item_completed' && i.type === 'AgentMessage' && i.phase === 'final_answer')
        this.final = textOf(i.content);
      if (p.type === 'task_complete' && p.turn_id === this.c.turn) {
        const last = typeof p.last_agent_message === 'string' ? p.last_agent_message : '';
        if (last && this.final && last.trim() !== this.final.trim()) throw Error('final_text_mismatch');
        this.final = last || this.final;
        this.status = this.final.trim() && this.model && this.effort && this.sandbox && this.approval
          ? 'awaiting_review' : 'incomplete_evidence';
        this.terminal = true;
      } else if (['turn_aborted', 'task_failed'].includes(p.type) && p.turn_id === this.c.turn) {
        this.status = p.type === 'turn_aborted' ? 'interrupted' : 'failed'; this.terminal = true;
      }
    }
  }
  snapshot() {
    return {status: this.status, started: this.started, activity: this.activity, model: this.model,
      effort: this.effort, sandbox: this.sandbox, approval_policy: this.approval,
      cli_version: this.version, terminal: this.terminal,
      ...(this.terminal && this.final ? {final_text: this.final, final_sha256: sha(this.final)} : {})};
  }
}
async function observe(c) {
  const parser = new Parser(c), digest = crypto.createHash('sha256');
  const fd = fs.openSync(c.log, 'r'), initial = fs.fstatSync(fd);
  let offset = 0, completeBytes = 0, pending = Buffer.alloc(0), lastSent = '', lastBeat = 0;
  let replayVerified = !c.checkpoint;
  const emit = (kind, body) => process.stdout.write(JSON.stringify({protocol: 1, token: c.token,
    session: c.session, turn: c.turn, kind, ...body}) + '\n');
  try {
    for (;;) {
      const stat = fs.fstatSync(fd), named = fs.statSync(c.log);
      if (stat.size < offset || named.ino !== initial.ino || named.dev !== initial.dev)
        throw Error('source_replaced_or_truncated');
      if (stat.size > MAX_FILE) throw Error('source_size_limit');
      if (c.checkpoint && stat.size < c.checkpoint.bytes) throw Error('source_prefix_truncated');
      let budget = 8 * 1024 * 1024;
      while (offset < stat.size && budget > 0) {
        const b = Buffer.alloc(Math.min(65536, stat.size - offset, budget));
        const n = fs.readSync(fd, b, 0, b.length, offset);
        if (!n) break;
        offset += n; budget -= n;
        pending = Buffer.concat([pending, b.subarray(0, n)]);
        let end;
        while ((end = pending.indexOf(10)) >= 0) {
          if (end > MAX_LINE) throw Error('record_size_limit');
          const line = pending.subarray(0, end + 1); pending = pending.subarray(end + 1);
          digest.update(line);
          completeBytes += line.length;
          if (!replayVerified && completeBytes >= c.checkpoint.bytes) {
            if (completeBytes !== c.checkpoint.bytes || digest.copy().digest('hex') !== c.checkpoint.sha256)
              throw Error('source_prefix_changed');
            replayVerified = true;
          }
          if (line.toString('utf8').trim()) parser.consume(JSON.parse(line.toString('utf8')));
        }
        if (pending.length > MAX_LINE) throw Error('record_size_limit');
      }
      if (offset < stat.size) continue;
      if (!replayVerified) throw Error('source_checkpoint_missing');
      const state = parser.snapshot();
      const value = JSON.stringify(state), now = Date.now();
      if (value !== lastSent || now - lastBeat >= 5000) {
        emit('state', {...state, source: {path: c.log, bytes: offset - pending.length,
          sha256: digest.copy().digest('hex')}, observed_at: new Date(now).toISOString()});
        lastSent = value; lastBeat = now;
      }
      if (parser.terminal || c.once) break;
      await new Promise(resolve => setTimeout(resolve, 300));
    }
  } finally { fs.closeSync(fd); }
}
if (require.main === module) {
  const c = JSON.parse(Buffer.from(process.argv[2], 'base64').toString('utf8'));
  observe(c).catch(e => {
    process.stdout.write(JSON.stringify({protocol: 1, token: c.token, session: c.session,
      turn: c.turn, kind: 'error', error: e.code || e.message}) + '\n'); process.exitCode = 1;
  });
}
module.exports = {Parser, observe, sha};
