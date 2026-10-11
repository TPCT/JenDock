"""Official MCP protocol -> actual Unix worker; scripted engine evidence only."""
import copy
import hashlib
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from server import Application, handler
import dataclasses
import json
import os
import socketserver
import threading
import time
import unittest
from unittest.mock import patch
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
import control
import database_auth
import database_inspection
import database_rpc
import mcp_credentials
import mcp_server
import test_database_inspection as fixtures
import test_mcp_server as transport


class DatabaseMCPTests(unittest.IsolatedAsyncioTestCase):
    _callTestMethod=transport.MCPTransportTests._callTestMethod
    post=transport.MCPTransportTests.post
    async def asyncSetUp(self):
        self.fixture=fixtures.InspectionTests();self.fixture.setUp()
        self.db=self.fixture.db;self.path=self.fixture.f.f.path
        self.fixture.accept(self.fixture.preview())
        self.owner=dataclasses.replace(self.fixture.authority.principal,scopes=mcp_credentials.SCOPES)
        self.socket=str(self.fixture.config.parent/'mcp-control.sock')
        self.patchers=[patch.dict(os.environ,{'CREDENTIALS_FILE':str(self.fixture.config),'DATABASE_MCP_UID':str(os.getuid()),'DATABASE_RECEIVER_UID':str(os.getuid()),'DATABASE_WORKER_UID':str(os.getuid()),'DATABASE_INSPECTION_KEY_FILE':str(self.fixture.f.key)}),patch.object(control,'DATABASE',self.path),patch.object(control,'database_inspector',self.factory)]
        for p in self.patchers:p.start()
        self.unix=socketserver.ThreadingUnixStreamServer(self.socket,control.Handler);self.unix.daemon_threads=True
        self.thread=threading.Thread(target=self.unix.serve_forever,daemon=True);self.thread.start()
        self.fixture.config.write_text(json.dumps({'username':self.owner.subject_id,'salt':'ab'*16,'password_hash':hashlib.pbkdf2_hmac('sha256',b'fixture-login',bytes.fromhex('ab'*16),260000).hex()}))
        config=self.fixture.config.parent/'projects.json';config.write_text('{}')
        console=Application(config,self.path,self.fixture.config);console.secure=False;console.cookie_name='monitor_session'
        self.console=ThreadingHTTPServer(('127.0.0.1',0),handler(console));self.console_origin=f'http://127.0.0.1:{self.console.server_port}';console.origin=self.console_origin
        self.console_thread=threading.Thread(target=self.console.serve_forever,daemon=True);self.console_thread.start()
        self.cookie=None;self.csrf=None
        login,headers=self.console_post('login',{'username':self.owner.subject_id,'password':'fixture-login'})
        self.cookie=headers['Set-Cookie'].split(';',1)[0];self.csrf=login['csrf']
        self.issued=self.issue(['databases:read','databases:data:read'])
        self.app=mcp_server.create_app(self.path,self.fixture.f.key,allowed_hosts=['testserver'],database_socket=self.socket)
        self.lifespan=self.app.app.router.lifespan_context(self.app.app)
        self.http=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app),base_url='http://testserver',headers={'Authorization':'Bearer '+self.issued['token']});await self.http.__aenter__()
    def factory(self,db,authority):
        return database_inspection.InspectorService(db,authority,resolve_candidate=self.fixture.resolve,helper=self.fixture.helper,key_path=self.fixture.f.key,cursor_key=self.fixture.service.cursor_key)
    def console_post(self,path,body):
        headers={'Origin':self.console_origin,'Content-Type':'application/json'}
        if self.cookie:headers.update(Cookie=self.cookie,**{'X-CSRF-Token':self.csrf})
        with urlopen(Request(self.console_origin+'/_deploy/api/'+path,data=json.dumps(body).encode(),headers=headers),timeout=5) as response:return json.load(response),response.headers
    def issue(self,scopes):
        return self.console_post('action',{'action':'mcp-access-create','values':{'label':'Database test','scopes':scopes,'allowed_projects':['app'],'allowed_repository_ids':[12],'allowed_environments':[['app','development']],'expires_at':time.time()+3600}})[0]
    async def asyncTearDown(self):
        await self.http.__aexit__(None,None,None)
        self.unix.shutdown();self.unix.server_close();self.thread.join()
        self.console.shutdown();self.console.server_close();self.console_thread.join()
        for p in reversed(self.patchers):p.stop()
        self.fixture.doCleanups()
    async def test_sdk_read_exact_scope_no_secret_arguments_and_revocation(self):
        async with streamable_http_client('http://testserver/_deploy/mcp',http_client=self.http) as (read,write):
            async with ClientSession(read,write) as client:
                await client.discover();listed=(await client.list_tools()).tools
                self.assertEqual({tool.name for tool in listed},mcp_server.DATABASE_TOOLS)
                self.assertTrue(all(tool.annotations.read_only_hint for tool in listed))
                self.assertNotIn('password',json.dumps([tool.input_schema for tool in listed]))
                catalog=await client.call_tool('database_bindings',{'project_id':'app','environment':'development'})
                self.assertFalse(catalog.is_error);self.assertEqual(catalog.structured_content['bindings'][0]['id'],self.fixture.binding['id'])
                args={'project_id':'app','environment':'development','binding_id':self.fixture.binding['id'],'expected_revision':1}
                result=await client.call_tool('database_schemas',args)
                self.assertFalse(result.is_error);self.assertEqual(result.structured_content['data'],['public'])
                self.assertNotIn('private-password',json.dumps(result.structured_content))
                self.fixture.helper.response={'result':{'engine':'postgresql','identity':{'database':'app','user':'reader','database_oid':'42','server_version':'170004','cluster_identity_verified':False},'data':{'columns':['id'],'rows':[[{'value':'1','truncated':False}]]},'next_cursor':None,'page_size':25,'observed_at':time.time()}}
                rows=await client.call_tool('database_rows',dict(args,schema='public',table='sentinel'))
                self.assertFalse(rows.is_error);self.assertEqual(rows.structured_content['data']['rows'][0][0]['value'],'1')
                foreign=await client.call_tool('database_schemas',dict(args,environment='production'));self.assertTrue(foreign.is_error)
                extra=await client.call_tool('database_schemas',dict(args,password='forbidden'));self.assertTrue(extra.is_error)
                mcp_credentials.revoke(self.db,self.owner,self.issued['credential']['id'])
                self.assertEqual((await self.post()).status_code,401)
    async def test_worker_bearer_proof_restricts_mutations_and_owner_recheck(self):
        body={'action':'database-bindings','project':'app','environment':'development','authentication':{'kind':'mcp','token':self.issued['token']}}
        self.assertTrue(database_rpc.request(body,self.socket)['ok'])
        denied=database_rpc.request(dict(body,action='database-credentials'),self.socket);self.assertFalse(denied['ok'])
        denied=database_rpc.request(dict(body,authentication={'kind':'mcp','credential_id':self.issued['credential']['id']}),self.socket);self.assertFalse(denied['ok'])
        with patch.dict(os.environ,{'DATABASE_MCP_UID':str(os.getuid()+1)}):self.assertFalse(database_rpc.request(body,self.socket)['ok'])
        with patch.dict(os.environ):
            os.environ.pop('DATABASE_MCP_UID',None)
            self.assertFalse(database_rpc.request(body,self.socket)['ok'])
        self.fixture.config.write_text('{"username":"replacement"}')
        self.assertFalse(database_rpc.request(body,self.socket)['ok'])
    async def test_revocation_during_worker_read_suppresses_result(self):
        from store import connect
        def revoke():
            connection=connect(self.path)
            try:mcp_credentials.revoke(connection,self.owner,self.issued['credential']['id'])
            finally:connection.close()
        self.fixture.helper.hook=revoke
        response=await self.post('tools/call',{'name':'database_schemas','arguments':{'project_id':'app','environment':'development','binding_id':self.fixture.binding['id'],'expected_revision':1}})
        self.assertTrue(response.json()['result']['isError'])
        self.assertNotIn('database_oid',json.dumps(response.json()))

    async def test_scope_catalog_does_not_expand_issued_tokens(self):
        token=self.issue(['databases:read']);self.http.headers['Authorization']='Bearer '+token['token']
        tools=(await self.post()).json()['result']['tools'];self.assertNotIn('database_rows',{t['name'] for t in tools})
        with self.assertRaises(HTTPError) as error:self.issue(['databases:data:read'])
        error.exception.close()
        values={'label':'Unbounded','scopes':['databases:read'],'allowed_projects':['app'],'allowed_repository_ids':[12],'allowed_environments':[],'expires_at':time.time()+3600}
        with self.assertRaises(ValueError):mcp_credentials.create(self.db,self.owner,values)
        authority=database_auth.MCPAuthority(self.db,{'kind':'mcp','token':token['token']},self.fixture.config)
        mcp_credentials.revoke(self.db,self.owner,token['credential']['id'])
        with self.assertRaises(ValueError):authority.recheck()

class DatabaseMCPDisabledTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=transport.MCPTransportTests.asyncSetUp
    asyncTearDown=transport.MCPTransportTests.asyncTearDown
    _callTestMethod=transport.MCPTransportTests._callTestMethod
    post=transport.MCPTransportTests.post
    async def test_runtime_default_hides_database_tools(self):
        issued=mcp_credentials.create(self.db,self.owner,{'label':'Database disabled','scopes':['databases:read'],'allowed_projects':['sample'],'allowed_repository_ids':[12],'allowed_environments':[['sample','development']],'expires_at':time.time()+3600})
        self.http.headers['Authorization']='Bearer '+issued['token']
        tools=(await self.post()).json()['result']['tools']
        self.assertFalse(any(t['name'].startswith('database_') for t in tools))
