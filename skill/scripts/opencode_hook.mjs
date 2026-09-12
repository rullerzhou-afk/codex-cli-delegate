// One export: OpenCode loads each exported function as a plugin.
import { appendFileSync } from 'node:fs';
export default async function ({ directory }) {
  let route;
  try { route = JSON.parse(process.env.CODEX_OPENCODE_ROUTE || 'null'); } catch { return {}; }
  if (!route || route.cwd !== directory || !route.token || !route.inbox) return {};
  let sid = route.session || null;
  let active = false;
  const save = (data) => appendFileSync(route.inbox, JSON.stringify({token:route.token, sessionID:sid, ...data})+'\n', {mode:0o600});
  const allowed = new Set(route.tools || []);
  if (allowed.has('edit')) for (const t of ['write', 'apply_patch', 'multiedit']) allowed.add(t);
  return {
    'tool.execute.before': async (input) => {
      if (input.sessionID !== sid || !active) return;
      if (!allowed.has(input.tool)) {
        save({hook:'PostToolUseFailure', tool:input.tool});
        throw new Error('Tool outside the delegated tool selection');
      }
      save({hook:'ToolStart', tool:input.tool, callID:input.callID});
    },
    'tool.execute.after': async (input) => {
      if (active && input.sessionID === sid) save({hook:'ToolEnd', tool:input.tool, callID:input.callID});
    },
    'chat.message': async (input, output) => {
      if (!(output.parts || []).some(p => p.type === 'text' && p.text?.includes(route.marker))) return;
      if (sid && sid !== input.sessionID) throw new Error('Delegate session mismatch');
      sid = input.sessionID; active = true;
      save({hook:'bound', cwd:directory});
    },
    event: async ({event}) => {
      if (!active) return;
      const p = event.properties || {};
      const eventSid = p.sessionID || p.info?.sessionID || p.part?.sessionID;
      if (eventSid !== sid) return; // Never infer the root from a child or a missing ID.
      if (event.type === 'session.idle' || (event.type === 'session.status' && p.status?.type === 'idle')) save({hook:'Stop'});
      if (event.type === 'session.error') save({hook:'StopFailure'});
      if (event.type === 'permission.asked') save({hook:'PermissionRequest', requestID:p.id, tool:p.permission});
      if (event.type === 'permission.replied') save({hook:'PermissionReply', requestID:p.requestID, reply:p.reply});
      if (event.type === 'message.part.updated' && p.part?.type === 'tool' && p.part.state?.status === 'error') save({hook:'PostToolUseFailure', tool:p.part.tool});
    }
  };
}
