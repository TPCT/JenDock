"""Official SDK ASGI transport tests; no Docker/provider mutations."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
import application_operations as ops
import mcp_credentials as credentials
import mcp_server
import registry
from store import connect


class MCPTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.db'
        self.db = connect(self.path)
        registry.initialize(self.db)
        registry._insert(self.db, 'sample', {'repository': 'owner/sample', 'repository_id': 12, 'label': 'Sample'})
        self.owner = ops.Principal('owner', 'console', frozenset(mcp_server.SCOPES.values()) | {'services:start'})
        self.issued = credentials.create(self.db, self.owner, {'label': 'Test', 'scopes': ['projects:read', 'services:read', 'operations:read'],
            'allowed_projects': ['sample'], 'allowed_repository_ids': [12], 'allowed_environments': [['sample', 'development']],
            'expires_at': time.time() + 3600})
        self.app = mcp_server.create_app(self.path, Path(self.tmp.name) / 'key', allowed_hosts=['testserver'])
        self.lifespan = self.app.app.router.lifespan_context(self.app.app)

        self.http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), base_url='http://testserver',
            headers={'Authorization': 'Bearer ' + self.issued['token']})
        await self.http.__aenter__()

    async def asyncTearDown(self):
        await self.http.__aexit__(None,None,None)

        self.db.close(); self.tmp.cleanup()

    def _callTestMethod(self, method):
        async def run():
            async with self.lifespan:
                await method()
        self._asyncioRunner.run(run(), context=self._asyncioTestContext)

    async def post(self, method='tools/list', params=None, extra=None):
        headers = {'MCP-Protocol-Version': '2026-07-28', 'Mcp-Method': method, 'Accept': 'application/json, text/event-stream'}
        if method == 'tools/call': headers['Mcp-Name'] = params['name']
        headers.update(extra or {})
        return await self.http.post('/_deploy/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': {**(params or {}), '_meta': {'io.modelcontextprotocol/protocolVersion': '2026-07-28', 'io.modelcontextprotocol/clientCapabilities': {}}}}, headers=headers)

    async def test_actual_sdk_client_current_protocol_discovery_and_scope(self):
        async with streamable_http_client('http://testserver/_deploy/mcp', http_client=self.http) as (read, write):
            async with ClientSession(read, write) as client:
                discovered = await client.discover()
                self.assertIn('2026-07-28', str(discovered))
                tools = await client.list_tools()
                self.assertEqual({t.name for t in tools.tools}, {'project_list', 'project_get', 'service_list', 'service_get', 'operation_status'})
                out = await client.call_tool('project_get', {'project_id': 'sample'})
                self.assertFalse(out.is_error)
                self.assertEqual(set(out.structured_content['environments']), {'development'})
                self.assertNotIn('recipe', json.dumps(out.structured_content))
                credentials.revoke(self.db, self.owner, self.issued['credential']['id'])
                denied = await self.post()
                self.assertEqual(denied.status_code, 401)

    async def test_origin_host_and_auth_protection(self):
        for extra, status in [({'Host': 'attacker'},421), ({'Origin': 'null'},403),
                              ({'Origin': 'https://attacker'},403), ({'X-Forwarded-Host': 'testserver'},400),
                              ({'Authorization': ''},401), ({'Authorization': 'Bearer invalid'},401)]:
            self.assertEqual((await self.post(extra=extra)).status_code, status)

    async def test_sdk_metadata_mismatch_and_body_bound(self):
        for extra in [{'Mcp-Method': 'tools/call'}, {'MCP-Protocol-Version': '1900-01-01'}]:
            r = await self.post(extra=extra)
            self.assertTrue(r.status_code >= 400 or 'error' in r.json(), r.text)
        r = await self.post('tools/call', {'name': 'project_get', 'arguments': {'project_id': 'sample'}}, {'Mcp-Name': 'service_get'})
        self.assertTrue(r.status_code >= 400 or 'error' in r.json(), r.text)
        r = await self.post(params={'padding': 'x' * 70000})
        self.assertEqual(r.status_code, 413)

    async def test_schema_denials_and_private_result_sanitizing(self):
        for name, arguments in [('project_list', {'limit': 101}), ('project_get', {'project_id': 'sample', 'actor':'SECRET'}),
                                 ('project_create', {})]:
            r = await self.post('tools/call', {'name':name,'arguments':arguments})
            self.assertTrue(r.json()['result']['isError'], r.text)
            self.assertNotIn('SECRET', r.text)
        private = {'id':'service', 'definition': {'command': ['SECRET'], 'artifact': {'env': 'SECRET'}, 'name': 'test'},
                   'binding': {'password':'SECRET'}}
        self.assertNotIn('SECRET', json.dumps(mcp_server.public_service(private)))
        with patch.object(ops, 'get_project', side_effect=RuntimeError('SECRET provider traceback')):
            r = await self.post('tools/call', {'name':'project_get','arguments':{'project_id':'sample'}})
        self.assertTrue(r.json()['result']['isError']); self.assertNotIn('SECRET', r.text)

    async def test_rate_bound_and_sessionless_response(self):
        r = await self.post()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn('Mcp-Session-Id', r.headers)
        self.app.rates[self.owner.subject_id].extend([time.monotonic()] * 120)
        self.assertEqual((await self.post()).status_code, 429)

class MCPCreationSDKTests(unittest.TestCase):
    def test_all_tools_actual_sdk_template_selection_and_revocation_before_worker(self):
        # Controlled provider/build evidence fixture: this proves adapter persistence,
        # not actual image build/provisioning or external GitHub availability.
        import test_managed_services as fixtures
        import managed_services
        import integrations
        from cryptography.fernet import Fernet
        fixture = fixtures.ManagedServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        owner = ops.Principal('owner', 'console', frozenset(mcp_server.SCOPES.values()) | {'services:start'})
        issued = credentials.create(fixture.db, owner, {'label':'SDK all tools', 'scopes':list(owner.scopes),
            'allowed_projects':['app', 'new-app'], 'allowed_repository_ids':[12],
            'allowed_environments':[['app','development'],['new-app','development']], 'expires_at':time.time()+3600})
        key = Path(fixture.tmp.name) / 'key'
        key.write_bytes(Fernet.generate_key()); key.chmod(0o600)
        integrations.save_connection(fixture.db, 'github', 0, 'provider-secret', key,
            verifier=lambda *args: {'api_access':'verified','identity':'fixture','repositories':[], 'clone_access':'unverified','webhook_management':'unverified'})
        app = mcp_server.create_app(fixture.path, key, allowed_hosts=['testserver'], enable_creation=True)
        async def run():
            async with app.app.router.lifespan_context(app.app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers={'Authorization':'Bearer '+issued['token']}) as http:
                    async with streamable_http_client('http://testserver/_deploy/mcp', http_client=http) as (read, write):
                        async with ClientSession(read,write) as client:
                            await client.discover()
                            self.assertEqual({t.name for t in (await client.list_tools()).tools}, set(mcp_server.SCOPES))
                            entered, release = threading.Event(), threading.Event()
                            event_loop_thread = threading.get_ident()
                            def blocked_verifier(*args):
                                self.assertNotEqual(threading.get_ident(), event_loop_thread)
                                entered.set()
                                if not release.wait(5):
                                    raise AssertionError('Event loop could not release the blocked verifier.')
                                return {'id':12,'full_name':'TPCT/new-app'}
                            with patch.object(integrations, 'get_json', side_effect=blocked_verifier):
                                creating = asyncio.create_task(client.call_tool('project_create', {
                                    'project_id':'new-app','repository':'TPCT/new-app','repository_id':12,
                                    'idempotency_key':'sdk-project-key-12345'}))
                                try:
                                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                                    # An actual authenticated SDK read must finish while the
                                    # create operation is still waiting on its provider.
                                    listed_while_pending = await asyncio.wait_for(client.call_tool('project_list', {}), 1)
                                    self.assertFalse(listed_while_pending.is_error)
                                    self.assertEqual(len(listed_while_pending.structured_content['projects']), 1)
                                    self.assertFalse(creating.done())
                                finally:
                                    release.set()
                                    created = await creating
                            self.assertFalse(created.is_error, str(created))
                            self.assertEqual(set(created.structured_content['project']['environments']), {'development'})
                            listed = await client.call_tool('project_list', {})
                            self.assertEqual(len(listed.structured_content['projects']), 2)
                            self.assertFalse((await client.call_tool('project_get', {'project_id':'new-app'})).is_error)
                            empty = await client.call_tool('service_templates', {'project_id':'app','environment':'development'})
                            self.assertEqual(empty.structured_content['templates'], [])
                            self.assertIn('environment-specific build', empty.structured_content['empty_reason'])
                            # Publish verified fixture artifacts from both source environments;
                            # the credential permits only Development.
                            import job_target
                            from store import enqueue
                            for env, char in [('development', 'c'), ('production', 'd')]:
                                target = job_target.capture('app', env, 'TPCT/app', 12, 'main', char*40,
                                    'validate', 1, fixture.recipe, project_revision=1, environment_revision=1)
                                job, _ = enqueue(fixture.db, 'sdk-artifact-'+env, 'app', char*40, True, 'main',
                                    'validate', target_factory=lambda db, target=target: target)
                                fixture.db.execute("UPDATE jobs SET status='validated' WHERE id=?", (job,))
                                managed_services.publish_artifact(fixture.db, job, 'application-v1', 'sha256:'+char*64)
                            catalog = await client.call_tool('service_templates', {'project_id':'app','environment':'development'})
                            self.assertFalse(catalog.is_error, str(catalog))
                            metadata = catalog.structured_content
                            self.assertEqual(len(metadata['templates']), 1)
                            selected = metadata['templates'][0]
                            self.assertEqual(selected['source_environment'], 'development')
                            for private in ('command', 'healthcheck', 'user', 'legacy-unclassified', 'production'):
                                self.assertNotIn(private, json.dumps(metadata))
                            denied = await client.call_tool('service_templates', {'project_id':'app','environment':'production'})
                            self.assertTrue(denied.is_error)
                            definition = {'name':'web', 'artifact_id':selected['artifact_id'], 'template_id':selected['id'],
                                'template_revision':selected['revision'], 'environment_revision':metadata['environment_revision'],
                                'policy_revision':metadata['policy_revision'], 'resources':{'memory_mib':160,'cpu':0.5},
                                'desired_state':'stopped'}
                            args={'project_id':'app','environment':'development','definition':definition,'idempotency_key':'sdk-service-key-12345'}
                            created = await client.call_tool('service_create', args)
                            self.assertFalse(created.is_error, str(created))
                            value = created.structured_content
                            self.assertEqual(value['operation']['status'], 'queued')
                            replay = await client.call_tool('service_create', args)
                            self.assertEqual(replay.structured_content, value)
                            self.assertFalse((await client.call_tool('service_get', {'service_id':value['service']['id']})).is_error)
                            services = await client.call_tool('service_list', {'project_id':'app','environment':'development'})
                            self.assertEqual(len(services.structured_content['items']),1)
                            status = await client.call_tool('operation_status', {'operation_id':value['operation']['operation_id'], 'after_event':0, 'limit':1})
                            self.assertEqual(status.structured_content['status'],'queued')
                            self.assertEqual(status.structured_content['events'][0]['code'], 'service-queued')
                            self.assertFalse(status.structured_content['recovery']['mcp_available'])
                            following = await client.call_tool('operation_status', {'operation_id':value['operation']['operation_id'], 'after_event':status.structured_content['last_event'], 'limit':1})
                            self.assertEqual(following.structured_content['events'], [])
                            for output in (created, replay, services, status):
                                self.assertNotIn('provider-secret', str(output))
                                self.assertNotIn('command', str(output))
                            credentials.revoke(fixture.db,owner,issued['credential']['id'])
                            claim = managed_services.claim_next(fixture.db,'worker')
                            with self.assertRaises(ops.OperationError):
                                managed_services.authorize_claim(fixture.db,claim['operation_id'],'worker')
        asyncio.run(run())

if __name__ == '__main__': unittest.main()
