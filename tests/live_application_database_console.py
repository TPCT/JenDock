"""Actual browser → authenticated console → application attachment → PostgreSQL acceptance.

Git transport alone is simulated by copying immutable local fixture sources.
Uses private SQLite and disposable resources in Compose project deploy-monitor.
"""
import argparse,contextlib,hashlib,json,os,shutil,sys,tempfile,time,uuid,threading,socketserver
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Application,handler
from store import snapshot
import database_rpc
import psycopg
from cryptography.fernet import Fernet
import adoption,application_operations as ops,artifact_publication,build_profiles,control,database_candidates,job_target,managed_delivery,managed_services,migration_bindings,migration_credentials,migration_jobs,migration_runner,registry,resource_policy,worker
from store import connect,enqueue
import live_ingress_runtime as live
from live_ingress_runtime import create_application,deliver_application
import application_credentials,application_database_bindings,application_database_networks,service_runtime_operations
from live_migration_postgres import command,peers,source

PROJECT='deploy-monitor'
HELPER='sha256:181aa258b69f0be25f34a6c3a22ebac7d6dd76aab71d1c43df3994bfb02360b0'

from live_application_database import application_command

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--browser-ready',type=Path,required=True);options=parser.parse_args()
    ready=options.browser_ready.absolute();release=Path(str(ready)+'.release')
    assert not ready.exists() and not release.exists()
    docker=shutil.which('docker');compose=next(p['Path'] for p in json.loads(command([docker,'info','--format','{{json .ClientInfo.Plugins}}'])) if p['Name']=='compose')
    baseline=peers();nonce=uuid.uuid4().hex;containers=set();networks=set();volumes=[];service_ids=[];images=set();db=None;admin=None
    base=command([docker,'image','inspect','python:3.13-slim','--format','{{.Id}}']);pgimage=command([docker,'image','inspect','postgres:16-alpine','--format','{{.Id}}'])
    assert command([docker,'image','inspect',HELPER,'--format','{{.Id}}'])==HELPER
    for env in registry.ENVIRONMENTS:
        assert not command([docker,'network','ls','-q','--filter','name=^'+PROJECT+'-'+env+'-managed$'])
    with tempfile.TemporaryDirectory(prefix='application-console-',dir=str(Path(__file__).resolve().parents[1]/'.runtime')) as temporary:
        root=Path(temporary);(root/'managed').mkdir();(root/'work').mkdir();(root/'state').mkdir()
        try:
            build=root/'app-image';build.mkdir();(build/'Dockerfile').write_text('FROM '+HELPER+'\nENTRYPOINT []\nCMD []\nCOPY marker /fixture-marker\n');(build/'marker').write_text('one')
            base=command([docker,'build','-q',str(build)],env=dict(os.environ,DOCKER_BUILDKIT='0'));images.add(base)
            pgservice='migration-db-'+nonce[:8];pgname='deploy-monitor-migration-db-'+nonce
            cfg=root/'database.json';cfg.write_text(json.dumps({'services':{pgservice:{'image':'postgres:16-alpine'}}}))
            recipe={'compose_project':PROJECT,'compose_files':[str(cfg)],'services':[pgservice]}
            db=connect(root/'state.sqlite');registry.import_legacy(db,{PROJECT:{'repository':'TPCT/deploy-monitor','repository_id':12,'branch':'main','container_prefixes':[pgname],'recipe':recipe}},[])
            resource_policy.save(db,PROJECT,0,{'budget':{'memory_mib':512,'cpu':2},'environments':{e:{'budget':{'memory_mib':256,'cpu':1},'services':{}} for e in registry.ENVIRONMENTS}})
            pgnet=command([docker,'network','create','--label','com.docker.compose.project='+PROJECT,'--label','net.shiftcodes.migration-fixture='+nonce,'deploy-monitor-migration-'+nonce]);networks.add(pgnet)
            volume=command([docker,'volume','create','--label','net.shiftcodes.migration-fixture='+nonce,'--driver','local','--opt','type=tmpfs','--opt','device=tmpfs','--opt','o=size=268435456','deploy-monitor-migration-'+nonce]);volumes.append(volume)
            password=uuid.uuid4().hex
            pg=command([docker,'run','-d','--name',pgname,'--label','com.docker.compose.project='+PROJECT,'--label','com.docker.compose.service='+pgservice,'--label','com.docker.compose.project.config_files='+str(cfg),'--label','net.shiftcodes.migration-fixture='+nonce,'--network',pgnet,'--network-alias',pgservice,'--memory','256m','--cpus','.5','--mount','type=volume,src='+volume+',dst=/var/lib/postgresql/data','--env','POSTGRES_PASSWORD','--env','POSTGRES_DB=fixture','-p','127.0.0.1::5432','postgres:16-alpine'],env=dict(os.environ,POSTGRES_PASSWORD=password));containers.add(pg)
            row=json.loads(command([docker,'inspect',pg]))[0];port=int(row['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort'])
            for _ in range(80):
                try:admin=psycopg.connect(host='127.0.0.1',port=port,user='postgres',password=password,dbname='fixture',connect_timeout=1,autocommit=True);break
                except psycopg.Error:time.sleep(.25)
            assert admin
            admin.execute("CREATE TABLE business_sentinel(id integer PRIMARY KEY,value text);INSERT INTO business_sentinel VALUES(1,'preserved')")
            def scoped_inventory_command(args):
                if args[1:]==['ps','-aq','--no-trunc']:return pg
                return command(args)
            inventory=adoption.collect(docker,scoped_inventory_command)
            evidence=adoption.preview(db,PROJECT,'development',inventory)
            assert evidence['databases'][0]['database']=='fixture'
            adoption.adopt(db,PROJECT,'development',evidence['fingerprint'],inventory,'fixture-owner')
            import adopted_workloads
            rev=adopted_workloads.get(db,PROJECT,'development')['recipe_revision']
            app_password=uuid.uuid4().hex
            admin.execute(psycopg.sql.SQL('CREATE ROLE app_runtime LOGIN PASSWORD {}').format(psycopg.sql.Literal(app_password)))
            admin.execute('CREATE TABLE app_data(id integer PRIMARY KEY,value text);GRANT CONNECT ON DATABASE fixture TO app_runtime;GRANT USAGE ON SCHEMA public TO app_runtime;GRANT SELECT,INSERT,UPDATE ON app_data TO app_runtime')
            apps={}
            for environment in registry.ENVIRONMENTS:
                live.app_command=application_command
                apps[environment]=create_application(db,docker,compose,root,environment,'v1-'+environment,base,containers,networks,name='migration-fixture')
                service_ids.append(apps[environment]['id'])
            production_id=apps['production']['binding']['container_id']
            assert registry.get_environment(db,PROJECT,'development')['recipe_revision']!=rev
            assert adopted_workloads.get(db,PROJECT,'development')['recipe_revision']==rev
            db.close();db=connect(root/'state.sqlite')
            assert adopted_workloads.get(db,PROJECT,'development')['recipe_revision']==rev
            key=root/'vault';key.write_bytes(Fernet.generate_key());key.chmod(0o600)
            principal=ops.Principal('fixture-owner','console',frozenset({'databases:manage'}))
            secret=application_credentials.save_credential(db,principal,PROJECT,'development',{'username':'app_runtime','password':app_password},expected_revision=0,key_path=key)
            sid=apps['development']['id']
            attachment=application_database_bindings.save_binding(db,principal,PROJECT,'development',dict(service_id=sid,engine='postgresql',ownership={'kind':'adopted-compose','reference':pgservice,'revision':rev},database_name='fixture',database_port=5432,secret_id=secret['id'],secret_revision=1,environment_mapping={'url':'DATABASE_URL'}),expected_revision=0)
            initial_id=apps['development']['binding']['container_id'];container_name=apps['development']['binding']['container_name']
            login=uuid.uuid4().hex;credentials=root/'console.json';salt='ab'*16
            credentials.write_text(json.dumps({'username':'owner','salt':salt,'password_hash':hashlib.pbkdf2_hmac('sha256',login.encode(),bytes.fromhex(salt),260000).hex()}));credentials.chmod(0o600)
            config=root/'projects.json';config.write_text('{}');sock=str(root/'control.sock')
            with contextlib.ExitStack() as stack:
                for name,value in {'DATABASE':root/'state.sqlite','ROOT':root,'CONFIG':str(config),'DOCKER':docker,'COMPOSE':compose,'JOURNAL':root/'control.json'}.items():stack.enter_context(patch.object(control,name,value))
                stack.enter_context(patch.dict(os.environ,{'CREDENTIALS_FILE':str(credentials),'DATABASE_INSPECTION_KEY_FILE':str(key),'DATABASE_RECEIVER_UID':str(os.getuid()),'DATABASE_WORKER_UID':str(os.getuid()),'DOCKER_HOST':'unix:///var/run/docker.sock','STATE_DIR':str(root/'state')}))
                stack.enter_context(patch.object(database_rpc.request,'__defaults__',(sock,)))
                unix=socketserver.ThreadingUnixStreamServer(sock,control.Handler);unix.daemon_threads=True
                ut=threading.Thread(target=unix.serve_forever,daemon=True);ut.start();stack.callback(lambda:(unix.shutdown(),unix.server_close(),ut.join()))
                app=Application(config,root/'state.sqlite',credentials);app.secure=False;app.cookie_name='application_runtime_fixture';app.control_socket=sock;app.web_root=Path(__file__).resolve().parents[1]/'frontend/dist'
                http=ThreadingHTTPServer(('127.0.0.1',0),handler(app));app.origin=f'http://127.0.0.1:{http.server_port}'
                ht=threading.Thread(target=http.serve_forever,daemon=True);ht.start();stack.callback(lambda:(http.shutdown(),http.server_close(),ht.join()))
                def observe():
                    for service_id in service_ids:containers.update(command([docker,'ps','-aq','--no-trunc','--filter','label=net.shiftcodes.managed.service='+service_id]).split())
                    actual_ids=command([docker,'ps','-aq','--no-trunc','--filter','label=com.docker.compose.project='+PROJECT]).split()
                    rows=[]
                    for item in json.loads(command([docker,'inspect',*actual_ids])):
                        labels=item['Config'].get('Labels') or {}
                        rows.append({'ID':item['Id'],'Names':item['Name'].lstrip('/'),'Image':item['Config']['Image'],'State':item['State']['Status'],'Status':item['State']['Status'],'ComposeProject':labels.get('com.docker.compose.project'),'ComposeService':labels.get('com.docker.compose.service'),'ComposeConfigFiles':labels.get('com.docker.compose.project.config_files')})
                    snapshot(db,'containers',rows);snapshot(db,'worker',{'state':'running' if ut.is_alive() else 'stopped'})
                observe()
                descriptor=os.open(ready,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(descriptor,'w') as stream:json.dump({'url':app.origin,'username':'owner','password':login,'project':PROJECT,'environment':'development','container':container_name,'container_id':initial_id,'service_id':sid,'db':str(root/'state.sqlite')},stream)
                print(json.dumps({'browser_ready':str(ready),'url':app.origin,'container':container_name}),flush=True)
                deadline=time.monotonic()+900
                while not release.exists():
                    if time.monotonic()>deadline:raise TimeoutError('Browser acceptance deadline expired.')
                    observe();time.sleep(2)
                release.unlink();ready.unlink()
                current=managed_services.get(db,sid);cid=current['binding']['container_id'];containers.add(cid)
                assert cid!=initial_id,'Browser did not apply a replacement container'
                def request(path):return json.loads(command([docker,'exec',cid,'python','-c',"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/"+path+"',timeout=5).read().decode())"]))
                assert request('write')['rows']==[[1,'application-sentinel']]
                assert request('read')['rows']==[[1,'application-sentinel']]
                assert admin.execute('SELECT * FROM app_data').fetchall()==[(1,'application-sentinel')]
                assert admin.execute('SELECT * FROM business_sentinel').fetchall()==[(1,'preserved')]
                db.close();db=connect(root/'state.sqlite');assert managed_services.get(db,sid)['binding']['container_id']==cid
                actual=json.loads(command([docker,'inspect',cid]))[0]
                assert len(actual['NetworkSettings']['Networks'])==2
                assert pgnet not in [n['NetworkID'] for n in actual['NetworkSettings']['Networks'].values()]
                assert json.loads(command([docker,'inspect',production_id]))[0]['State']['Running']
                print(json.dumps({'browser_applied_new_identity':True,'actual_application_write_read':True,'database_sentinel_preserved':True,'sqlite_reconnect':True,'dedicated_database_bridge':True,'production_peer_preserved':True}),flush=True)
        finally:
            ready.unlink(missing_ok=True);release.unlink(missing_ok=True)
            if admin:admin.close()
            # Include replacement IDs only through fixture-owned service IDs.
            for sid in service_ids:
                containers.update(command([docker,'ps','-aq','--no-trunc','--filter','label=net.shiftcodes.managed.service='+sid]).split())
            for cid in containers:
                if not command([docker,'ps','-aq','--no-trunc','--filter','id='+cid]):continue
                actual=json.loads(command([docker,'inspect',cid]))[0];labels=actual['Config']['Labels']
                assert labels.get('net.shiftcodes.migration-fixture')==nonce or labels.get('net.shiftcodes.managed.service') in service_ids
                command([docker,'rm','-f','-v',cid])
            if db and db.execute("SELECT 1 FROM sqlite_master WHERE name='application_database_networks'").fetchone():
                for row in db.execute('SELECT document FROM application_database_networks'):
                    journal=json.loads(row['document'])
                    if journal.get('network_id'):networks.add(journal['network_id'])
            for net in networks:command([docker,'network','rm',net])
            for volume in volumes:command([docker,'volume','rm',volume])
            for image in images:
                if image not in (pgimage,HELPER):command([docker,'image','rm','-f',image])
            if db:db.close()
            assert peers()==baseline,'Peer identity/running state changed'
            assert not command([docker,'ps','-aq','--filter','label=net.shiftcodes.migration-fixture='+nonce])
            print(json.dumps({'exact_cleanup':True,'peers_preserved':True}),flush=True)

if __name__=='__main__':main()
