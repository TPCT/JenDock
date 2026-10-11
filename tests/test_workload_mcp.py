"""Official MCP SDK through actual Unix peer/auth/journal; Docker is simulated."""
import json
import os
from pathlib import Path
import socketserver
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
import application_operations as ops
import control
import mcp_credentials
import mcp_server
import registry
from store import connect
import test_mcp_server as sdk_fixture
from test_workload_control import TARGET, RETRY, Transport


class WorkloadMCPTests(unittest.IsolatedAsyncioTestCase):
    _callTestMethod=sdk_fixture.MCPTransportTests._callTestMethod
    post=sdk_fixture.MCPTransportTests.post
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.path=root/'db';self.db=connect(self.path)
        registry.create_project(self.db,'demo',{'repository':'TPCT/demo','repository_id':42,'label':'Demo'})
        self.owner=ops.Principal('owner','console',mcp_credentials.SCOPES)
        self.owner_file=root/'owner';self.owner_file.write_text('{"username":"owner"}')
        self.transport=Transport();self.transport.db=self.db
        self.socket=str(root/'control.sock')
        self.patchers=[patch.dict(os.environ,{'CREDENTIALS_FILE':str(self.owner_file),'WORKLOAD_MCP_UID':str(os.getuid()),'WORKLOAD_WORKER_UID':str(os.getuid())}),
                      patch.object(control,'DATABASE',str(self.path)),
                      patch('workload_transport.WorkloadTransport',return_value=self.transport),
                      patch('workload_control._target',return_value={'role':'queue','configuration':{'connection':'pgsql','queues':['default']}})]
        for p in self.patchers:p.start()
        self.unix=socketserver.ThreadingUnixStreamServer(self.socket,control.Handler);self.unix.daemon_threads=True
        self.thread=threading.Thread(target=self.unix.serve_forever,daemon=True);self.thread.start()
        self.issued=self.issue(['workloads:read','workloads:retry'])
        self.app=mcp_server.create_app(self.path,root/'key',allowed_hosts=['testserver'],workload_socket=self.socket)
        self.lifespan=self.app.app.router.lifespan_context(self.app.app)
        self.http=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app),base_url='http://testserver',headers={'Authorization':'Bearer '+self.issued['token']})
        await self.http.__aenter__()
    def issue(self,scopes):
        return mcp_credentials.create(self.db,self.owner,{'label':'Workload test','scopes':scopes,'allowed_projects':['demo'],'allowed_repository_ids':[42],'allowed_environments':[['demo','development']],'expires_at':time.time()+3600})
    async def asyncTearDown(self):
        await self.http.__aexit__(None,None,None)
        self.unix.shutdown();self.unix.server_close();self.thread.join()
        for p in reversed(self.patchers):p.stop()
        self.db.close();self.tmp.cleanup()
    def args(self,value=TARGET):return {('project_id' if k=='project' else k):v for k,v in value.items()}
    async def test_sdk_read_retry_and_lost_reply_receipt_only(self):
        async with streamable_http_client('http://testserver/_deploy/mcp',http_client=self.http) as (read,write):
            async with ClientSession(read,write) as client:
                await client.discover()
                tools={tool.name:tool for tool in (await client.list_tools()).tools}
                self.assertEqual(set(tools),mcp_server.WORKLOAD_TOOLS)
                self.assertFalse(tools['queue_retry'].annotations.read_only_hint)
                self.assertTrue(tools['queue_retry'].annotations.destructive_hint)
                self.assertTrue(tools['workload_operation'].annotations.read_only_hint)
                catalog=await client.call_tool('workload_targets',{'project_id':'demo','environment':'development'})
                self.assertFalse(catalog.is_error);self.assertEqual(catalog.structured_content,{'items':[], 'next_cursor':None})
                self.assertEqual(self.transport.calls,[])
                result=await client.call_tool('workload_role_details',self.args())
                self.assertFalse(result.is_error);self.assertEqual(result.structured_content['workload']['project'],'demo')
                self.transport.lost=True
                retry=await client.call_tool('queue_retry',self.args(RETRY))
                self.assertFalse(retry.is_error);self.assertEqual(retry.structured_content['operation']['status'],'recovery-required')
                recovered=await client.call_tool('workload_operation',{k:self.args(RETRY)[k] for k in ('project_id','environment','service_id','request_id')})
                self.assertFalse(recovered.is_error);self.assertEqual(recovered.structured_content['operation']['status'],'requeued')
                self.assertEqual(self.transport.calls,['queue.observe','queue.retry','queue.receipt'])
    async def test_read_grant_does_not_offer_retry_and_rejects_cross_scope(self):
        issued=self.issue(['workloads:read']);self.http.headers['Authorization']='Bearer '+issued['token']
        tools=(await self.post()).json()['result']['tools'];self.assertNotIn('queue_retry',{t['name'] for t in tools})
        for name,args in [('queue_retry',self.args(RETRY)),('workload_role_details',dict(self.args(),environment='production')),('workload_role_details',dict(self.args(),token='secret'))]:
            response=await self.post('tools/call',{'name':name,'arguments':args})
            self.assertTrue(response.json()['result']['isError'])
        self.assertEqual(self.transport.calls,[])
    async def test_worker_peer_rejection_and_revocation_suppress_output(self):
        with patch.dict(os.environ,{'WORKLOAD_MCP_UID':str(os.getuid()+1)}):
            response=await self.post('tools/call',{'name':'workload_role_details','arguments':self.args()})
            self.assertTrue(response.json()['result']['isError'])
        self.assertEqual(self.transport.calls,[])
        mcp_credentials.revoke(self.db,self.owner,self.issued['credential']['id'])
        self.assertEqual((await self.post()).status_code,401)
    async def test_revocation_during_read_suppresses_observation(self):
        original=self.transport.call
        def revoke(*args,**kwargs):
            value=original(*args,**kwargs)
            db=connect(self.path)
            try:mcp_credentials.revoke(db,self.owner,self.issued['credential']['id'])
            finally:db.close()
            return value
        self.transport.call=revoke
        response=await self.post('tools/call',{'name':'workload_role_details','arguments':self.args()})
        value=response.json()['result']
        self.assertTrue(value['isError']);self.assertNotIn('queues',json.dumps(value))

    async def test_owner_rotation_after_retry_hides_result_and_recovers_original(self):
        original=self.transport.call
        def rotate(*args,**kwargs):
            value=original(*args,**kwargs)
            self.owner_file.write_text('{"username":"replacement"}')
            return value
        self.transport.call=rotate
        response=await self.post('tools/call',{'name':'queue_retry','arguments':self.args(RETRY)})
        value=response.json()['result']
        self.assertTrue(value['isError']);self.assertNotIn('queue_job_id',json.dumps(value))
        self.assertEqual(self.db.execute('SELECT state FROM workload_retry_operations').fetchone()[0],'dispatching')
        self.owner_file.write_text('{"username":"owner"}');self.transport.call=original
        recovered=await self.post('tools/call',{'name':'workload_operation','arguments':{k:self.args(RETRY)[k] for k in ('project_id','environment','service_id','request_id')}})
        self.assertEqual(recovered.json()['result']['structuredContent']['operation']['status'],'requeued')
        self.assertEqual(self.transport.calls,['queue.retry','queue.receipt'])

    async def test_separate_credential_cannot_read_original_retry(self):
        first=await self.post('tools/call',{'name':'queue_retry','arguments':self.args(RETRY)})
        self.assertFalse(first.json()['result'].get('isError',False))
        other=self.issue(['workloads:read']);self.http.headers['Authorization']='Bearer '+other['token']
        response=await self.post('tools/call',{'name':'workload_operation','arguments':{k:self.args(RETRY)[k] for k in ('project_id','environment','service_id','request_id')}})
        self.assertTrue(response.json()['result']['isError']);self.assertEqual(self.transport.calls,['queue.retry'])
