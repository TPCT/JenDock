"""Real local Docker acceptance for managed environment ingress.

Uses disposable synthetic services in the actual deploy-monitor Compose project.
Never calls a deployed monitor or provider; removes only recorded fixture IDs.
"""
import http.client
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live_adoption import command, inspect, identity

PROJECT = 'deploy-monitor'
GATEWAY_IMAGE = 'nginxinc/nginx-unprivileged@sha256:15c994d10d6d78658721c3bcafff14cb281fba2a4bdf9d5ba92c416a472516e3'
APP_IMAGE = 'python:3.13-slim'


def peer_snapshot(docker):
    ids = command([docker, 'ps', '--no-trunc', '-aq', '--filter', 'label=com.docker.compose.project='+PROJECT]).split()
    result = {}
    for identifier in ids:
        row = inspect(docker, identifier)
        value = identity(row)
        value['Mounts'] = sorted(value['Mounts'], key=lambda item:(item['Type'],item['Destination'],item.get('Source','')))
        result[identifier] = value
    return result


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def request(port, hostname, path='/'):
    client = http.client.HTTPConnection('127.0.0.1',port,timeout=3)
    try:
        client.request('GET',path,headers={'Host':hostname})
        result=client.getresponse()
        return result.status,result.read(4096).decode()
    finally:
        client.close()


def wait_body(port, hostname, expected, timeout=20):
    deadline=time.monotonic()+timeout
    last=None
    while time.monotonic()<deadline:
        try:
            last=request(port,hostname)
            if last==(200,expected):return last
        except (OSError,http.client.HTTPException) as error:
            last=type(error).__name__
        time.sleep(.25)
    raise AssertionError(f'Expected {expected!r} via {hostname}; observed {last!r}')


def app_command(body):
    source=("from http.server import BaseHTTPRequestHandler,HTTPServer; "
            "H=type('H',(BaseHTTPRequestHandler,),{'do_GET':lambda s:(s.send_response(200),s.end_headers(),s.wfile.write("+repr(body.encode())+"))}); "
            "HTTPServer(('0.0.0.0',8080),H).serve_forever()")
    return ['python','-c',source]


def compose_service(docker,compose,path,service,containers):
    command([compose,'-p',PROJECT,'-f',str(path),'up','-d','--no-build','--no-deps',service])
    identifier=command([compose,'-p',PROJECT,'-f',str(path),'ps','-q',service]).strip()
    containers.add(identifier)
    return inspect(docker,identifier)


def create_application(db,docker,compose,root,environment,body,image,containers,networks,name="ingress-fixture"):
    import application_operations as operations
    import job_target
    import managed_services
    import registry
    import resource_policy
    import service_provisioning
    import build_profiles
    profile={'revision':2,'image_name':PROJECT+'-'+environment,'command':app_command(body),
             'healthcheck':['python','-c',"import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8080/',timeout=2).status==200"],
             'user':'65534:65534','http_endpoint':{'port':8080,'health_path':'/'}}
    recipe=build_profiles._recipe(PROJECT,environment,{'dockerfile':'Dockerfile','context':'.','target':None,
        'command':profile['command'],'healthcheck':profile['healthcheck'],'user':profile['user'],
        'test_command':['python','-c','import http.server,urllib.request'], 'http_endpoint':profile['http_endpoint']})
    now=time.time()
    job=db.execute('INSERT INTO jobs(delivery,project,sha,created,updated,status,branch,mode,actor) VALUES(?,?,?,?,?,?,?,?,?)',
        ('fixture-'+environment+'-'+name,PROJECT,'a'*40,now,now,'validated','main','validate','fixture')).lastrowid
    target=job_target.capture(PROJECT,environment,'TPCT/deploy-monitor',12,'main','a'*40,'validate',1,recipe)
    job_target.store(db,job,target)
    artifact=managed_services.publish_artifact(db,job,'application-v1',image)
    db.execute('INSERT INTO telemetry VALUES(?,?)',(time.time(),json.dumps({'memory_total':8*1024**3,'memory_used':0,'cpu_count':8})))
    principal=operations.Principal('fixture-owner','console',frozenset({'services:create','services:start','services:read','operations:read'}))
    values={'name':name,'artifact_id':artifact['id'],'template_id':'application-v1','template_revision':2,
            'environment_revision':registry.get_environment(db,PROJECT,environment)['revision'],
            'policy_revision':resource_policy.read(db,PROJECT)['revision'],'resources':{'memory_mib':64,'cpu':.25},'desired_state':'running'}
    accepted=operations.create_service(db,principal,operations.service_input(PROJECT,environment,values),str(uuid.uuid4()))
    claim=managed_services.claim_next(db,'fixture-worker',lease_seconds=120)
    assert claim['operation_id']==accepted['operation']['operation_id']
    binding=service_provisioning.provision(db,claim['operation_id'],claim['definition'],root=root/'managed',docker=docker,compose=compose,
        authorize=lambda:managed_services.renew_claim(db,claim['operation_id'],'fixture-worker',lease_seconds=120))
    containers.add(binding['container_id']);networks.update(binding['network_ids'])
    saved=managed_services.finish(db,claim['operation_id'],'fixture-worker',binding,'running')
    row=inspect(docker,binding['container_id'])
    assert row['State']['Running'] and not row['HostConfig']['PortBindings']
    assert len(row['NetworkSettings']['Networks'])==1
    return saved


