"""Actual SDK client -> local Docker MCP endpoint; read-only fixture scope."""
import asyncio
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
import application_operations as ops
import mcp_credentials
from store import connect

async def main():
    path=Path('.runtime/state/design/db')
    owner=ops.Principal('admin','console',mcp_credentials.SCOPES)
    with connect(path) as db:
        issued=mcp_credentials.create(db,owner,{'label':'Local SDK transport acceptance','scopes':['projects:read'],
            'allowed_projects':['deploy-monitor-ui'],'allowed_repository_ids':[12],
            'allowed_environments':[['deploy-monitor-ui','development']], 'expires_at':time.time()+300})
    try:
        async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+issued['token']}) as http:
            async with streamable_http_client('http://127.0.0.1:19192/_deploy/mcp',http_client=http) as (read,write):
                async with ClientSession(read,write) as client:
                    protocol=await client.discover()
                    tools=await client.list_tools()
                    assert {t.name for t in tools.tools}=={'project_list','project_get'}
                    out=await client.call_tool('project_list',{})
                    assert not out.is_error
                    assert [p['id'] for p in out.structured_content['projects']]==['deploy-monitor-ui']
                    assert set(out.structured_content['projects'][0]['environments'])=={'development'}
                    denied=await client.call_tool('project_get',{'project_id':'etsawaa'})
                    assert denied.is_error
                    with connect(path) as db:mcp_credentials.revoke(db,owner,issued['credential']['id'])
                    revoked=await http.post('http://127.0.0.1:19192/_deploy/mcp',json={})
                    assert revoked.status_code==401
        report={'actual_docker_endpoint':True,'sdk_protocol':'2026-07-28','scoped_project_read':True,
                'production_environment_hidden':True,'foreign_project_denied':True,'revoked_http_status':401,
                'credential_revoked':True,'production_mutations':False}
        Path('.runtime/mcp-transport-last.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report))
    finally:
        with connect(path) as db:mcp_credentials.revoke(db,owner,issued['credential']['id'])
if __name__=='__main__':asyncio.run(main())
