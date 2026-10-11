"""Real Docker/SDK/worker/console acceptance using synthetic temporary data only."""
import asyncio
from contextlib import ExitStack
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
import uvicorn
import application_operations as operations
import control
import job_target
import managed_services
import mcp_credentials
import mcp_server
import registry
import resource_policy
import server
import service_provisioning
import worker
from store import connect, snapshot
from live_service_creation import APP, HEALTH, PROJECT, command, inspect, peers


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def post(url, data, headers=None):
    request = urllib.request.Request(url, data=json.dumps(data).encode(), headers={
        'Content-Type': 'application/json', **(headers or {})})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response), response.headers


async def exercise(root, db, docker, compose, report, tracked_containers, tracked_networks):
    owner = operations.Principal('fixture-owner', 'console', mcp_credentials.SCOPES)
    issued = mcp_credentials.create(db, owner, {'label': 'Temporary local SDK fixture', 'scopes': list(owner.scopes),
        'allowed_projects': [PROJECT], 'allowed_repository_ids': [12],
        'allowed_environments': [[PROJECT, env] for env in registry.ENVIRONMENTS], 'expires_at': time.time()+3600})
    port = free_port(); console_port = free_port()
    origin = 'http://127.0.0.1:'+str(console_port)
    app = mcp_server.create_app(root/'state.sqlite', root/'unused-key', allowed_hosts=['127.0.0.1:'+str(port)], enable_creation=True)
    running = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    thread = threading.Thread(target=running.run, daemon=True); thread.start()
    for _ in range(100):
        if running.started: break
        await asyncio.sleep(.05)
    assert running.started, 'MCP HTTP listener failed to start'
    with patch.dict(os.environ, {'PUBLIC_ORIGIN': origin}):
        console_app = server.Application(str(root/'projects.json'), str(root/'state.sqlite'), str(root/'credentials.json'))
    console = ThreadingHTTPServer(('127.0.0.1', console_port), server.handler(console_app))
    console_thread = threading.Thread(target=console.serve_forever, daemon=True); console_thread.start()
    try:
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer '+issued['token']}) as http:
            async with streamable_http_client('http://127.0.0.1:'+str(port)+'/_deploy/mcp', http_client=http) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.discover()
                    tools = {tool.name for tool in (await client.list_tools()).tools}
                    assert {'service_templates', 'service_create', 'service_list', 'operation_status'} <= tools
                    for env in registry.ENVIRONMENTS:
                        catalog = await client.call_tool('service_templates', {'project_id': PROJECT, 'environment': env})
                        assert not catalog.is_error, str(catalog)
                        meta = catalog.structured_content
                        selected = next(row for row in meta['templates'] if row['source_environment'] == env)
                        args = {'project_id': PROJECT, 'environment': env, 'idempotency_key': str(uuid.uuid4()), 'definition': {
                            'name': 'fixture-mcp', 'artifact_id': selected['artifact_id'], 'template_id': selected['id'],
                            'template_revision': selected['revision'], 'environment_revision': meta['environment_revision'],
                            'policy_revision': meta['policy_revision'], 'resources': {'memory_mib': 64, 'cpu': .25}, 'desired_state': 'running'}}
                        accepted = await client.call_tool('service_create', args)
                        assert not accepted.is_error, str(accepted)
                        result = accepted.structured_content
                        assert result['operation']['status'] == 'queued'
                        assert worker.process_service_once(db), 'Real worker did not claim fixture'
                        service = managed_services.get(db, result['service']['id'])
                        assert service['state'] == 'running', service['state']
                        binding = service['binding']; tracked_containers.add(binding['container_id']); tracked_networks.update(binding['network_ids'])
                        actual = inspect(docker, binding['container_id'])
                        assert actual['State']['Running'] and actual['State']['Health']['Status'] == 'healthy'
                        assert not actual['Mounts'] and not actual['HostConfig']['PortBindings']
                        limits = command([docker, 'exec', actual['Id'], 'python', '-c', "from pathlib import Path;print(Path('/sys/fs/cgroup/memory.max').read_text().strip());print(Path('/sys/fs/cgroup/memory.swap.max').read_text().strip());print(Path('/sys/fs/cgroup/cpu.max').read_text().strip())"]).splitlines()
                        assert limits == ['67108864', '0', '25000 100000'], limits
                        status = await client.call_tool('operation_status', {'operation_id': result['operation']['operation_id']})
                        assert not status.is_error, str(status)
                        status_value = status.structured_content
                        assert (status_value.get('operation') or status_value)['status'] in ('running', 'succeeded'), status_value
                        listing = await client.call_tool('service_list', {'project_id': PROJECT, 'environment': env})
                        assert not listing.is_error and service['id'] in json.dumps(listing.structured_content)
                        repeated = await client.call_tool('service_create', args)
                        assert not repeated.is_error
                        assert repeated.structured_content['operation']['operation_id'] == result['operation']['operation_id']
                        assert not worker.process_service_once(db), 'Duplicate request queued another worker operation'
                        ids = command([docker,'ps','-aq','--filter','label=com.docker.compose.project.config_files='+str(root/'managed'/PROJECT/env/'compose.json')]).split()
                        assert len(ids) == 1
                        report['scenarios'].append({'environment': env, 'container_id': actual['Id'], 'operation_id': result['operation']['operation_id'],
                            'sdk_worker_console_flow': 'passed', 'health': 'healthy', 'limits': limits, 'duplicate_same_operation': True})
        observed = []
        for identifier in tracked_containers:
            actual = inspect(docker, identifier); labels = actual['Config']['Labels']
            observed.append({'ID': identifier, 'Names': actual['Name'].lstrip('/'), 'State': actual['State']['Status'],
                'ComposeProject': PROJECT, 'ComposeService': labels['com.docker.compose.service'],
                'ComposeConfigFiles': labels['com.docker.compose.project.config_files']})
        snapshot(db, 'containers', observed); snapshot(db, 'worker', {'state': 'running'})
        login, headers = post(origin+'/_deploy/api/login', {'username':'fixture-owner','password':'fixture-only-password'}, {'Origin': origin})
        session_headers = {'Origin': origin, 'Cookie': headers['Set-Cookie'].split(';')[0], 'X-CSRF-Token': login['csrf']}
        for env in registry.ENVIRONMENTS:
            listed, _ = post(origin+'/_deploy/api/action', {'action':'service-list','project':PROJECT,'environment':env}, session_headers)
            assert listed['services'][0]['state'] == 'running', listed
            view, _ = post(origin+'/_deploy/api/action', {'action':'environment-workspace','project':PROJECT,'environment':env}, session_headers)
            assert view['services'] and all(row['capabilities']['manage'] for row in view['services']), view
        report['authenticated_console_http'] = 'passed'
        with connect(root/'state.sqlite') as reopened:
            for env in registry.ENVIRONMENTS:
                assert managed_services.list_services(reopened, PROJECT, env)[0]['state'] == 'running'
        report['persistence_reconnect'] = 'passed'
        actual = [inspect(docker, row['container_id']) for row in report['scenarios']]
        prod_ip = next(iter(actual[1]['NetworkSettings']['Networks'].values()))['IPAddress']
        command([docker,'exec',actual[0]['Id'],'python','-c',"import socket;s=socket.socket();s.settimeout(2);assert s.connect_ex((%r,8080))!=0" % prod_ip])
        report['cross_environment_connection_denied'] = True
    finally:
        console.shutdown(); console.server_close(); console_thread.join(5)
        running.should_exit = True; thread.join(10)