def deliver_application(db,docker,compose,root,service,image,body,containers,*,recover=False,unhealthy=False):
    """Use normal durable delivery/CAS APIs; route tables are never written here."""
    import artifact_publication
    import build_profiles
    import job_target
    import managed_delivery
    import managed_rollout
    import managed_services
    import registry
    selection=managed_delivery.capture(db,PROJECT,'development',[service['id']])
    recipe=build_profiles._recipe(PROJECT,'development',{'dockerfile':'Dockerfile','context':'.','target':None,
        'command':app_command(body),'healthcheck':['python','-c',"import urllib.request;assert urllib.request.urlopen('http://127.0.0.1:8080/',timeout=2).status==200"],
        'user':'65534:65534','test_command':['python','-c','import http.server,urllib.request'],
        'http_endpoint':{'port':8080,'health_path':'/'}})
    if unhealthy:recipe['service_profiles']['application-v1']['healthcheck']=['python','-c','raise SystemExit(1)']
    recipe.update(schema_kind='managed-build',managed_target=selection)
    now=time.time();sha=uuid.uuid4().hex+'12345678'
    job=db.execute('INSERT INTO jobs(delivery,project,sha,created,updated,status,branch,mode,actor) VALUES(?,?,?,?,?,?,?,?,?)',
        (str(uuid.uuid4()),PROJECT,sha,now,now,'testing','main','deploy','fixture')).lastrowid
    target=job_target.capture(PROJECT,'development','TPCT/deploy-monitor',12,'main',sha,'deploy',1,recipe,
        project_revision=registry.get_project(db,PROJECT)['revision'],environment_revision=selection['environment_revision'])
    job_target.store(db,job,target)
    # Real isolated fixture validation precedes trusted worker publication.
    command([docker,'run','--rm','--network','none','--read-only','--user','65534:65534',image,
             'python','-c','import http.server,urllib.request'])
    artifact_publication.record(db,job,recipe,{PROJECT+'-development':image},status='deploying',detail='Synthetic source verified for actual runtime acceptance.')
    assert artifact_publication.process(db,lambda reference:json.loads(command([docker,'image','inspect',reference]))[0]['Id'],job)
    managed_delivery.prepare(db,job)
    lease='fixture-delivery'
    plan=managed_delivery.claim(db,job,lease,lease_seconds=120)
    assert plan and len(plan['services'])==1
    item=plan['services'][0];operation=str(job)+'-'+service['id']
    try:
        binding=managed_rollout.rollout(db,operation,item,docker=docker,compose=compose,
            authorize=lambda:managed_delivery.renew_claim(db,job,lease,lease_seconds=120),health_timeout=35)
        assert not unhealthy,'Deliberately unhealthy image must not complete'
    except managed_rollout.RolloutError as error:
        assert unhealthy and error.restored_binding is not None,str(error)
        binding=error.restored_binding
        assert binding['container_id']!=service['binding']['container_id']
        assert binding['definition_digest']==service['binding']['definition_digest']

    containers.discard(service['binding']['container_id']);containers.add(binding['container_id'])
    if unhealthy:
        managed_delivery.rollback_service(db,job,lease,service['id'],binding)
        result=managed_delivery.fail(db,job,lease,recovery_required=False)
    elif recover:
        # Simulate the supported failure boundary after Docker succeeded and
        # its durable journal exists but before application/route metadata CAS.
        managed_delivery.fail(db,job,lease,recovery_required=True)
        recovery=managed_delivery.claim_recovery(db,job,'fixture-recovery',lease_seconds=120)
        assert recovery
        item=recovery['services'][0]
        outcome=managed_rollout.reconcile(db,operation,item,docker=docker,
            authorize=lambda:managed_delivery.renew_recovery(db,job,'fixture-recovery',lease_seconds=120))
        assert outcome['outcome']=='verified-new',outcome
        result=managed_delivery.finish_recovery(db,job,'fixture-recovery',{service['id']:{'outcome':'new','binding':outcome['binding']}})
    else:
        result=managed_delivery.complete_service(db,job,lease,service['id'],binding)
    assert result['status']==('failed' if unhealthy else 'succeeded'),result
    return managed_services.get(db,service['id']),job


