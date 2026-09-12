import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import plugin from '../scripts/opencode_hook.mjs';
test('route binding, child filtering, permission observation, tool selection',async()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'oc-hook-'));
 const inbox=path.join(dir,'hooks');
 try {
 process.env.CODEX_OPENCODE_ROUTE=JSON.stringify({cwd:dir,token:'t',marker:'marker',inbox,tools:['read']});
 const hooks=await plugin({directory:dir});
 await hooks.event({event:{type:'session.idle',properties:{sessionID:'root'}}});
 assert.equal(fs.existsSync(inbox),false);
 await hooks['chat.message']({sessionID:'root'},{parts:[{type:'text',text:'marker task'}]});
 await hooks.event({event:{type:'session.idle',properties:{sessionID:'child'}}});
 await hooks.event({event:{type:'session.idle',properties:{}}});
 await hooks.event({event:{type:'permission.asked',properties:{sessionID:'root',id:'request',permission:'bash'}}});
 await hooks['tool.execute.before']({sessionID:'root',tool:'read'});
 await assert.rejects(hooks['tool.execute.before']({sessionID:'root',tool:'bash'}));
 await hooks.event({event:{type:'session.idle',properties:{sessionID:'root'}}});
 const rows=fs.readFileSync(inbox,'utf8').trim().split('\n').map(JSON.parse);
 assert.deepEqual(rows.map(r=>r.hook),['bound','PermissionRequest','ToolStart','PostToolUseFailure','Stop']);
 assert.equal(rows.every(r=>r.sessionID==='root'),true);
 await assert.rejects(hooks['chat.message']({sessionID:'other'},{parts:[{type:'text',text:'marker task'}]}));
 assert.deepEqual(await plugin({directory:'/other'}),{});
 }finally{delete process.env.CODEX_OPENCODE_ROUTE;fs.rmSync(dir,{recursive:true,force:true});}
});