def main():
    docker = shutil.which('docker')
    command([docker,'image','inspect','python:3.13-slim'])
    compose = next(item['Path'] for item in json.loads(command([docker,'info','--format','{{json .ClientInfo.Plugins}}'])) if item['Name']=='compose')
    for env in registry.ENVIRONMENTS:
        assert not command([docker,'network','ls','-q','--filter','name=^'+PROJECT+'-'+env+'-managed$']).strip(), 'Existing managed fixture network must not be reused'
    baseline = peers(docker); containers = set(); networks = set()
    runtime = ROOT/'.runtime'; runtime.mkdir(exist_ok=True)
    report = {'project': PROJECT, 'scope':'synthetic temporary local database; no production mutation', 'scenarios': []}
    with tempfile.TemporaryDirectory(prefix='mcp-creation-', dir=runtime) as directory:
        root = Path(directory); (root/'projects.json').write_text('{}')
        salt = '45'*16
        (root/'credentials.json').write_text(json.dumps({'username':'fixture-owner','salt':salt,
            'password_hash':hashlib.pbkdf2_hmac('sha256',b'fixture-only-password',bytes.fromhex(salt),260000).hex()}))
        (root/'Dockerfile').write_text('FROM python:3.13-slim\nWORKDIR /tmp\nLABEL local.mcp-fixture="'+str(uuid.uuid4())+'"\n')
        built = None; db = connect(root/'state.sqlite')
        try:
            built = command([docker,'build','--pull=false','--network=none','-q',str(root)]).strip()
            command([docker,'run','--rm','--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--user=65534:65534',built,'python','-c','import http.server,urllib.request'])
            registry.import_legacy(db, {}, [])
            registry.create_project(db, PROJECT, {'repository':'TPCT/deploy-monitor','repository_id':12})
            profile = {'revision':1,'image_name':'fixture-app','command':['python','-c',APP],'healthcheck':['python','-c',HEALTH],'user':'65534:65534'}
            recipe = {'compose_project':PROJECT,'compose_files':[str(root/'source.yml')],'services':['fixture'],'images':[{'name':'fixture-app'}],'service_profiles':{'fixture-http':profile}}
            for env in registry.ENVIRONMENTS:
                job = db.execute('INSERT INTO jobs(delivery,project,sha,created,updated,status,branch,mode,actor) VALUES(?,?,?,?,?,?,?,?,?)',
                    ('fixture-'+env,PROJECT,'a'*40,time.time(),time.time(),'validated','main','validate','fixture')).lastrowid
                target = job_target.capture(PROJECT,env,'TPCT/deploy-monitor',12,'main','a'*40,'validate',1,recipe)
                job_target.initialize(db); job_target.store(db,job,target)
                managed_services.publish_artifact(db,job,'fixture-http',built)
            resource_policy.save(db, PROJECT, 0, {'budget':{'memory_mib':512,'cpu':2},'environments':{
                env:{'budget':{'memory_mib':256,'cpu':1},'services':{}} for env in registry.ENVIRONMENTS}})
            db.execute('INSERT INTO telemetry VALUES(?,?)',(time.time(),json.dumps({'memory_total':8*1024**3,'memory_used':0,'cpu_count':8})))
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {'CREDENTIALS_FILE':str(root/'credentials.json')}))
                for module, name, value in [(control,'ROOT',root),(control,'JOURNAL',root/'control-journal.json'),
                    (worker,'STATE',root),(worker,'DOCKER',docker),(worker,'COMPOSE',compose)]:
                    stack.enter_context(patch.object(module,name,value))
                asyncio.run(exercise(root,db,docker,compose,report,containers,networks))
        finally:
            # Only exact temp-manifest matches authorize cleanup after interrupted responses.
            for env in registry.ENVIRONMENTS:
                containers.update(command([docker,'ps','-aq','--filter','label=com.docker.compose.project.config_files='+str(root/'managed'/PROJECT/env/'compose.json')]).split())
            for identifier in containers: command([docker,'rm','-f',identifier])
            for env in registry.ENVIRONMENTS:
                for identifier in command([docker,'network','ls','-q','--filter','name=^'+PROJECT+'-'+env+'-managed$']).split():
                    network=json.loads(command([docker,'network','inspect',identifier]))[0]
                    if network['Labels'].get(service_provisioning.PREFIX+'project')==PROJECT and network['Labels'].get(service_provisioning.PREFIX+'environment')==env:
                        networks.add(network['Id'])
            for identifier in networks: command([docker,'network','rm',identifier])
            db.close()
            if built: command([docker,'image','rm',built])
    assert peers(docker)==baseline, 'Existing peer identity changed'
    report.update(existing_peers_unchanged=True, cleanup='passed')
    (runtime/'mcp-creation-last.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__ == '__main__': main()