def main():
    import ingress_ports
    import ingress_capacity
    import ingress_runtime
    import registry
    import resource_policy
    from store import connect
    docker=shutil.which('docker')
    if not docker:raise RuntimeError('Docker required')
    image=json.loads(command([docker,'image','inspect',APP_IMAGE]))[0]['Id']
    gateway_image=json.loads(command([docker,'image','inspect',GATEWAY_IMAGE]))[0]
    version=subprocess.run([docker,'run','--rm','--network','none','--entrypoint','nginx',GATEWAY_IMAGE,'-v'],capture_output=True,text=True,check=True,timeout=30).stderr
    compose=next(item['Path'] for item in json.loads(command([docker,'info','--format','{{json .ClientInfo.Plugins}}'])) if item['Name']=='compose')
    for env in registry.ENVIRONMENTS:
        for network in (PROJECT+'-'+env+'-managed',PROJECT+'-'+env+'-ingress-transport'):
            if command([docker,'network','ls','--no-trunc','-q','--filter','name=^'+network+'$']).strip():
                raise RuntimeError('Existing environment network is not disposable: '+network)
    baseline=peer_snapshot(docker)
    runtime=ROOT/'.runtime';runtime.mkdir(exist_ok=True)
    report={'project':PROJECT,'gateway_image':gateway_image['Id'],'gateway_version':version.strip(),'scenarios':{}}
    containers=set();networks=set();gateway_operations=[]
    with tempfile.TemporaryDirectory(prefix='ingress-runtime-',dir=runtime) as temporary:
        root=Path(temporary);(root/'managed').mkdir()
        db=connect(root/'state.sqlite')
        try:
            registry.import_legacy(db,{},[])
            registry.create_project(db,PROJECT,{'repository':'TPCT/deploy-monitor','repository_id':12})
            resource_policy.save(db,PROJECT,0,{'budget':{'memory_mib':512,'cpu':2},'environments':{
                env:{'budget':{'memory_mib':256,'cpu':1},'services':{}} for env in registry.ENVIRONMENTS}})
            apps={};gateways={};definitions={};ports={};same_env_peer=None
            for env in registry.ENVIRONMENTS:
                body=PROJECT+':'+env+':v1'
                apps[env]=create_application(db,docker,compose,root,env,body,image,containers,networks)
                routes=[{'hostname':env+'.fixture.test','service_id':apps[env]['id']}]
                if env=='development':
                    same_env_peer=create_application(db,docker,compose,root,env,PROJECT+':development:peer',image,containers,networks,name='ingress-peer')
                    routes.append({'hostname':'peer.development.fixture.test','service_id':same_env_peer['id']})
                op=str(uuid.uuid4());gateway_operations.append(op);port=free_port();ports[env]=port
                ingress_ports.reserve(db,PROJECT,env,op,first=port,last=port,occupied_ports=set())
                ingress_capacity.reserve(db,PROJECT,env,op,{'memory_mib':64,'cpu':.25},resource_policy.read(db,PROJECT)['revision'])
                definition=ingress_runtime.prepare_gateway(db,PROJECT,env,op,routes=routes,
                    image=GATEWAY_IMAGE,resources={'memory_mib':64,'cpu':.25},docker=docker)
                definitions[env]=definition
                binding=ingress_runtime.apply_gateway(db,definition,root=root/'managed',docker=docker,compose=compose,authorize=lambda:None)
                gateways[env]=binding
                containers.add(binding['container_id'])
                gateway=inspect(docker,binding['container_id'])
                networks.update(value['NetworkID'] for value in gateway['NetworkSettings']['Networks'].values())
                try:wait_body(port,env+'.fixture.test',body)
                except Exception:
                    diagnostic={'container':inspect(docker,binding['container_id']),'logs':subprocess.run([docker,'logs',binding['container_id']],capture_output=True,text=True,timeout=10).stderr}
                    (runtime/'ingress-runtime-failure.json').write_text(json.dumps(diagnostic,indent=2))
                    raise
                assert request(port,'unknown.fixture.test')[0] in (400,404,421)
                assert request(port,('production' if env=='development' else 'development')+'.fixture.test')[0] in (400,404,421)
                assert gateway['HostConfig']['Memory']==64*1048576 and gateway['HostConfig']['NanoCpus']==250000000
                assert len(gateway['NetworkSettings']['Networks'])==2
                assert all(item['HostIp']=='127.0.0.1' for entries in gateway['HostConfig']['PortBindings'].values() for item in entries)
                replay=ingress_runtime.apply_gateway(db,definition,root=root/'managed',docker=docker,compose=compose,authorize=lambda:None)
                assert replay['container_id']==binding['container_id']
                report['scenarios'][env]={'body':body,'port':port,'container_id':binding['container_id'],'application_id':apps[env]['binding']['container_id'],
                    'unknown_host_rejected':True,'resources_verified':True,'replay_same_container':True}
            report['simultaneous_environments']=True
            prod=inspect(docker,apps['production']['binding']['container_id'])
            prod_ip=next(iter(prod['NetworkSettings']['Networks'].values()))['IPAddress']
            command([docker,'exec',apps['development']['binding']['container_id'],'python','-c',
                "import socket;s=socket.socket();s.settimeout(2);assert s.connect_ex((%r,8080))!=0" % prod_ip])
            report['cross_environment_application_network_denied']=True
            selected_gateway=gateways['development']['container_id']
            command([docker,'update','--memory','80m','--memory-swap','80m',selected_gateway])
            try:
                try:ingress_runtime.verify_gateway(db,definitions['development'],root=root/'managed',docker=docker,compose=compose,authorize=lambda:None)
                except ValueError:pass
                else:raise AssertionError('Gateway resource drift must fail verification')
            finally:
                command([docker,'update','--memory','64m','--memory-swap','64m',selected_gateway])
            ingress_runtime.verify_gateway(db,definitions['development'],root=root/'managed',docker=docker,compose=compose,authorize=lambda:None)
            report['resource_drift_rejected']=True
            import ingress_bindings
            import managed_services
            same_env_peer_before=identity(inspect(docker,same_env_peer['binding']['container_id']))
            same_env_route_before=ingress_bindings.capture(db,PROJECT,'development',same_env_peer['id'])
            production_route_before=ingress_bindings.capture(db,PROJECT,'production',apps['production']['id'])
            report['durable_route_transitions']=[]
            original_generation=ingress_bindings.capture(db,PROJECT,'development',apps['development']['id'])
            for version,recover,unhealthy in [('v2',False,False),('v3',True,False),('bad-health',False,True)]:
                previous=ingress_bindings.capture(db,PROJECT,'development',apps['development']['id'])
                apps['development'],job=deliver_application(db,docker,compose,root,apps['development'],image,PROJECT+':development:'+version,containers,recover=recover,unhealthy=unhealthy)
                current=ingress_bindings.capture(db,PROJECT,'development',apps['development']['id'])
                assert current['generation']==previous['generation']+1
                assert current['backend']['container_id']==apps['development']['binding']['container_id']
                assert current['backend']['definition_id']==apps['development']['definition_id']
                if unhealthy:
                    assert current['backend']['definition_id']==previous['backend']['definition_id']
                    assert current['backend']['container_id']!=previous['backend']['container_id']
                verified=ingress_runtime.verify_gateway(db,definitions['development'],root=root/'managed',docker=docker,compose=compose,authorize=lambda:None)
                replay=ingress_runtime.apply_gateway(db,definitions['development'],root=root/'managed',docker=docker,compose=compose,authorize=lambda:None)
                assert verified['container_id']==replay['container_id']==gateways['development']['container_id']
                wait_body(ports['development'],'development.fixture.test',PROJECT+':development:'+('v3' if unhealthy else version))
                wait_body(ports['development'],'peer.development.fixture.test',PROJECT+':development:peer')
                wait_body(ports['production'],'production.fixture.test',PROJECT+':production:v1')
                stale_target=current['backend'] if current['generation']>original_generation['generation']+1 else original_generation['backend']
                with registry.transaction(db):
                    try:
                        ingress_bindings.advance(db,original_generation,**{key:stale_target[key] for key in ('definition_id','definition_digest','container_id','alias','http_endpoint')})
                    except ValueError:pass
                    else:raise AssertionError('Stale route generation must not restore old backend pins')
                assert ingress_bindings.capture(db,PROJECT,'development',apps['development']['id'])==current
                assert ingress_bindings.capture(db,PROJECT,'development',same_env_peer['id'])==same_env_route_before
                assert ingress_bindings.capture(db,PROJECT,'production',apps['production']['id'])==production_route_before
                assert identity(inspect(docker,same_env_peer['binding']['container_id']))==same_env_peer_before
                report['durable_route_transitions'].append({'kind':'verified-health-rollback' if unhealthy else 'pre-CAS-recovery' if recover else 'normal-completion',
                    'job_id':job,'old_generation':previous['generation'],'generation':current['generation'],
                    'container_id':current['backend']['container_id'],'restored_original_definition':unhealthy,
                    'strong_verify_passed':True,'gateway_replay_preserved_id':True,'stale_generation_rejected':True,'sibling_route_preserved':True})
            production_before=identity(inspect(docker,apps['production']['binding']['container_id']))
            production_gateway_before=identity(inspect(docker,gateways['production']['container_id']))
            import managed_services
            application=apps['development'];old=application['binding']
            old_row=inspect(docker,old['container_id'])
            old_ip=old_row['NetworkSettings']['Networks'][old['network_name']]['IPAddress']
            command([docker,'rm','-f',old['container_id']]);containers.discard(old['container_id'])
            blocker=command([docker,'run','-d','--network',old['network_name'],'--ip',old_ip,'--label','fixture.ingress='+str(root),
                             image,'python','-c','import time;time.sleep(120)']).strip();containers.add(blocker)
            path=Path(old['compose_files'][0])
            new_row=compose_service(docker,compose,path,old['compose_service'],containers)
            new_ip=new_row['NetworkSettings']['Networks'][old['network_name']]['IPAddress']
            assert new_ip!=old_ip,'Fixture must force application IP change'
            import service_provisioning
            service_provisioning._verify(new_row,application['definition'],application['operation_id'],old['image_id'],old['network_ids'][0],old['container_name'],path,application['definition']['resources'])
            refreshed=dict(old,container_id=new_row['Id'],observed=time.time())
            managed_services.refresh_binding(db,application['id'],old['container_id'],refreshed)
            command([docker,'rm','-f',blocker]);containers.discard(blocker)
            wait_body(ports['development'],'development.fixture.test',PROJECT+':development:v3',timeout=30)
            assert inspect(docker,gateways['development']['container_id'])['Id']==gateways['development']['container_id']
            try:
                ingress_runtime.verify_gateway(db,definitions['development'],root=root/'managed',docker=docker,compose=compose,authorize=lambda:None)
            except ValueError:pass
            else:raise AssertionError('Old route pin must reject replaced backend identity')
            report['dynamic_dns_replacement']={'old_ip':old_ip,'new_ip':new_ip,'gateway_not_recreated':True,'stale_pin_rejected':True}
            command([docker,'stop','-t','2',new_row['Id']])
            stopped=inspect(docker,new_row['Id']);assert not stopped['State']['Running']
            try:
                ingress_runtime.verify_gateway(db,definitions['development'],root=root/'managed',docker=docker,compose=compose,authorize=lambda:None)
            except ValueError:
                pass
            else:
                raise AssertionError('Stopped backend must fail gateway readiness')
            status,body=request(ports['development'],'development.fixture.test')
            assert status in (502,503,504),status
            assert not inspect(docker,new_row['Id'])['State']['Running']
            report['stopped_backend_no_wake']=True
            wait_body(ports['production'],'production.fixture.test',PROJECT+':production:v1')
            assert identity(inspect(docker,apps['production']['binding']['container_id']))==production_before
            assert identity(inspect(docker,gateways['production']['container_id']))==production_gateway_before
            report['sibling_environment_unchanged']=True
        finally:
            # Discovery and diagnostics may fail; teardown still runs for every
            # exact ID already captured. Never compose down or prune the product.
            try:
                for operation in gateway_operations:
                    label='net.shiftcodes.ingress.operation='+operation
                    containers.update(command([docker,'ps','--no-trunc','-aq','--filter','label='+label]).split())
                    networks.update(command([docker,'network','ls','--no-trunc','-q','--filter','label='+label]).split())
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='managed_services'").fetchone():
                    for saved in db.execute('SELECT operation_id FROM managed_services'):
                        containers.update(command([docker,'ps','--no-trunc','-aq','--filter','label=net.shiftcodes.managed.operation='+saved['operation_id']]).split())
                for env in registry.ENVIRONMENTS:
                    networks.update(command([docker,'network','ls','--no-trunc','-q','--filter','name=^'+PROJECT+'-'+env+'-managed$',
                        '--filter','label=net.shiftcodes.managed.project='+PROJECT,'--filter','label=net.shiftcodes.managed.environment='+env]).split())
                if sys.exc_info()[0] is not None:
                    try:
                        evidence=[]
                        for identifier in containers:
                            inspected=subprocess.run([docker,'inspect',identifier],capture_output=True,text=True,timeout=10)
                            if inspected.returncode==0:
                                evidence.append({'container':json.loads(inspected.stdout)[0],'logs':subprocess.run([docker,'logs',identifier],capture_output=True,text=True,timeout=10).stderr})
                        (runtime/'ingress-runtime-failure.json').write_text(json.dumps(evidence,indent=2))
                    except Exception:
                        pass  # Diagnostic output must never prevent exact teardown.
            finally:
                cleanup_errors=[]
                try:
                    for kind,identifiers in [('container',containers),('network',networks)]:
                        for identifier in identifiers:
                            try:
                                args=['rm','-f',identifier] if kind=='container' else ['network','rm',identifier]
                                command([docker,*args])
                            except Exception:
                                cleanup_errors.append((kind,identifier))
                finally:
                    db.close()
                assert peer_snapshot(docker)==baseline,'Existing project peers changed during failed or successful execution'
                if cleanup_errors:raise RuntimeError('Exact fixture cleanup failed: '+repr(cleanup_errors))
        assert peer_snapshot(docker)==baseline,'Existing project peers changed'
        report['peers_unchanged']=True
        report['cleanup_verified']=not any(command([docker,'ps','--no-trunc','-aq','--filter','id='+identifier]).strip() for identifier in containers)
    path=runtime/'ingress-runtime-acceptance.json';path.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
