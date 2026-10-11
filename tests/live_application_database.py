"""Actual application credential → attachment → runtime → PostgreSQL acceptance.

Git transport alone is simulated by copying immutable local fixture sources.
Uses private SQLite and disposable resources in Compose project deploy-monitor.
"""
import contextlib,hashlib,json,os,shutil,sys,tempfile,time,uuid,traceback
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psycopg
from cryptography.fernet import Fernet
import adoption,application_operations as ops,artifact_publication,build_profiles,control,database_candidates,job_target,managed_delivery,managed_services,migration_bindings,migration_credentials,migration_jobs,migration_runner,registry,resource_policy,worker
from store import connect,enqueue
import live_ingress_runtime as live
from live_ingress_runtime import create_application,deliver_application
import application_credentials,application_database_bindings,application_database_networks,service_runtime_operations,managed_lifecycle,direct_limits
from live_migration_postgres import command,peers,source

PROJECT='deploy-monitor'
HELPER='sha256:181aa258b69f0be25f34a6c3a22ebac7d6dd76aab71d1c43df3994bfb02360b0'

def application_command(body):
    source = """import http.server,json,os,psycopg
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  result={'version':VERSION,'message':os.environ.get('APP_MESSAGE')}
  if self.path in ('/write','/read'):
   with psycopg.connect(os.environ['DATABASE_URL'],connect_timeout=3) as db:
    if self.path=='/write': db.execute("INSERT INTO app_data VALUES(1,'application-sentinel') ON CONFLICT(id) DO UPDATE SET value=excluded.value")
    result['rows']=db.execute('SELECT id,value FROM app_data ORDER BY id').fetchall()
  data=json.dumps(result).encode();self.send_response(200);self.end_headers();self.wfile.write(data)
 def log_message(self,*args):pass
http.server.HTTPServer(('0.0.0.0',8080),Handler).serve_forever()
""".replace('VERSION',repr(body))
    return ['python','-u','-c','exec('+repr(source)+')']

MODULES=('application_credentials.py','application_database_bindings.py','application_database_networks.py','service_runtime.py','service_runtime_operations.py','managed_lifecycle.py','managed_rollout.py','service_provisioning.py','managed_delivery.py','control.py','direct_limits.py')
def source_hashes():
    root=Path(__file__).resolve().parents[1]
    return {name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in MODULES}

