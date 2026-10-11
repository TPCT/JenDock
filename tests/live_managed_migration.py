"""Actual queued managed migration→Docker build→rollout acceptance.

Git transport alone is simulated by copying immutable local fixture sources.
Uses private SQLite and disposable resources in Compose project deploy-monitor.
"""
import contextlib,hashlib,json,os,shutil,sys,tempfile,time,uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psycopg
from cryptography.fernet import Fernet
import adoption,application_operations as ops,artifact_publication,build_profiles,control,database_candidates,job_target,managed_delivery,managed_services,migration_bindings,migration_credentials,migration_jobs,migration_runner,registry,resource_policy,worker
from store import connect,enqueue
from live_ingress_runtime import create_application,app_command
from live_migration_postgres import command,peers,source

PROJECT='deploy-monitor'
HELPER='sha256:181aa258b69f0be25f34a6c3a22ebac7d6dd76aab71d1c43df3994bfb02360b0'

def main():
    docker=shutil.which('docker');compose=next(p['Path'] for p in json.loads(command([docker,'info','--format','{{json .ClientInfo.Plugins}}'])) if p['Name']=='compose')
    baseline=peers();nonce=uuid.uuid4().hex;containers=set();networks=set();volumes=[];service_ids=[];images=set();db=None;admin=None
    base=command([docker,'image','inspect','python:3.13-slim','--format','{{.Id}}']);pgimage=command([docker,'image','inspect','postgres:16-alpine','--format','{{.Id}}'])
    assert command([docker,'image','inspect',HELPER,'--format','{{.Id}}'])==HELPER
    for env in registry.ENVIRONMENTS:
        assert not command([docker,'network','ls','-q','--filter','name=^'+PROJECT+'-'+env+'-managed$'])
    with tempfile.TemporaryDirectory(prefix='managed-migration-',dir=str(Path(__file__).resolve().parents[1]/'.runtime')) as temporary:
        root=Path(temporary);(root/'managed').mkdir();(root/'work').mkdir();(root/'state').mkdir()
        try:
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
            apps={}
            for environment in registry.ENVIRONMENTS:
                apps[environment]=create_application(db,docker,compose,root,environment,'v1-'+environment,base,containers,networks,name='migration-fixture')
                service_ids.append(apps[environment]['id'])
            production_id=apps['production']['binding']['container_id']
            assert registry.get_environment(db,PROJECT,'development')['recipe_revision']!=rev
            assert adopted_workloads.get(db,PROJECT,'development')['recipe_revision']==rev
            db.close();db=connect(root/'state.sqlite')
            assert adopted_workloads.get(db,PROJECT,'development')['recipe_revision']==rev
            key=root/'vault';key.write_bytes(Fernet.generate_key());key.chmod(0o600)
            principal=ops.Principal('fixture-owner','console',frozenset({'databases:manage'}))
            secret=migration_credentials.save_credential(db,principal,PROJECT,'development',{'username':'postgres','password':password},expected_revision=0,key_path=key)
            migration_bindings.save_binding(db,principal,PROJECT,'development',dict(engine='postgresql',adapter_version=1,ownership={'kind':'adopted-compose','reference':pgservice,'revision':rev},database_name='fixture',database_port=5432,secret_id=secret['id'],secret_revision=1,framework='alembic',grammar=None,migration_paths=['versions'],ledger_schema='public'),expected_revision=0)
            with contextlib.ExitStack() as stack:
                for obj,keyname,value in [(worker,'DOCKER',docker),(worker,'COMPOSE',compose),(worker,'STATE',root/'state'),(worker,'ARTIFACT_WORK',root/'work'),(worker,'ARTIFACT_BUILD_NETWORK','none'),(control,'ROOT',root),(control,'DOCKER',docker),(control,'COMPOSE',compose),(control,'JOURNAL',root/'control.json')]:stack.enter_context(patch.object(obj,keyname,value))
                stack.enter_context(patch.dict(os.environ,{'MIGRATION_HELPER_IMAGE':HELPER,'DATABASE_INSPECTION_KEY_FILE':str(key),'DOCKER_HOST':'unix:///var/run/docker.sock'}))
                stack.enter_context(patch.object(worker,'schedule_cleanup'))
                def queue(number,extra=False):
                    local=root/('source'+str(number));(local/'versions').mkdir(parents=True)
                    (local/'Dockerfile').write_text('FROM '+base+'\nCOPY marker /fixture-marker\n')
                    (local/'marker').write_text(str(number))
                    (local/'versions/one.py').write_text(source('one',None,["op.create_table('items',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('name',sa.String(80),nullable=True))"]))
                    if extra:(local/'versions/two.py').write_text(source('two','one',["op.add_column('items',sa.Column('note',sa.Text(),nullable=True))"]))
                    selection=managed_delivery.capture(db,PROJECT,'development',[apps['development']['id']])
                    profile=build_profiles._recipe(PROJECT,'development',{'dockerfile':'Dockerfile','context':'.','target':None,'command':app_command('v'+str(number)),'healthcheck':['python','-c',"import urllib.request;assert urllib.request.urlopen('http://127.0.0.1:8080/',timeout=2).status==200"],'user':'65534:65534','test_command':['python','-c','import http.server'],'http_endpoint':{'port':8080,'health_path':'/'}})
                    profile.update(schema_kind='managed-build',managed_target=selection);sha=hashlib.sha1(('fixture-'+nonce+str(number)).encode()).hexdigest()
                    target=job_target.capture(PROJECT,'development','TPCT/deploy-monitor',12,'main',sha,'deploy',rev,profile,project_revision=registry.get_project(db,PROJECT)['revision'],environment_revision=selection['environment_revision'])
                    jid,_=enqueue(db,nonce+'-'+str(number),PROJECT,sha,True,'main','deploy',target_factory=lambda db:target)
                    job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone());actual_run=worker.run
                    def run(args,**kwargs):
                        if args[:2]==['git','clone']:shutil.copytree(local,args[-1]);return ''
                        if args[:2]==['git','rev-parse']:return sha
                        if args[:2]==['git','ls-files']:return '\n'.join(p.relative_to(local).as_posix() for p in local.rglob('*') if p.is_file())
                        return actual_run(args,**kwargs)
                    with patch.object(worker,'run',side_effect=run):worker.build_artifacts(db,job,target,False)
                    images.add(command([docker,'image','inspect',PROJECT+'-development:'+sha[:12]+'-'+str(jid),'--format','{{.Id}}']))
                    assert migration_jobs.load(db,jid)['state']=='source-ready'
                    return jid
                def appid():return managed_services.get(db,apps['development']['id'])['binding']['container_id']
                def assert_runtime(jid,body):
                    assert worker.process_managed_delivery(db,jid),dict(db.execute('SELECT status,detail FROM jobs WHERE id=?',(jid,)).fetchone())
                    saved=managed_services.get(db,apps['development']['id']);cid=saved['binding']['container_id'];containers.add(cid)
                    assert json.loads(command([docker,'inspect',cid]))[0]['State']['Health']['Status']=='healthy'
                    assert command([docker,'exec',cid,'python','-c',"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/').read().decode())"])==body
                    assert migration_jobs.load(db,jid)['state']=='verified'
                    return cid
                first=queue(1);newid=assert_runtime(first,'v1');assert newid!=apps['development']['binding']['container_id']
                admin.execute("INSERT INTO items(name) VALUES('business-row')")
                second=queue(2);assert_runtime(second,'v2');assert migration_jobs.load(db,second)['proof']['helper_proof']['plan']['migrations']==[]
                print(json.dumps({'first_migration_rollout':True,'subsequent_noop_rollout':True}),flush=True)
                # Real apply commits but its response is lost: recovery may only
                # inspect DB and existing containers, never launch a second apply.
                third=queue(3,extra=True);original=migration_runner.MigrationHelper.invoke;applies=[]
                def lost(helper,cid,request,**kwargs):
                    result=original(helper,cid,request,**kwargs)
                    if request['operation']=='apply':applies.append(1);raise ValueError('Fixture lost committed reply')
                    return result
                with patch.object(migration_runner.MigrationHelper,'invoke',lost):assert not worker.process_managed_delivery(db,third)
                assert migration_jobs.load(db,third)['state']=='recovery-required'
                worker.recover_managed_deliveries(db)
                assert migration_jobs.load(db,third)['state']=='verified';assert managed_delivery.public(db,third)['status']=='failed';assert len(applies)==1
                fourth=queue(4,extra=True)
                # Simulate interruption after durable plan but before apply using
                # the actual helper plan, then let worker recover without mutation.
                import migration_coordinator
                original_begin=migration_jobs.begin_apply
                def interrupted(*args,**kwargs):raise ValueError('Fixture pre-apply interruption')
                with patch.object(migration_jobs,'begin_apply',interrupted):assert not worker.process_managed_delivery(db,fourth)
                assert migration_jobs.load(db,fourth)['state']=='planned'
                worker.recover_managed_deliveries(db);assert managed_delivery.public(db,fourth)['status']=='failed'
                assert admin.execute('SELECT * FROM business_sentinel').fetchall()==[(1,'preserved')]
                assert admin.execute('SELECT name FROM items').fetchall()==[('business-row',)]
                assert json.loads(command([docker,'inspect',production_id]))[0]['State']['Running']
                print(json.dumps({'git_transport_simulated':True,'actual_docker_build':True,'helper_image':HELPER,'uncertain_commit_reconciled_without_reapply':True,'preapply_recovery_no_mutation':True,'production_peer_preserved':True,'business_sentinels_preserved':True}),flush=True)
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
            for net in networks:command([docker,'network','rm',net])
            for volume in volumes:command([docker,'volume','rm',volume])
            for image in images:
                if image not in (base,pgimage,HELPER):command([docker,'image','rm','-f',image])
            if db:db.close()
            assert peers()==baseline,'Peer identity/running state changed'
            assert not command([docker,'ps','-aq','--filter','label=net.shiftcodes.migration-fixture='+nonce])
            print(json.dumps({'exact_cleanup':True,'peers_preserved':True}),flush=True)

if __name__=='__main__':main()
