"""Authenticated HTTP and official MCP SDK domain reads, with scoped credentials."""
import dataclasses
import hashlib
import json
from pathlib import Path
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
import ingress
import mcp_credentials
import mcp_server
import registry
from server import Application, handler
import test_ingress as domain_fixture
import test_mcp_server as mcp_fixture

class DomainHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture=domain_fixture.IngressTests();self.fixture.setUp()
        self.accepted=self.fixture.accept();root=self.fixture.fixture.tmp.name
        config=Path(root)/'config.json';config.write_text('{}')
        credentials=Path(root)/'console.json';credentials.write_text(json.dumps({'username':'owner','salt':'ab'*16,'password_hash':hashlib.pbkdf2_hmac('sha256',b'local-test-only',bytes.fromhex('ab'*16),260000).hex()}))
        app=Application(config,self.fixture.fixture.path,credentials);app.secure=False;app.cookie_name='monitor_session'
        self.http=ThreadingHTTPServer(('127.0.0.1',0),handler(app));self.origin=f'http://127.0.0.1:{self.http.server_port}';app.origin=self.origin
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        result,headers=self.post('/_deploy/api/login',{'username':'owner','password':'local-test-only'})
        self.cookie=headers['Set-Cookie'].split(';',1)[0];self.csrf=result['csrf']
    def tearDown(self):self.http.shutdown();self.http.server_close();self.thread.join();self.fixture.tearDown()
    def post(self,path,body,authenticated=False):
        headers={'Origin':self.origin,'Content-Type':'application/json'}
        if authenticated:headers.update(Cookie=self.cookie,**{'X-CSRF-Token':self.csrf})
        with urlopen(Request(self.origin+path,data=json.dumps(body).encode(),headers=headers),timeout=5) as response:return json.load(response),response.headers
    def action(self,action,**values):return self.post('/_deploy/api/action',dict(action=action,project='app',environment='development',**values),True)[0]
    def test_authenticated_options_list_get_and_operation_same_domain(self):
        options=self.action('domain-options');self.assertEqual(options['zones'][0]['id'],'b'*32)
        routes=self.action('domain-list')['routes'];self.assertEqual(routes[0]['id'],self.accepted['route']['id'])
        route=self.action('domain-get',route_id=routes[0]['id'])['route'];self.assertFalse(route['ready'])
        operation=self.action('domain-operation',operation_id=self.accepted['operation']['id'])['operation']
        self.assertEqual(operation['route_id'],route['id']);self.assertFalse(operation['executor_available'])
        with self.assertRaises(HTTPError) as error:self.post('/_deploy/api/action',{'action':'domain-list','project':'app','environment':'development'})
        self.assertEqual(error.exception.code,401);error.exception.close()
    def test_mutation_actions_are_not_enabled(self):
        with self.assertRaises(HTTPError) as error:self.action('domain-create',values=self.fixture.values,request_id='not-enabled-123456')
        error.exception.close()
        self.assertEqual(self.fixture.db.execute('SELECT count(*) FROM ingress_routes').fetchone()[0],1)

class DomainMCPTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=mcp_fixture.MCPTransportTests.asyncSetUp
    asyncTearDown=mcp_fixture.MCPTransportTests.asyncTearDown
    _callTestMethod=mcp_fixture.MCPTransportTests._callTestMethod
    post=mcp_fixture.MCPTransportTests.post
    async def domain_token(self):
        issued=mcp_credentials.create(self.db,self.owner,{'label':'Domain reads','scopes':['domains:read'],'allowed_projects':['sample'],'allowed_repository_ids':[12],'allowed_environments':[['sample','development']],'expires_at':time.time()+3600})
        self.http.headers['Authorization']='Bearer '+issued['token'];return issued
    def seed(self):
        ingress.initialize(self.db)
        definition={'project':'sample','environment':'development','service_id':'service','hostname':'dev.example.test','zone_id':'b'*32,'dns_type':'A','target':'192.0.2.10','proxied':True,'ttl':1,'http_endpoint':{'port':8080,'health_path':'/'},'source_environment':'development'}
        accepted={'route':{'id':'a'*32,'project':'sample','environment':'development','hostname':'dev.example.test','definition_id':'b'*32},'operation':{'id':'c'*32}}
        self.db.execute('INSERT INTO ingress_definitions VALUES(?,?,?,?)',('b'*32,registry.canonical(definition),ingress._digest(definition),time.time()))
        self.db.execute('INSERT INTO ingress_routes VALUES(?,?,?,?,?,?,?,?)',('a'*32,'sample','development','dev.example.test','b'*32,1,'accepted',time.time()))
        self.db.execute('INSERT INTO ingress_operations VALUES(?,?,?,?,?,?,?,?,?)',('c'*32,'owner',None,'domain-request-123456','d'*64,'a'*32,'pending-provisioning',registry.canonical(accepted),time.time()))
    async def test_sdk_read_discovery_scope_and_revocation(self):
        self.seed();issued=await self.domain_token()
        async with streamable_http_client('http://testserver/_deploy/mcp',http_client=self.http) as (read,write):
            async with ClientSession(read,write) as client:
                await client.discover();tools=(await client.list_tools()).tools
                self.assertEqual({tool.name for tool in tools},{'domain_options','domain_list','domain_get','domain_operation_status','domain_preview'})
                self.assertTrue(all(tool.annotations.read_only_hint for tool in tools))
                options=await client.call_tool('domain_options',{'project_id':'sample','environment':'development'})
                self.assertFalse(options.is_error);self.assertFalse(options.structured_content['executor_available'])
                direct=await client.call_tool('domain_get',{'project_id':'sample','environment':'development','route_id':'a'*32})
                self.assertFalse(direct.is_error);self.assertEqual(direct.structured_content['id'],'a'*32)
                listed=await client.call_tool('domain_list',{'project_id':'sample','environment':'development'})
                self.assertFalse(listed.is_error);self.assertEqual(listed.structured_content['routes'][0]['id'],'a'*32)
                status=await client.call_tool('domain_operation_status',{'project_id':'sample','environment':'development','operation_id':'c'*32})
                self.assertFalse(status.is_error);self.assertFalse(status.structured_content['ready'])
                denied=await client.call_tool('domain_list',{'project_id':'sample','environment':'production'})
                self.assertTrue(denied.is_error)
                mutation=await client.call_tool('domain_create',{'project_id':'sample','environment':'development'})
                self.assertTrue(mutation.is_error)
        mcp_credentials.revoke(self.db,self.owner,issued['credential']['id'])
        self.assertEqual((await self.post()).status_code,401)
    async def test_existing_credentials_unchanged_and_domain_scope_requires_environment(self):
        tools=(await self.post()).json()['result']['tools']
        self.assertFalse(any(tool['name'].startswith('domain_') for tool in tools))
        with self.assertRaises(ValueError):mcp_credentials.create(self.db,self.owner,{'label':'Unbounded domains','scopes':['domains:read'],'allowed_projects':['sample'],'allowed_repository_ids':[12],'allowed_environments':[],'expires_at':time.time()+3600})
        self.assertIn('domains:create',mcp_credentials.SCOPES)
        self.assertTrue({'project_create','service_create','domain_create','domain_resume','domain_adopt'} <= mcp_server.MUTATING_TOOLS)