def main():
    frozen=source_hashes();print(json.dumps({'source_hashes':frozen}),flush=True)
    docker=shutil.which('docker');compose=next(p['Path'] for p in json.loads(command([docker,'info','--format','{{json .ClientInfo.Plugins}}'])) if p['Name']=='compose')
    baseline=peers();print(json.dumps({'peer_baseline':baseline}),flush=True);nonce=uuid.uuid4().hex;containers=set();networks=set();volumes=[];service_ids=[];images=set();db=None;admin=None
    base=command([docker,'image','inspect','python:3.13-slim','--format','{{.Id}}']);pgimage=command([docker,'image','inspect','postgres:16-alpine','--format','{{.Id}}'])
    assert command([docker,'image','inspect',HELPER,'--format','{{.Id}}'])==HELPER
    for env in registry.ENVIRONMENTS:
        assert not command([docker,'network','ls','-q','--filter','name=^'+PROJECT+'-'+env+'-managed$'])
    with tempfile.TemporaryDirectory(prefix='managed-migration-',dir=str(Path(__file__).resolve().parents[1]/'.runtime')) as temporary:
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
            pin={'binding_id':attachment['id'],'revision':1,'digest':attachment['definition_digest']}
            with patch.dict(os.environ,{'DATABASE_INSPECTION_KEY_FILE':str(key),'DOCKER_HOST':'unix:///var/run/docker.sock'}):
                def current():return managed_services.get(db,sid)
                def request(path):
                    cid=current()['binding']['container_id'];containers.add(cid)
                    return json.loads(command([docker,'exec',cid,'python','-c',"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/"+path+"',timeout=5).read().decode())"]))
                def apply(values,**extra):
                    op=uuid.uuid4().hex
                    service_runtime_operations.accept(db,op,sid,values,expected_container_id=current()['binding']['container_id'],key_path=key,authorize=lambda:None,**extra)
                    import managed_rollout
                    original_rollout=managed_rollout.rollout
                    def diagnostic_rollout(*args,**kwargs):
                        try:return original_rollout(*args,**kwargs)
                        except Exception as error:
                            safe=str(error).replace(password,'[redacted]').replace(app_password,'[redacted]')[:1000]
                            print(json.dumps({'rollout_exception':type(error).__name__,'message':safe,'frames':[frame.name for frame in traceback.extract_tb(error.__traceback__)]}),flush=True)
                            raise
                    with patch.object(managed_rollout,'rollout',diagnostic_rollout):
                        result=service_runtime_operations.apply(db,op,docker=docker,compose=compose,authorize=lambda:None)
                    assert result['status']=='succeeded',result
                    containers.add(current()['binding']['container_id'])
                apply({'APP_MESSAGE':'persisted-setting'},database_attachment=pin)
                assert request('write')['rows']==[[1,'application-sentinel']]
                assert request('read')['message']=='persisted-setting'
                assert admin.execute('SELECT * FROM app_data').fetchall()==[(1,'application-sentinel')]
                apply({'APP_MESSAGE':'updated-setting'})
                assert request('read')['message']=='updated-setting'
                (build/'marker').write_text('two')
                next_image=command([docker,'build','-q',str(build)],env=dict(os.environ,DOCKER_BUILDKIT='0'));images.add(next_image)
                deliver_application(db,docker,compose,root,current(),next_image,'v2',containers,recover=True)
                assert request('read')['message']=='updated-setting'
                deliver_application(db,docker,compose,root,current(),base,'bad',containers,unhealthy=True)
                assert request('read')['version']=='v2'
                assert request('read')['rows']==[[1,'application-sentinel']]
                db.close();db=connect(root/'state.sqlite')
                assert request('read')['message']=='updated-setting'
                actual=json.loads(command([docker,'inspect',current()['binding']['container_id']]))[0]
                assert len(actual['NetworkSettings']['Networks'])==2
                assert pgnet not in [n['NetworkID'] for n in actual['NetworkSettings']['Networks'].values()]
                assert json.loads(command([docker,'inspect',production_id]))[0]['State']['Running']
                # Normal lifecycle uses the same exact container and preserves
                # its accepted database attachment and persisted desired state.
                lifecycle_cid=current()['binding']['container_id']
                for action in ('stop','start','restart'):
                    op=uuid.uuid4().hex
                    before=json.loads(command([docker,'inspect',lifecycle_cid]))[0]
                    managed_lifecycle.accept(db,op,sid,action,expected_container_id=lifecycle_cid,docker=docker,authorize=lambda:None)
                    outcome=managed_lifecycle.apply(db,op,docker=docker,authorize=lambda:None)
                    assert outcome['status']=='succeeded',outcome
                    after=json.loads(command([docker,'inspect',lifecycle_cid]))[0]
                    assert current()['binding']['container_id']==lifecycle_cid
                    assert after['State']['Running']==(action!='stop')
                    assert current()['binding']['state']==('stopped' if action=='stop' else 'running')
                    if action=='restart':assert after['State']['StartedAt']!=before['State']['StartedAt']
                    if action!='stop':assert request('read')['rows']==[[1,'application-sentinel']]
                # Capacity admission and normal immutable runtime transition;
                # no direct Docker update bypasses policy or replacement checks.
                actual=json.loads(command([docker,'inspect',lifecycle_cid]))[0]
                paths=[Path(p) for p in current()['binding']['compose_files']]
                cap_id=uuid.uuid4().hex;limits={'memory_mib':80,'cpu':.3}
                cap_body={'action':'container-limits-save','request_id':cap_id,'actor':'fixture-owner','project':PROJECT,
                          'container':current()['binding']['container_name'],'container_id':lifecycle_cid,'limits':limits,
                          'policy_revision':resource_policy.read(db,PROJECT)['revision'],
                          'revision':control.revision(paths,actual,json.loads(paths[0].read_text()))}
                transition=direct_limits.admit(db,cap_body,actual)
                assert transition is not None
                service_runtime_operations.accept(db,cap_id,sid,{'APP_MESSAGE':'updated-setting'},expected_container_id=lifecycle_cid,
                    key_path=key,authorize=lambda:None,resources=limits,resource_transition=transition)
                outcome=service_runtime_operations.apply(db,cap_id,docker=docker,compose=compose,authorize=lambda:None)
                assert outcome['status']=='succeeded',outcome
                actual=json.loads(command([docker,'inspect',current()['binding']['container_id']]))[0]
                containers.add(actual['Id'])
                assert actual['HostConfig']['Memory']==80*1024*1024
                assert actual['HostConfig']['NanoCpus']==300000000
                assert current()['definition']['resources']==limits
                assert resource_policy.read(db,PROJECT)['document']['environments']['development']['services'][current()['binding']['container_name']]==limits
                assert request('read')['rows']==[[1,'application-sentinel']]
                assert request('read')['message']=='updated-setting'
                print(json.dumps({'normal_stop_start_restart':True,'lifecycle_keeps_container_identity':True,'actual_resource_transition':limits,'database_access_after_caps':True}),flush=True)
                # Stop-only authority must work with Docker's real cleared DB
                # endpoint state, even after future credential selection is revoked.
                application_credentials.revoke_credential(db,principal,PROJECT,'development',secret['id'],expected_revision=1)
                application_database_bindings.disable_binding(db,principal,PROJECT,'development',attachment['id'],expected_revision=1)
                command([docker,'stop','--time','5',pg])
                stopped_database=json.loads(command([docker,'inspect',pg]))[0]
                assert stopped_database['State']['Running'] is False
                print(json.dumps({'stopped_database_endpoint_ids':[n['EndpointID'] for n in stopped_database['NetworkSettings']['Networks'].values()]}),flush=True)
                stop_id=uuid.uuid4().hex;cid=current()['binding']['container_id']
                managed_lifecycle.accept(db,stop_id,sid,'stop',expected_container_id=cid,docker=docker,authorize=lambda:None)
                stopped=managed_lifecycle.apply(db,stop_id,docker=docker,authorize=lambda:None)
                assert stopped['status']=='succeeded',stopped
                assert json.loads(command([docker,'inspect',cid]))[0]['State']['Running'] is False
                try:managed_lifecycle.accept(db,uuid.uuid4().hex,sid,'start',expected_container_id=cid,docker=docker,authorize=lambda:None)
                except ValueError:pass
                else:raise AssertionError('Revoked attachment must not permit startup')
                assert json.loads(command([docker,'inspect',cid]))[0]['State']['Running'] is False
                print(json.dumps({'actual_stopped_database_safe_app_stop':True,'revoked_credential_disabled_attachment_stop':True,'startup_rejected':True}),flush=True)
                print(json.dumps({'actual_application_read_write':True,'settings_survive_replacement_and_image_delivery':True,'failed_health_rollback':True,'sqlite_reconnect':True,'dedicated_database_bridge':True,'production_peer_preserved':True}),flush=True)
        finally:
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
            after_peers=peers()
            if after_peers!=baseline:print(json.dumps({'peer_diff':{identifier:{'before':baseline.get(identifier),'after':after_peers.get(identifier)} for identifier in baseline.keys()|after_peers.keys() if baseline.get(identifier)!=after_peers.get(identifier)}}),flush=True)
            print(json.dumps({'fixture_resources_removed':True}),flush=True)
            assert after_peers==baseline,'Peer identity/running state changed'
            assert not command([docker,'ps','-aq','--filter','label=net.shiftcodes.migration-fixture='+nonce])
            assert source_hashes()==frozen,'Product source changed during acceptance'
            print(json.dumps({'exact_cleanup':True,'peers_preserved':True,'source_hashes_unchanged':True}),flush=True)

if __name__=='__main__':main()
