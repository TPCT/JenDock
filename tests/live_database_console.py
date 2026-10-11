"""Local disposable HTTP -> worker -> Docker -> PostgreSQL acceptance."""
import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psycopg
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
import mcp_server
from psycopg import sql
from cryptography.fernet import Fernet
import adoption
import control
import database_candidates
import database_rpc
import registry
from database_inventory import DockerInventory
from database_helper import InspectorHelper
from database_inspection import InspectorService
from server import Application,handler
from store import connect, snapshot


def command(args,**kwargs):return subprocess.check_output(args,stderr=subprocess.DEVNULL,**kwargs).decode().strip()


async def verify_mcp(dbpath,key,sock,action,project,environment,binding,expected_scope):
    """Official SDK, real MCP application and Unix/Docker/PG path; no engine mocks."""
    issued=action('mcp-access-create',values={'label':'Live database fixture',
        'scopes':['databases:read','databases:data:read'],'allowed_projects':[project],
        'allowed_repository_ids':[1201],'allowed_environments':[[project,environment]],
        'expires_at':time.time()+600})
    app=mcp_server.create_app(dbpath,key,allowed_hosts=['testserver'],database_socket=sock)
    async with app.app.router.lifespan_context(app.app), httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
            base_url='http://testserver',headers={'Authorization':'Bearer '+issued['token']}) as http:
        async with streamable_http_client('http://testserver/_deploy/mcp',http_client=http) as (read,write):
            async with ClientSession(read,write) as client:
                await client.discover()
                assert {tool.name for tool in (await client.list_tools()).tools}==mcp_server.DATABASE_TOOLS
                args={'project_id':project,'environment':environment,'binding_id':binding['id'],'expected_revision':1}
                async def call(name,values):
                    result=await client.call_tool(name,values)
                    assert not result.is_error,'MCP '+name+' failed'
                    return result.structured_content
                schemas=await call('database_schemas',args)
                assert 'public' in schemas['data'] and schemas['scope']==expected_scope
                table=await call('database_table',dict(args,schema='public',table='sentinel'))
                assert table['scope']==expected_scope
                first=await call('database_rows',dict(args,schema='public',table='sentinel',limit=2))
                assert len(first['data']['rows'])==2 and first['data']['rows'][0][1]['value']==environment
                second=await call('database_rows',dict(args,schema='public',table='sentinel',limit=2,cursor=first['next_cursor']))
                assert len(second['data']['rows'])==1 and second['next_cursor'] is None
                assert first['scope']==expected_scope and second['scope']==expected_scope
                denied=await client.call_tool('database_schemas',dict(args,environment='production' if environment=='development' else 'development'))
                assert denied.is_error,'Foreign environment MCP read succeeded'
                action('mcp-access-revoke',credential_id=issued['credential']['id'])
                revoked=await http.post('/_deploy/mcp',json={'jsonrpc':'2.0','id':99,'method':'tools/list'},headers={'Accept':'application/json, text/event-stream'})
                assert revoked.status_code==401,'Revoked MCP bearer was accepted'
    return {'official_mcp_sdk_real_postgres':True,'mcp_pages':[2,1],
            'mcp_foreign_environment_denied':True,'mcp_revoked_bearer_denied':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-ready',type=Path,help='Private readiness JSON; release each environment with PATH.release (10 minute timeout).')
    arguments=parser.parse_args()
    ready=arguments.browser_ready.absolute() if arguments.browser_ready else None
    release=Path(str(ready)+'.release') if ready else None
    if ready and any(p.exists() or p.is_symlink() for p in (ready,release)):
        raise ValueError('Browser readiness/release paths must not already exist.')
    docker=shutil.which('docker');socket=os.environ.get('DOCKER_HOST','unix:///var/run/docker.sock')
    def peer_snapshot():
        ids=command([docker,'ps','-aq','--no-trunc']).splitlines()
        if not ids:return {}
        rows=json.loads(command([docker,'inspect',*ids]))
        return {r['Id']:{'name':r['Name'],'image':r['Image'],'running':r['State']['Running'],
            'restarting':r['State']['Restarting'],'status':r['State']['Status'],
            'started_at':r['State']['StartedAt'],'restart_count':r['RestartCount']} for r in rows}
    before_peers=peer_snapshot()
    before={identity for identity,row in before_peers.items() if row['running']}
    image=command([docker,'image','inspect','deploy-monitor-database-inspector:review','--format','{{.Id}}'])
    containers=[];networks=[];volumes=[];checks=[]
    login_password=uuid.uuid4().hex if ready else 'fixture-login'
    try:
        with tempfile.TemporaryDirectory(prefix='database-console-') as temporary:
            root=Path(temporary);dbpath=root/'state.sqlite';db=connect(dbpath)
            try:
                credential_file=root/'console.json';credential_file.write_text(json.dumps({'username':'owner','salt':'ab'*16,'password_hash':hashlib.pbkdf2_hmac('sha256',login_password.encode(),bytes.fromhex('ab'*16),260000).hex()}))
                key=root/'key';key.write_bytes(Fernet.generate_key());key.chmod(0o600)
                state=root/'helper';state.mkdir(mode=0o700)
                cursor_key=Fernet.generate_key().decode()
                inventory=DockerInventory(docker=docker,socket=socket)
                def factory(connection,authority):
                    def candidate(project,environment,reference,**kwargs):return database_candidates.resolve(connection,project,environment,reference,inventory=inventory,**kwargs)
                    return InspectorService(connection,authority,resolve_candidate=candidate,helper=InspectorHelper(docker=docker,socket=socket,image=image,state_dir=state),key_path=key,cursor_key=cursor_key)
                sock=str(root/'control.sock')
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.dict(os.environ,{'CREDENTIALS_FILE':str(credential_file),'DATABASE_INSPECTION_KEY_FILE':str(key),'DATABASE_RECEIVER_UID':str(os.getuid()),'DATABASE_MCP_UID':str(os.getuid()),'DATABASE_WORKER_UID':str(os.getuid())}))
                    stack.enter_context(patch.object(control,'DATABASE',dbpath));stack.enter_context(patch.object(control,'database_inspector',factory));stack.enter_context(patch.object(database_rpc.request,'__defaults__',(sock,)))
                    unix=socketserver.ThreadingUnixStreamServer(sock,control.Handler);unix.daemon_threads=True
                    ut=threading.Thread(target=unix.serve_forever,daemon=True);ut.start()
                    stack.callback(lambda:(unix.shutdown(),unix.server_close(),ut.join()))
                    config=root/'projects.json';config.write_text('{}')
                    app=Application(config,dbpath,credential_file);app.secure=False;app.cookie_name='monitor_session'
                    if ready:
                        compose_cli=shutil.which('docker-compose') or next((str(p) for p in (Path.home()/'.docker/cli-plugins/docker-compose',Path('/usr/libexec/docker/cli-plugins/docker-compose'),Path('/usr/lib/docker/cli-plugins/docker-compose')) if p.is_file()),None)
                        if not compose_cli:raise RuntimeError('A real Docker Compose executable is required for browser acceptance.')
                        app.web_root=(Path(__file__).resolve().parents[1]/'frontend/dist').resolve()
                        if not (app.web_root/'index.html').is_file():raise RuntimeError('Build frontend/dist before browser acceptance.')
                        app.control_socket=sock
                        for name,value in {'ROOT':root,'CONFIG':str(config),'DOCKER':docker,'COMPOSE':compose_cli,'JOURNAL':root/'environment-transaction.json'}.items():
                            stack.enter_context(patch.object(control,name,value))
                    http=ThreadingHTTPServer(('127.0.0.1',0),handler(app));app.origin=f'http://127.0.0.1:{http.server_port}'
                    ht=threading.Thread(target=http.serve_forever,daemon=True);ht.start();stack.callback(lambda:(http.shutdown(),http.server_close(),ht.join()))
                    cookie=None;csrf=None
                    def post(path,body):
                        headers={'Origin':app.origin,'Content-Type':'application/json'}
                        if cookie:headers.update(Cookie=cookie,**{'X-CSRF-Token':csrf})
                        with urlopen(Request(app.origin+path,data=json.dumps(body).encode(),headers=headers),timeout=20) as response:return json.load(response),response.headers
                    login,headers=post('/_deploy/api/login',{'username':'owner','password':login_password})
                    cookie=headers['Set-Cookie'].split(';',1)[0];csrf=login['csrf']
                    for environment in ('development','production'):
                        suffix=uuid.uuid4().hex;project='fixture-'+environment;service='db-'+environment
                        network=command([docker,'network','create','--label','com.docker.compose.project=deploy-monitor','deploy-monitor-db-console-'+suffix]);networks.append(network)
                        volume=command([docker,'volume','create','--label','com.docker.compose.project=deploy-monitor','--driver','local','--opt','type=tmpfs','--opt','device=tmpfs','--opt','o=size=268435456','deploy-monitor-db-console-'+suffix]);volumes.append(volume)
                        compose=root/(environment+'.yml');compose.write_text('services:\n  '+service+':\n    image: postgres:16-alpine\n')
                        password=uuid.uuid4().hex;reader_password=uuid.uuid4().hex
                        cid=command([docker,'run','-d','--label','com.docker.compose.project=deploy-monitor','--label','com.docker.compose.service='+service,'--label','com.docker.compose.project.config_files='+str(compose),'--network',network,'--network-alias',service,'--memory','256m','--cpus','0.5','--mount','type=volume,src='+volume+',dst=/var/lib/postgresql/data','--env','POSTGRES_PASSWORD','--env','POSTGRES_DB=fixture','-p','127.0.0.1::5432','postgres:16-alpine'],env=dict(os.environ,POSTGRES_PASSWORD=password));containers.append(cid)
                        row=json.loads(command([docker,'inspect',cid]))[0];port=int(row['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort'])
                        admin=None
                        for _ in range(80):
                            try:admin=psycopg.connect(host='127.0.0.1',port=port,user='postgres',password=password,dbname='fixture',connect_timeout=1,autocommit=True);break
                            except psycopg.Error:time.sleep(.25)
                        assert admin is not None,'database readiness failed'
                        with admin:
                            admin.execute(sql.SQL('CREATE ROLE inspector LOGIN PASSWORD {}').format(sql.Literal(reader_password)))
                            admin.execute('REVOKE ALL ON DATABASE fixture FROM PUBLIC;GRANT CONNECT ON DATABASE fixture TO inspector;REVOKE CREATE ON SCHEMA public FROM PUBLIC;GRANT USAGE ON SCHEMA public TO inspector')
                            admin.execute('CREATE TABLE sentinel(id bigint PRIMARY KEY,value text);GRANT SELECT ON sentinel TO inspector')
                            admin.execute('INSERT INTO sentinel VALUES(1,%s),(2,%s),(3,%s)',(environment,environment,environment))
                        registry.create_project(db,project,{'repository':'TPCT/'+project,'repository_id':1201})
                        recipe={'compose_project':'deploy-monitor','compose_files':[str(compose)],'services':[service]};document=registry.canonical(recipe)
                        revision=db.execute('INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,?)',(project,hashlib.sha256(document.encode()).hexdigest(),document,time.time())).lastrowid
                        db.execute('UPDATE registry_environments SET recipe_revision=? WHERE project=? AND name=?',(revision,project,environment))
                        adoption.initialize(db)
                        row=json.loads(command([docker,'inspect',cid]))[0];mount=next(m for m in row['Mounts'] if m['Destination']=='/var/lib/postgresql/data')
                        evidence={'project':project,'environment':environment,'containers':[{'id':cid,'image_id':row['Image'],'service':service}],'databases':[{'container_id':cid,'engine':'pgsql','database':'fixture','port':5432,'client':cid,'host':service,'storage':[{'type':'volume','source':mount['Source'],'destination':mount['Destination']}]}]}
                        evidence['fingerprint']=database_candidates.digest(evidence)
                        db.execute('INSERT INTO workload_adoptions VALUES(?,?,?,?,?,?)',(project,environment,evidence['fingerprint'],json.dumps(evidence),'owner',time.time()))
                        def action(name,**values):
                            try:return post('/_deploy/api/action',dict(action=name,project=project,environment=environment,**values))[0]
                            except HTTPError as error:
                                if name!='database-read':print(json.dumps({'action':name,'status':error.code,'response':json.loads(error.read())}),flush=True)
                                raise
                        if ready:
                            def refresh_observed():
                                # Project real observations into the collector's public snapshot contract.
                                observed=[]
                                for actual in json.loads(command([docker,'inspect',*containers])):
                                    labels=actual['Config'].get('Labels') or {}
                                    observed.append({'ID':actual['Id'],'Names':actual['Name'].lstrip('/'),'Image':actual['Config']['Image'],
                                        'State':actual['State']['Status'],'Status':actual['State']['Status'],
                                        'ComposeProject':labels.get('com.docker.compose.project'),
                                        'ComposeService':labels.get('com.docker.compose.service'),
                                        'ComposeConfigFiles':labels.get('com.docker.compose.project.config_files')})
                                snapshot(db,'containers',observed)
                                # The fixture's real Unix worker and collector must both be alive.
                                # Publish this only after the actual observation completed successfully.
                                snapshot(db,'worker',{'state':'running' if ut.is_alive() else 'stopped',
                                    'external_monitor':False,'outbound_alerts':False})
                            refresh_observed()
                            actual_details=action('container-details',container=row['Name'].lstrip('/'))
                            assert actual_details['container_id']==cid and actual_details['service']==service
                            document={'url':app.origin,'username':'owner','password':login_password,'reader_username':'inspector',
                                'reader_password':reader_password,'project':project,'environment':environment,'service':service,
                                'container':row['Name'].lstrip('/'),'container_id':cid,'database':'fixture','release_path':str(release)}
                            temporary_ready=ready.with_name(ready.name+'.'+uuid.uuid4().hex+'.tmp')
                            descriptor=os.open(temporary_ready,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                            with os.fdopen(descriptor,'w') as stream:json.dump(document,stream)
                            os.replace(temporary_ready,ready)
                            print(json.dumps({'browser_ready':str(ready),'environment':environment,'url':app.origin}),flush=True)
                            deadline=time.monotonic()+600
                            while not release.exists():
                                if time.monotonic()>=deadline:raise TimeoutError('Browser fixture release deadline expired.')
                                refresh_observed()
                                time.sleep(2)
                            release.unlink()
                            checks.append({'environment':environment,'browser_hold_released':True})
                        save={'values':{'username':'inspector','password':reader_password},'expected_revision':0,'request_id':'credential-'+suffix}
                        options=action('database-options',reference=service)['candidate']
                        assert options['container_id']==cid and options['reference']==service
                        assert options['project']==project and options['environment']==environment
                        assert options['recipe_revision']==revision and options['database']=='fixture'
                        assert options['port']==5432 and options['engine']=='postgresql'
                        assert 'storage' not in options and 'password' not in options
                        credential=action('database-credential-save',**save)['credential'];assert action('database-credential-save',**save)['credential']==credential
                        values={'engine':'postgresql','adapter_version':1,'ownership':{'kind':'adopted-compose','reference':service,'revision':revision},'database_name':'fixture','database_port':5432,'secret_id':credential['id'],'secret_revision':1,'allowed_schemas':['public'],'policy_revision':1,'row_columns':{'public':{'sentinel':['id','value']}}}
                        binding=action('database-binding-save',values=values,expected_revision=0,request_id='binding-'+suffix)['binding'];assert binding['verified'] is False and binding['acceptance'] is None
                        target={'binding_id':binding['id'],'expected_revision':1}
                        preview=action('database-preview',**target)
                        accepted=action('database-accept',preview_id=preview['preview_id'],request_id='accept-'+suffix)
                        assert action('database-accept',preview_id=preview['preview_id'],request_id='accept-'+suffix)==accepted
                        reloaded=action('database-binding-get',binding_id=binding['id'])['binding']
                        acceptance=reloaded['acceptance']
                        assert reloaded['verified'] is False and acceptance['runtime_revalidation_required'] is True
                        assert acceptance['binding_revision']==1 and acceptance['definition_digest']==binding['definition_digest']
                        assert acceptance['accepted_at']==accepted['accepted']
                        listed=action('database-bindings')['bindings']
                        assert next(v for v in listed if v['id']==binding['id'])['acceptance']==acceptance
                        request={'action':'rows','schema':'public','table':'sentinel','limit':2}
                        first=action('database-read',**target,request=request);assert len(first['data']['rows'])==2 and first['data']['rows'][0][1]['value']==environment
                        second=action('database-read',**target,request=dict(request,cursor=first['next_cursor']));assert len(second['data']['rows'])==1 and second['next_cursor'] is None
                        expected_scope={'project':project,'environment':environment,'binding_id':binding['id'],'binding_revision':1,
                            'definition_digest':binding['definition_digest'],'container_id':cid,'candidate_fingerprint':preview['candidate']['fingerprint']}
                        assert first['scope']==expected_scope and second['scope']==expected_scope
                        mcp_checks=asyncio.run(verify_mcp(dbpath,key,sock,action,project,environment,binding,expected_scope))
                        action('database-credential-revoke',credential_id=credential['id'],expected_revision=1)
                        revoked=action('database-binding-get',binding_id=binding['id'])['binding']
                        assert revoked['acceptance'] is None and revoked['status']=='credential-stale' and revoked['verified'] is False
                        assert next(v for v in action('database-bindings')['bindings'] if v['id']==binding['id'])['acceptance'] is None
                        try:action('database-read',**target,request=request);raise AssertionError('revoked credential read succeeded')
                        except HTTPError as error:assert error.code in (403,409);error.close()
                        if not ready:assert db.execute('SELECT count(*) FROM database_credentials WHERE project=?',(project,)).fetchone()[0]==1
                        assert not list(state.iterdir()),'helper recovery files remain'
                        assert reader_password not in json.dumps([credential,binding,preview,accepted,first,second])
                        checks.append({'environment':environment,**mcp_checks,'http_unix_docker_postgres':True,'pages':[2,1],'response_scope_verified':True,'acceptance_reload_verified':True,'revocation_clears_acceptance':True,'revocation_denied':True,'replay_no_duplicate':True,'cluster_identity_verified':False})
            finally:db.close()
    finally:
        if ready:ready.unlink(missing_ok=True)
        if release:release.unlink(missing_ok=True)
        for cid in containers:subprocess.run([docker,'rm','-f','-v',cid],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        for network in networks:subprocess.run([docker,'network','rm',network],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        for volume in volumes:subprocess.run([docker,'volume','rm',volume],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        after_peers=peer_snapshot()
        missing_identities=sorted(set(before_peers)-set(after_peers))
        changed_images=[identity for identity in before_peers.keys()&after_peers.keys() if before_peers[identity]['image']!=after_peers[identity]['image']]
        transitions={identity:{'before':row,'after':after_peers.get(identity)} for identity,row in before_peers.items() if row!=after_peers.get(identity)}
        if transitions:print(json.dumps({'peer_transitions':transitions}),flush=True)
        assert not missing_identities and not changed_images,'existing peer identity/image changed'
        after={identity for identity,row in after_peers.items() if row['running']}
        missing=sorted(before-after)
        if missing:print(json.dumps({'checks_completed':checks,'missing_running_peers':missing,'owned_resources_removed':True}),flush=True)
        assert not missing,'existing peer changed: '+','.join(missing)
        assert not command([docker,'ps','-aq','--filter','label=net.shiftcodes.database-inspection']),'helper remained'
    print(json.dumps({'checks':checks,'helper_image':image,'cleanup':True}))

if __name__=='__main__':
    # A stopped interactive test must still execute exact resource cleanup.
    # SIGKILL cannot be handled; SIGTERM should not strand a credential file or DB.
    def terminate(signum, frame):
        raise KeyboardInterrupt('Database browser fixture interrupted.')
    signal.signal(signal.SIGTERM, terminate)
    main()
