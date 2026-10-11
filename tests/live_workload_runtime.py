"""Actual isolated two-environment Laravel worker/scheduler acceptance.

Uses real managed creation, retained database adoption, encrypted runtime attachment,
verified artifact publication and workload control/transport. No production calls.
"""
import argparse,base64,hashlib,json,os,shutil,sys,tempfile,time,uuid
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import psycopg
from cryptography.fernet import Fernet
import adopted_workloads,adoption,application_credentials,application_database_bindings,application_operations as ops
import artifact_publication,build_profiles,job_target,managed_lifecycle,managed_services,registry,resource_policy,service_provisioning,service_runtime_operations,workload_auth,workload_control
from workload_transport import WorkloadTransport
from workload_artifact_verifier import WorkloadArtifactVerifier,SQL_SCHEMA
from store import connect,enqueue
from live_workload_artifact import build,document,POSTGRES,peers,run
from live_migration_postgres import command
PROJECT='deploy-monitor'
TAG='deploy-monitor-workload-candidate:runtime-review'
MODULES=('adoption.py','adopted_workloads.py','database_candidates.py','service_provisioning.py','service_runtime_operations.py','managed_lifecycle.py','workload_control.py','workload_transport.py','workload_operations.py','workload_artifact_verifier.py','application_database_networks.py')

def until(check,seconds=30):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=check()
        if value:return value
        time.sleep(.4)
    raise AssertionError('Actual runtime condition did not complete within its bounded deadline.')

def publish(db,image,verifier,docker):
    before=build_profiles.get(db,PROJECT,'development')
    roles={document(image,role)['template_id']:{key:document(image,role)['workload'][key] for key in ('framework_version','instrumentation','configuration')} for role in ('queue','scheduler')}
    values={'dockerfile':'Dockerfile','context':'.','target':None,'test_command':['php','artisan','--version'],'command':['php','artisan','--version'],'healthcheck':['php','artisan','--version'],'user':'65534:65534','role_profiles':roles}
    build_profiles.save(db,PROJECT,'development',before['project_revision'],before['environment_revision'],0,values)
    source=build_profiles.get_private(db,PROJECT,'development');recipe=source['recipe'];current=build_profiles.get(db,PROJECT,'development')
    target=job_target.capture(PROJECT,'development','TPCT/deploy-monitor',12,'development','a'*40,'validate',source['source_revision'],recipe,project_revision=current['project_revision'],environment_revision=current['environment_revision'])
    job,_=enqueue(db,'actual-runtime-role-publication',PROJECT,'a'*40,True,'development','validate',target_factory=lambda db:target)
    artifact_publication.record(db,job,recipe,{recipe['images'][0]['name']:image},status='validated')
    assert artifact_publication.process(db,lambda ref:json.loads(run([docker,'image','inspect',ref]))[0]['Id'],job,verify_role=lambda doc:verifier.verify(db,doc))
    artifacts=[managed_services.artifact_for_service(db,PROJECT,r['id']) for r in db.execute('SELECT id FROM service_artifacts WHERE job_id=?',(job,))]
    return {a['workload']['role']:a for a in artifacts if 'workload' in a}

