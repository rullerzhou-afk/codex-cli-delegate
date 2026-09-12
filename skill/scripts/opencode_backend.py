"""OpenCode CLI adapter. Hooks are hints; native session records prove completion."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from claude_events import Monitor
from kimi_backend import capture_identity, error

MODEL = 'deepseek/deepseek-flash'
EFFORT = 'high'
TOOLS = ('read', 'glob', 'grep', 'edit', 'bash')


def prepare(explicit, tools, rules):
    if sys.platform != 'darwin':
        error('opencode_platform', 'Process identity currently verified on macOS only')
    if rules:
        error('opencode_permissions', 'Claude allow rules are not OpenCode rules; use --opencode-tool')
    binary = explicit or shutil.which('opencode')
    if not binary or not os.access(binary, os.X_OK):
        error('opencode_missing', 'OpenCode CLI not found')
    # npm bin can be a JS launcher; select the installed native binary so PID
    # birth/executable identity matches the process actually being supervised.
    binary = Path(binary).resolve()
    if binary.suffix in ('.js', '.exe') or binary.name == 'opencode':
        native = binary.parent.parent / 'node_modules/opencode-darwin-arm64/bin/opencode'
        if native.is_file(): binary = native
    version = subprocess.check_output([str(binary), '--version'], text=True).strip()
    if version != '1.18.30':
        error('opencode_version', 'Adapter verified against 1.18.30; inspect hooks/schema before accepting another version')
    data = Path(os.environ.get('XDG_DATA_HOME', str(Path.home()/'.local/share')))
    return dict(opencode_bin=str(binary), opencode_db=str(data/'opencode/opencode.db'),
                opencode_tools=sorted(set(tools or ('read','glob','grep'))), opencode_version=version)


def connection(job):
    return sqlite3.connect(Path(job['opencode_db']).as_uri()+'?mode=ro', uri=True)


def baseline(job):
    if not job.get('session_id'): error('opencode_session_missing', 'No verified session to resume')
    with connection(job) as c:
        ids = [r[0] for r in c.execute('select id from message where session_id=?', (job['session_id'],))]
    return ids, 'ok'


def setup(job, record, resume, prompt_file, directory):
    from claude_task import atomic_write_bytes
    directory = Path(directory)
    prepare(job['opencode_bin'], job['opencode_tools'], [])
    marker = '[codex-delegate:'+record['run_token']+']'
    prompt = directory/'opencode-prompt.txt'
    atomic_write_bytes(str(prompt), (marker+'\n'+Path(prompt_file).read_text()).encode())
    argv = [job['opencode_bin'], 'run', '--agent', 'codex-delegated-opencode', '--dir', job['cwd'], '--title', 'Codex delegated task', '--format', 'json', '--model', job['model'], '--variant', job['effort']]
    if resume: argv += ['--session', job['session_id']]
    # Per-process config merges with existing providers/plugins. Never write
    # permanent user configuration or issue permission replies from our hook.
    env = dict(os.environ, PWD=job['cwd'])
    overlay = json.loads(env.get('OPENCODE_CONFIG_CONTENT', '{}'))
    plugins = list(overlay.get('plugin', []))
    plugins.append((Path(__file__).parent/'opencode_hook.mjs').resolve().as_uri())
    overlay['plugin'] = plugins
    overlay['share'] = 'disabled'
    overlay['small_model'] = job['model']
    permissions = {'*':'deny', **{t:'allow' for t in job['opencode_tools']}, 'external_directory':'deny'}
    overlay['permission'] = permissions
    overlay.setdefault('agent', {})['codex-delegated-opencode'] = dict(description='Scoped Codex delegation', mode='primary', permission=permissions)
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(overlay)
    env['CODEX_OPENCODE_ROUTE'] = json.dumps(dict(cwd=job['cwd'], session=job.get('session_id'),
        token=record['run_token'], marker=marker, tools=job['opencode_tools'], inbox=str(directory/'hooks.ndjson')))
    return argv, env, str(prompt)


class OpenCodeMonitor(Monitor):
    def __init__(self, directory, job, record, write_json):
        self.token = record['run_token']
        super().__init__(directory, job.get('session_id'), record['started_epoch'], write_json)
    def hook(self, row):
        if row.get('token') != self.token: return
        if row.get('hook') == 'bound' and self.session_id is None: self.session_id = row.get('sessionID')
        if not self.session_id or row.get('sessionID') != self.session_id: return
        name = row.get('hook')
        self.hooks_seen[name] = self.hooks_seen.get(name, 0)+1
        if name == 'ToolStart':
            self.tools[row.get('callID')] = row.get('tool','')
            self.progress()
        elif name == 'ToolEnd':
            self.tools.pop(row.get('callID'), None)
            self.progress()
        if name in ('StopFailure','PostToolUseFailure','PermissionRequest'):
            self.error(name, row.get('tool',''), 'OpenCode failure or permission request; inspect private evidence', fatal=name=='StopFailure')
    def stream(self, row):
        if not self.session_id or row.get('sessionID') != self.session_id: return
        part = row.get('part') or {}
        if row.get('type') == 'error': self.error('opencode_error', message='OpenCode session error', fatal=True)
        elif row.get('type') == 'step_finish':
            tokens = part.get('tokens') or {}
            self.messages[part.get('id','unknown')] = dict(input=self.number(tokens.get('input')), output=self.number(tokens.get('output')))
            self.progress()
        elif row.get('type') in ('text','tool_use','step_start'):
            if row.get('type') == 'tool_use':
                self.tools.pop(part.get('callID'), None)
                if part.get('state',{}).get('status') == 'error':
                    self.error('tool_result', part.get('tool',''), 'OpenCode tool failed; inspect private evidence')
            self.progress()
    def snapshot(self):
        result = super().snapshot()
        result.update(output_source='opencode_step_finish' if self.messages else 'unknown', session_id=self.session_id, notification_mode='opencode_plugin_and_native_verification', quota=None)
        return result


def verify(job, record, stdout_path, exit_code):
    from claude_task import clip, write_json
    reasons, report, sid, assistants, denials = [], '', None, [], []
    try:
        stream = [json.loads(s) for s in Path(stdout_path).read_text().splitlines()]
        hooks = [json.loads(s) for s in (Path(stdout_path).parent/'hooks.ndjson').read_text().splitlines()]
        bound = [r for r in hooks if r.get('hook')=='bound' and r.get('token')==record['run_token'] and r.get('cwd')==job['cwd']]
        if len(bound)!=1: raise ValueError('hook binding missing or ambiguous')
        sid = bound[0]['sessionID']
        if job.get('session_id') not in (None,sid): raise ValueError('wrong resumed session')
        if any(r.get('sessionID') != sid for r in stream): raise ValueError('foreign stream session')
        if any(r.get('type')=='error' for r in stream): reasons.append('session_error')
        with connection(job) as c:
            session = c.execute('select directory,parent_id from session where id=?',(sid,)).fetchone()
            if not session or os.path.realpath(session[0])!=job['cwd'] or session[1]: raise ValueError('wrong native root/cwd')
            rows = [(r[0], json.loads(r[1])) for r in c.execute('select id,data from message where session_id=? order by time_created,id',(sid,))]
            parts = [(r[0],json.loads(r[1])) for r in c.execute('select message_id,data from part where session_id=? order by time_created,id',(sid,))]
        prior = record.get('prior_assistant_uuids') or []
        if record['kind']=='revision' and (record.get('baseline_status')!='ok' or not set(prior).issubset({i for i,d in rows})): raise ValueError('baseline missing')
        fresh = [(i,d) for i,d in rows if i not in prior]
        if any(d.get('agent') != 'codex-delegated-opencode' for i,d in fresh if d.get('role')=='assistant'):
            reasons.append('agent_unverified')
        users = [(i,d) for i,d in fresh if d.get('role')=='user']
        marker = '[codex-delegate:'+record['run_token']+']'
        if len(users)!=1 or not any(i==users[0][0] and d.get('type')=='text' and marker in d.get('text','') for i,d in parts): raise ValueError('current prompt missing or foreign turn')
        uid = users[0][0]
        assistants = [(i,d) for i,d in fresh if d.get('role')=='assistant']
        if not assistants or any(d.get('parentID')!=uid for i,d in assistants): raise ValueError('assistant parent mismatch')
        if any(str(d.get('providerID'))+'/'+str(d.get('modelID'))!=job['model'] for i,d in assistants): reasons.append('model_unverified')
        if any(d.get('variant')!=job['effort'] for i,d in assistants): reasons.append('effort_unverified')
        if any(d.get('error') for i,d in assistants): reasons.append('native_error')
        final_id, final = assistants[-1]
        if final.get('finish')!='stop' or not final.get('time',{}).get('completed'): reasons.append('native_completion_missing')
        report = ''.join(d.get('text','') for i,d in parts if i==final_id and d.get('type')=='text')
        delivered = ''.join((r.get('part') or {}).get('text','') for r in stream if r.get('type')=='text' and (r.get('part') or {}).get('messageID')==final_id)
        if not report or report != delivered: reasons.append('report_mismatch')
        denials = [r for r in stream if r.get('type')=='tool_use' and (r.get('part') or {}).get('state',{}).get('status')=='error']
        # Retain only current-turn formal text and identity metadata, not a
        # copy of the user's database or unrelated conversations.
        write_json(str(Path(stdout_path).parent/'opencode-native.json'),dict(session_id=sid, user_id=uid,
            assistants=[dict(id=i, **{k:d.get(k) for k in ('parentID','providerID','modelID','variant','finish','time')}) for i,d in assistants], report=report))
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error) as exc:
        reasons.append('native_evidence_invalid:'+str(exc)[:160])
    if exit_code!=0: reasons.insert(0,'exit_nonzero')
    return dict(ok=not reasons, needs_attention=exit_code==0 and bool(reasons), reasons=reasons, report=clip(report),
        tool_failures=len(denials), exit_code=exit_code, native_session_id=sid, session_ok=sid is not None and not any(r.startswith('native_evidence_invalid') for r in reasons), cli_version=job.get('opencode_version'),
        model_verified=bool(assistants) and not reasons, effort_verified=bool(assistants) and not reasons,
        assistant_models=sorted({str(d.get('providerID'))+'/'+str(d.get('modelID')) for i,d in assistants}),
        efforts=sorted({str(d.get('variant')) for i,d in assistants}),
        transcript_lookup=str(Path(stdout_path).parent/'opencode-native.json'), new_assistant_entries=len(assistants))
