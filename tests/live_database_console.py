"""Local disposable HTTP -> worker -> Docker -> PostgreSQL acceptance."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
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
from store import connect


def command(args,**kwargs):return subprocess.check_output(args,stderr=subprocess.DEVNULL,**kwargs).decode().strip()


def main():
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
    try:
        with tempfile.TemporaryDirectory(prefix='database-console-') as temporary:
            root=Path(temporary);dbpath=root/'state.sqlite';db=connect(dbpath)
            try:
                credential_file=root/'console.json';credential_file.write_text(json.dumps({'username':'owner','salt':'ab'*16,'password_hash':hashlib.pbkdf2_hmac('sha256',b'fixture-login',bytes.fromhex('ab'*16),260000).hex()}))
                key=root/'key';key.write_bytes(Fernet.generate_key());key.chmod(0o600)
                state=root/'helper';state.mkdir(mode=0o700)
                cursor_key=Fernet.generate_key().decode()
                inventory=DockerInventory(docker=docker,socket=socket)
                def factory(connection,authority):
                    def candidate(project,environment,reference,**kwargs):return database_candidates.resolve(connection,project,environment,reference,inventory=inventory,**kwargs)
                    return InspectorService(connection,authority,resolve_candidate=candidate,helper=InspectorHelper(docker=docker,socket=socket,image=image,state_dir=state),key_path=key,cursor_key=cursor_key)
                sock=str(root/'control.sock')
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.dict(os.environ,{'CREDENTIALS_FILE':str(credential_file),'DATABASE_INSPECTION_KEY_FILE':str(key),'DATABASE_RECEIVER_UID':str(os.getuid()),'DATABASE_WORKER_UID':str(os.getuid())}))
                    stack.enter_context(patch.object(control,'DATABASE',dbpath));stack.enter_context(patch.object(control,'database_inspector',factory));stack.enter_context(patch.object(database_rpc.request,'__defaults__',(sock,)))
                    unix=socketserver.ThreadingUnixStreamServer(sock,control.Handler);unix.daemon_threads=True
                    ut=threading.Thread(target=unix.serve_forever,daemon=True);ut.start()
                    stack.callback(lambda:(unix.shutdown(),unix.server_close(),ut.join()))
                    config=root/'projects.json';config.write_text('{}')
                    app=Application(config,dbpath,credential_file);app.secure=False;app.cookie_name='monitor_session'
                    http=ThreadingHTTPServer(('127.0.0.1',0),handler(app));app.origin=f'http://127.0.0.1:{http.server_port}'
                    ht=threading.Thread(target=http.serve_forever,daemon=True);ht.start();stack.callback(lambda:(http.shutdown(),http.server_close(),ht.join()))
                    cookie=None;csrf=None
                    def post(path,body):
                        headers={'Origin':app.origin,'Content-Type':'application/json'}
                        if cookie:headers.update(Cookie=cookie,**{'X-CSRF-Token':csrf})
                        with urlopen(Request(app.origin+path,data=json.dumps(body).encode(),headers=headers),timeout=20) as response:return json.load(response),response.headers
                    login,headers=post('/_deploy/api/login',{'username':'owner','password':'fixture-login'})
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
                        registry.create_project(db,project,{'repository':'TPCT/'+project})
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
                        save={'values':{'username':'inspector','password':reader_password},'expected_revision':0,'request_id':'credential-'+suffix}
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
                        action('database-credential-revoke',credential_id=credential['id'],expected_revision=1)
                        revoked=action('database-binding-get',binding_id=binding['id'])['binding']
                        assert revoked['acceptance'] is None and revoked['status']=='credential-stale' and revoked['verified'] is False
                        assert next(v for v in action('database-bindings')['bindings'] if v['id']==binding['id'])['acceptance'] is None
                        try:action('database-read',**target,request=request);raise AssertionError('revoked credential read succeeded')
                        except HTTPError as error:assert error.code in (403,409);error.close()
                        assert db.execute('SELECT count(*) FROM database_credentials WHERE project=?',(project,)).fetchone()[0]==1
                        assert not list(state.iterdir()),'helper recovery files remain'
                        assert reader_password not in json.dumps([credential,binding,preview,accepted,first,second])
                        checks.append({'environment':environment,'http_unix_docker_postgres':True,'pages':[2,1],'response_scope_verified':True,'acceptance_reload_verified':True,'revocation_clears_acceptance':True,'revocation_denied':True,'replay_no_duplicate':True,'cluster_identity_verified':False})
            finally:db.close()
    finally:
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

if __name__=='__main__':main()