def create(db,docker,compose,root,environment,role,artifact,services,containers,networks):
    db.execute('INSERT INTO telemetry VALUES(?,?)',(time.time(),json.dumps({'memory_total':8*1024**3,'memory_used':0,'cpu_count':8})))
    principal=ops.Principal('fixture-owner','console',frozenset({'services:create','services:start','services:read','operations:read'}))
    values={'name':'fixture-'+role,'artifact_id':artifact['id'],'template_id':artifact['template_id'],'template_revision':artifact['template_revision'],'environment_revision':registry.get_environment(db,PROJECT,environment)['revision'],'policy_revision':resource_policy.read(db,PROJECT)['revision'],'resources':{'memory_mib':128,'cpu':.25},'desired_state':'stopped'}
    accepted=ops.create_service(db,principal,ops.service_input(PROJECT,environment,values),uuid.uuid4().hex)
    services.append(accepted['service']['id']);claim=managed_services.claim_next(db,'fixture-worker',lease_seconds=120)
    assert claim['operation_id']==accepted['operation']['operation_id']
    binding=service_provisioning.provision(db,claim['operation_id'],claim['definition'],root=root/'managed',docker=docker,compose=compose,authorize=lambda:managed_services.renew_claim(db,claim['operation_id'],'fixture-worker',lease_seconds=120))
    containers.add(binding['container_id']);networks.update(binding['network_ids'])
    return managed_services.finish(db,claim['operation_id'],'fixture-worker',binding,'stopped')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--image');parser.add_argument('--evidence',type=Path);args=parser.parse_args()
    docker=shutil.which('docker');compose=next(p['Path'] for p in json.loads(command([docker,'info','--format','{{json .ClientInfo.Plugins}}'])) if p['Name']=='compose')
    for environment in registry.ENVIRONMENTS:
        assert not command([docker,'network','ls','-q','--filter','name=^'+PROJECT+'-'+environment+'-managed$']), 'Managed fixture environment is already in use.'
    pg_metadata=json.loads(command([docker,'image','inspect',POSTGRES]))[0]
    pg_reference=next(ref for ref in pg_metadata['RepoDigests'] if ref.split('@')[0].split('/')[-1]=='postgres')
    assert json.loads(command([docker,'image','inspect',pg_reference]))[0]['Id']==POSTGRES
    baseline=peers(docker);hashes={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in MODULES}
    nonce=uuid.uuid4().hex;containers=set();networks=set();volumes={};services=[];admins={};db=None;success=False
    root=Path(tempfile.mkdtemp(prefix='deploy-monitor-workload-runtime-'));root.chmod(0o700);(root/'managed').mkdir()
    image=args.image
    try:
        if not image:image,_=build(docker,TAG)
        db=connect(root/'db');database_recipe={'compose_project':PROJECT,'compose_files':[str(root/'database.json')],'services':['development-db','production-db']}
        (root/'database.json').write_text(json.dumps({'services':{e+'-db':{'image':pg_reference} for e in registry.ENVIRONMENTS}}))
        registry.import_legacy(db,{PROJECT:{'repository':'TPCT/deploy-monitor','repository_id':12,'branch':'development','container_prefixes':['deploy-monitor-workload-'+nonce],'recipe':database_recipe}},[])
        resource_policy.save(db,PROJECT,0,{'budget':{'memory_mib':1024,'cpu':4},'environments':{e:{'budget':{'memory_mib':512,'cpu':2},'services':{}} for e in registry.ENVIRONMENTS}})
        vault=root/'vault';vault.write_bytes(Fernet.generate_key());vault.chmod(0o600)
        principal=ops.Principal('fixture-owner','console',frozenset({'databases:manage'}));secrets={};revisions={}
        for environment in registry.ENVIRONMENTS:
            dbservice=environment+'-db';password=uuid.uuid4().hex
            network=command([docker,'network','create','--label','net.shiftcodes.workload-fixture='+nonce,'deploy-monitor-workload-'+nonce+'-'+environment]);networks.add(network)
            name='deploy-monitor-workload-'+nonce+'-'+environment
            volume=command([docker,'volume','create','--label','com.docker.compose.project='+PROJECT,'--label','net.shiftcodes.workload-fixture='+nonce,name+'-data'])
            volumes[volume]=json.loads(command([docker,'volume','inspect',volume]))[0]
            cid=command([docker,'run','-d','--name',name,'--label','com.docker.compose.project='+PROJECT,'--label','com.docker.compose.service='+dbservice,'--label','com.docker.compose.project.config_files='+str(root/'database.json'),'--label','net.shiftcodes.workload-fixture='+nonce,'--network',network,'--network-alias',dbservice,'--memory','256m','--cpus','.5','--mount','type=volume,src='+volume+',dst=/var/lib/postgresql/data','--env','POSTGRES_PASSWORD','--env','POSTGRES_DB=fixture','-p','127.0.0.1::5432',pg_reference],env=dict(os.environ,POSTGRES_PASSWORD=password));containers.add(cid)
            row=json.loads(command([docker,'inspect',cid]))[0];port=int(row['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort'])
            def connect_pg():
                try:return psycopg.connect(host='127.0.0.1',port=port,user='postgres',password=password,dbname='fixture',connect_timeout=1,autocommit=True)
                except psycopg.Error:return None
            admin=until(connect_pg);admins[environment]=admin
            admin.execute(SQL_SCHEMA+'CREATE TABLE fixture_jobs(identifier text PRIMARY KEY,attempts integer NOT NULL,completed boolean NOT NULL);CREATE TABLE fixture_schedule(id bigserial PRIMARY KEY,observed_at timestamptz NOT NULL);')
            app_password=uuid.uuid4().hex;admin.execute(psycopg.sql.SQL('CREATE ROLE fixture_app LOGIN PASSWORD {}').format(psycopg.sql.Literal(app_password)))
            admin.execute('GRANT CONNECT ON DATABASE fixture TO fixture_app;GRANT USAGE ON SCHEMA public TO fixture_app;GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO fixture_app;GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO fixture_app')
            inventory=adoption.collect(docker,command)
            preview=adoption.preview(db,PROJECT,environment,inventory,services=[dbservice])
            adoption.adopt(db,PROJECT,environment,preview['fingerprint'],inventory,'fixture-owner',services=[dbservice])
            revisions[environment]=adopted_workloads.get(db,PROJECT,environment)['recipe_revision']
            secrets[environment]=application_credentials.save_credential(db,principal,PROJECT,environment,{'username':'fixture_app','password':app_password},expected_revision=0,key_path=vault)
        verifier=WorkloadArtifactVerifier(docker=docker,socket='unix:///var/run/docker.sock',postgres_image=POSTGRES,state_dir=root/'verifier')
        print(json.dumps({'stage':'both-databases-adopted'}),flush=True)
        artifacts=publish(db,image,verifier,docker)
        print(json.dumps({'stage':'both-role-artifacts-verified','image':image}),flush=True)
        owner=root/'owner';owner.write_text('{"username":"fixture-owner"}');owner.chmod(0o600);session=hashlib.sha256(os.urandom(32)).hexdigest();db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(session,uuid.uuid4().hex,'fixture-owner',time.time(),time.time()))
        authority=workload_auth.resolve(db,{'kind':'console','session_digest':session},owner);transport=WorkloadTransport(db,docker=docker,socket='unix:///var/run/docker.sock')
        running={}
        with patch.dict(os.environ,{'DATABASE_INSPECTION_KEY_FILE':str(vault),'DOCKER_HOST':'unix:///var/run/docker.sock'}):
            for environment in registry.ENVIRONMENTS:
                for role in ('queue','scheduler'):
                    saved=create(db,docker,compose,root,environment,role,artifacts[role],services,containers,networks);sid=saved['id'];secret=secrets[environment]
                    attachment=application_database_bindings.save_binding(db,principal,PROJECT,environment,dict(service_id=sid,engine='postgresql',ownership={'kind':'adopted-compose','reference':environment+'-db','revision':revisions[environment]},database_name='fixture',database_port=5432,secret_id=secret['id'],secret_revision=1,environment_mapping={'host':'DB_HOST','port':'DB_PORT','database':'DB_DATABASE','username':'DB_USERNAME','password':'DB_PASSWORD'}),expected_revision=0)
                    op=uuid.uuid4().hex;service_runtime_operations.accept(db,op,sid,{'APP_ENV':'testing','APP_KEY':'base64:'+base64.b64encode(os.urandom(32)).decode(),'DB_CONNECTION':'pgsql'},expected_container_id=saved['binding']['container_id'],key_path=vault,authorize=authority.recheck,database_attachment={'binding_id':attachment['id'],'revision':1,'digest':attachment['definition_digest']})
                    result=service_runtime_operations.apply(db,op,docker=docker,compose=compose,authorize=authority.recheck);assert result['status']=='succeeded',result
                    saved=managed_services.get(db,sid);containers.add(saved['binding']['container_id']);op=uuid.uuid4().hex
                    managed_lifecycle.accept(db,op,sid,'start',expected_container_id=saved['binding']['container_id'],docker=docker,authorize=authority.recheck);result=managed_lifecycle.apply(db,op,docker=docker,authorize=authority.recheck);assert result['status']=='succeeded',result
                    running[environment,role]=sid
                    print(json.dumps({'stage':'managed-role-running','environment':environment,'role':role}),flush=True)
            def target(environment,role):return workload_control.recorded_workload(db,managed_services.get(db,running[environment,role]))['target']
            def call(action,environment,role,**extra):return workload_control.execute(db,{'action':action,**target(environment,role),**extra},transport=transport,authority=authority)
            for environment in registry.ENVIRONMENTS:
                cid=target(environment,'queue')['container_id'];command([docker,'exec',cid,'php','artisan','fixture:dispatch','successful'])
                command([docker,'exec',cid,'php','artisan','fixture:dispatch','retry-case','--fail-first'])
                admin=admins[environment];until(lambda:admin.execute("SELECT completed FROM fixture_jobs WHERE identifier='successful'").fetchone()==(True,))
                until(lambda:admin.execute('SELECT count(*) FROM failed_jobs').fetchone()[0]==1)
                response=call('queue-failures',environment,'queue',connection_id='database',queue='default');failure=response['workload']['observation']['result']['items'][0];assert failure['retry_supported']
                request_id=uuid.uuid4().hex;reply=call('queue-retry',environment,'queue',connection_id='database',queue='default',failed_job_id=failure['failed_job_id'],failure_revision=failure['failure_revision'],request_id=request_id);assert reply['operation']['status']=='requeued'
                until(lambda:admin.execute("SELECT attempts,completed FROM fixture_jobs WHERE identifier='retry-case'").fetchone()==(2,True))
                recovered=workload_control.execute(db,dict(action='workload-operation',project=PROJECT,environment=environment,service_id=running[environment,'queue'],request_id=request_id),transport=transport,authority=authority);assert recovered['operation']['status']=='requeued'
                assert admin.execute('SELECT count(*) FROM failed_jobs').fetchone()==(0,)
                observed=call('workload-role-details',environment,'queue')['workload']['observation'];assert observed['status']=='ok' and observed['result']['workers']
                until(lambda:admin.execute('SELECT count(*) FROM fixture_schedule').fetchone()[0]>=1,75)
                schedule=call('workload-role-details',environment,'scheduler')['workload']['observation'];assert schedule['status']=='ok' and len(schedule['result']['items'])==1
                until(lambda:any(x['outcome']=='completed' for x in call('scheduler-history',environment,'scheduler',registry_revision=target(environment,'scheduler')['definition_digest'])['workload']['observation']['result']['items']))
            environment_networks={}
            for environment in registry.ENVIRONMENTS:
                environment_networks[environment]=set()
                for role in ('queue','scheduler'):
                    actual=json.loads(command([docker,'inspect',target(environment,role)['container_id']]))[0]
                    environment_networks[environment].update(n['NetworkID'] for n in actual['NetworkSettings']['Networks'].values())
            assert not (environment_networks['development'] & environment_networks['production'])
            production_before={r:target('production',r)['container_id'] for r in ('queue','scheduler')};ticks=admins['production'].execute('SELECT count(*) FROM fixture_schedule').fetchone()[0]
            for role in ('queue','scheduler'):
                sid=running['development',role];op=uuid.uuid4().hex;managed_lifecycle.accept(db,op,sid,'stop',expected_container_id=target('development',role)['container_id'],docker=docker,authorize=authority.recheck);assert managed_lifecycle.apply(db,op,docker=docker,authorize=authority.recheck)['status']=='succeeded'
            command([docker,'exec',production_before['queue'],'php','artisan','fixture:dispatch','after-development-stop']);until(lambda:admins['production'].execute("SELECT completed FROM fixture_jobs WHERE identifier='after-development-stop'").fetchone()==(True,))
            until(lambda:admins['production'].execute('SELECT count(*) FROM fixture_schedule').fetchone()[0]>ticks,75)
            assert all(target('production',r)['container_id']==production_before[r] for r in production_before)
            assert admins['development'].execute("SELECT count(*) FROM fixture_jobs WHERE identifier='after-development-stop'").fetchone()==(0,)
            print(json.dumps({'stage':'two-environment-runtime-verified','queue_failure_retry':True,'scheduler_history':True,'disjoint_networks':True,'development_stop_production_continues':True}),flush=True)
            success=True
    finally:
        for admin in admins.values():admin.close()
        for sid in services:containers.update(command([docker,'ps','-aq','--no-trunc','--filter','label=net.shiftcodes.managed.service='+sid]).split())
        for cid in containers:
            if not command([docker,'ps','-aq','--no-trunc','--filter','id='+cid]):continue
            row=json.loads(command([docker,'inspect',cid]))[0];labels=row['Config']['Labels'];assert labels.get('net.shiftcodes.workload-fixture')==nonce or labels.get('net.shiftcodes.managed.service') in services
            command([docker,'rm','-f','-v',cid])
        if db and db.execute("SELECT 1 FROM sqlite_master WHERE name='application_database_networks'").fetchone():
            for row in db.execute('SELECT document FROM application_database_networks'):
                net=json.loads(row['document']).get('network_id')
                if net:networks.add(net)
        for network in networks:command([docker,'network','rm',network])
        for volume,original in volumes.items():
            actual=json.loads(command([docker,'volume','inspect',volume]))[0]
            assert actual['Labels'].get('net.shiftcodes.workload-fixture')==nonce
            assert all(actual.get(k)==original.get(k) for k in ('Name','Driver','Mountpoint','CreatedAt','Options','Labels'))
            assert not command([docker,'ps','-aq','--filter','volume='+volume])
            command([docker,'volume','rm',volume])
        if db:db.close()
        after=peers(docker)
        (root/'peer-observations.json').write_text(json.dumps({'before':baseline,'after':after}))
        if not success or after!=baseline:print(json.dumps({'fixture_state_retained':str(root)}),flush=True)
        assert after==baseline,'Peer containers changed.'
        assert hashes=={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in MODULES},'Source changed during acceptance.'
        evidence={'passed':success,'candidate_image':image,'postgres_image':POSTGRES,'source_hashes':hashes,'actual_two_environment_runtime':success,'queue_consumption_failure_retry':success,'scheduler_recurring_history':success,'disjoint_environment_networks':success,'development_stop_production_continues':success,'exact_cleanup':True,'peers_unchanged':True,'candidate_retained':True}
        if args.evidence:args.evidence.write_text(json.dumps(evidence,indent=2)+'\n')
        print(json.dumps(evidence),flush=True)
        if success:shutil.rmtree(root)
        else:print(json.dumps({'fixture_state_retained':str(root)}),flush=True)
if __name__=='__main__':main()
