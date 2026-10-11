import test from 'node:test';
import assert from 'node:assert/strict';
import {MCP_SCOPES,freshMcpDraft,mcpCredentialValues} from './mcpAccess.js';

test('domain reads are opt-in and require bounded environment selection',()=>{
  assert.ok(MCP_SCOPES.includes('domains:read'));
  assert.ok(!MCP_SCOPES.includes('domains:create'));
  assert.ok(!freshMcpDraft().scopes.includes('domains:read'));
  const draft={...freshMcpDraft(),label:'Domain reader',scopes:['domains:read'],projects:['app']};
  const projects={app:{repository_id:12}};
  assert.throws(()=>mcpCredentialValues(draft,projects),/exact environments/);
  draft.environments=[JSON.stringify(['app','development'])];
  assert.deepEqual(mcpCredentialValues(draft,projects).allowed_environments,[['app','development']]);
});
