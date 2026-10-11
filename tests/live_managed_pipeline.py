"""Real Docker managed replacement, failed health rollback and peer preservation."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import application_operations as operations
import control
import job_target
import managed_services
import managed_rollout
import managed_delivery
import build_profiles
import delivery_target
import worker
from store import enqueue
import registry
import resource_policy
import service_provisioning
import workspace
from store import connect, snapshot
from live_adoption import command, inspect, peers as raw_peers

def peers(docker):
    result = raw_peers(docker)
    for row in result.values():
        row["Mounts"] = sorted(row["Mounts"], key=lambda mount: (mount["Type"], mount["Destination"], mount.get("Source", "")))
    return result

PROJECT = 'deploy-monitor'
APP = "from http.server import BaseHTTPRequestHandler,HTTPServer; H=type('H',(BaseHTTPRequestHandler,),{'do_GET':lambda s:(s.send_response(200),s.end_headers(),s.wfile.write(b'fixture-ok'))}); HTTPServer(('0.0.0.0',8080),H).serve_forever()"
HEALTH = "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8080',timeout=2).read()==b'fixture-ok'"


def main(recovery=False):
    docker = shutil.which('docker')
    command([docker, 'image', 'inspect', 'python:3.13-slim'])
    compose = next(item['Path'] for item in json.loads(command([docker, 'info', '--format', '{{json .ClientInfo.Plugins}}'])) if item['Name'] == 'compose')
    for environment in registry.ENVIRONMENTS:
        if command([docker, 'network', 'ls', '-q', '--filter', 'name=^'+PROJECT+'-'+environment+'-managed$']).strip():
            raise RuntimeError('Existing managed environment network must not be reused by this fixture')
    baseline = peers(docker)
    runtime = ROOT / '.runtime'; runtime.mkdir(exist_ok=True)
    report = {'project': PROJECT, 'scenarios': []}
    containers, networks, built = set(), set(), None
    rollout_images = []
    with tempfile.TemporaryDirectory(prefix='managed-pipeline-', dir=runtime) as directory:
        root = Path(directory); managed = root / 'managed'; managed.mkdir()
        (root / 'Dockerfile').write_text('FROM python:3.13-slim\nWORKDIR /tmp\nLABEL fixture.rollout="'+str(uuid.uuid4())+'"\n')
        policy = root / 'projects.json'; policy.write_text('{}')
        db = connect(root / 'state.sqlite')
        try:
            # A real isolated fixture build and runtime test establish this test
            # artifact; no product catalog entry is created outside this temp DB.
            built = command([docker, 'build', '--pull=false', '--network=none', '-q', str(root)]).strip()
            command([docker, 'run', '--rm', '--network=none', '--read-only', '--cap-drop=ALL',
                     '--security-opt=no-new-privileges', '--user=65534:65534', built,
                     'python', '-c', 'import http.server,urllib.request; assert 2+2==4'])
            registry.import_legacy(db, {}, [])
            registry.create_project(db, PROJECT, {'repository': 'TPCT/deploy-monitor', 'repository_id': 12})
            profile = {'revision': 1, 'image_name': 'fixture-app', 'command': ['python', '-c', APP],
                       'healthcheck': ['python', '-c', HEALTH], 'user': '65534:65534'}
            recipe = {'compose_project': PROJECT, 'compose_files': [str(root/'source.yml')], 'services': ['fixture'],
                      'images': [{'name': 'fixture-app'}], 'service_profiles': {'application-v1': profile}}
            job = db.execute("INSERT INTO jobs(delivery,project,sha,created,updated,status,branch,mode,actor) VALUES(?,?,?,?,?,?,?,?,?)",
                             ('fixture-build', PROJECT, 'a'*40, time.time(), time.time(), 'validated', 'main', 'validate', 'fixture')).lastrowid
            target = job_target.capture(PROJECT, 'development', 'TPCT/deploy-monitor', 12, 'main', 'a'*40, 'validate', 1, recipe)
            job_target.initialize(db); job_target.store(db, job, target)
            artifact = managed_services.publish_artifact(db, job, 'application-v1', built)
            resource_policy.save(db, PROJECT, 0, {'budget': {'memory_mib': 512, 'cpu': 2}, 'environments': {
                env: {'budget': {'memory_mib': 256, 'cpu': 1}, 'services': {}} for env in registry.ENVIRONMENTS}})
            principal = operations.Principal('fixture-owner', 'console', frozenset({'services:create','services:start','services:read','operations:read'}))
            for environment, desired, name in [('development', 'running', 'fixture-running'), ('production', 'running', 'fixture-running'), ('development', 'stopped', 'fixture-stopped')]:
                db.execute('INSERT INTO telemetry VALUES(?,?)', (time.time(), json.dumps({'memory_total': 8*1024**3, 'memory_used': 0, 'cpu_count': 8})))
                values = {'name': name, 'artifact_id': artifact['id'], 'template_id': 'application-v1', 'template_revision': 1,
                          'environment_revision': registry.get_environment(db, PROJECT, environment)['revision'],
                          'policy_revision': resource_policy.read(db, PROJECT)['revision'],
                          'resources': {'memory_mib': 64, 'cpu': 0.25}, 'desired_state': desired}
                request_id = str(uuid.uuid4())
                input = operations.service_input(PROJECT, environment, values)
                accepted = operations.create_service(db, principal, input, request_id)
                claim = managed_services.claim_next(db, 'fixture-worker', lease_seconds=120)
                assert claim['operation_id'] == accepted['operation']['operation_id']
                authorize = lambda: managed_services.renew_claim(db, claim['operation_id'], 'fixture-worker', lease_seconds=120)
                binding = service_provisioning.provision(db, claim['operation_id'], claim['definition'], root=managed,
                    docker=docker, compose=compose, authorize=authorize)
                containers.add(binding['container_id']); networks.update(binding['network_ids'])
                replay = service_provisioning.provision(db, claim['operation_id'], claim['definition'], root=managed,
                    docker=docker, compose=compose, authorize=authorize)
                assert replay['container_id'] == binding['container_id']
                saved = managed_services.finish(db, claim['operation_id'], 'fixture-worker', binding, desired)
                retry = operations.create_service(db, principal, input, request_id)
                assert retry['operation']['operation_id'] == claim['operation_id']
                assert saved['binding']['container_id'] == binding['container_id']
                actual = inspect(docker, binding['container_id'])
                assert actual['State']['Running'] == (desired == 'running')
                assert not actual['Mounts'] and not actual['HostConfig']['PortBindings']
                if desired == 'running':
                    limits = command([docker, 'exec', binding['container_id'], 'python', '-c', "from pathlib import Path;print(Path('/sys/fs/cgroup/memory.max').read_text().strip());print(Path('/sys/fs/cgroup/memory.swap.max').read_text().strip());print(Path('/sys/fs/cgroup/cpu.max').read_text().strip())"]).splitlines()
                    assert limits == ['67108864', '0', '25000 100000'], limits
                report['scenarios'].append({'environment':environment,'desired':desired,'container_id':binding['container_id'],'replay_same_id':True})
                if environment == 'production':
                    first = report['scenarios'][0]
                    first_name = PROJECT + '-development-fixture-running-1'
                    with patch.object(control,'DATABASE',str(root/'state.sqlite')),patch.object(control,'CONFIG',str(policy)),patch.object(control,'ROOT',managed),patch.object(control,'DOCKER',docker),patch.object(control,'COMPOSE',compose),patch.object(control,'JOURNAL',root/'control-journal.json'):
                        details = control.execute({'action':'resource-details','project':PROJECT,'container':first_name})
                        changed = control.execute({'action':'container-limits-save','project':PROJECT,'container':first_name,
                            'container_id':details['container_id'],'revision':details['revision'],
                            'policy_revision':details['resource_policy']['revision'],'limits':{'memory_mib':80,'cpu':0.3},
                            'request_id':str(uuid.uuid4()),'actor':'fixture-owner'})
                        assert changed['status']=='succeeded', changed
                    replaced = inspect(docker,first_name)
                    assert replaced['Id'] != first['container_id']
                    containers.discard(first['container_id']); containers.add(replaced['Id'])
                    first['container_id'] = replaced['Id']
                    first['limits_recreated'] = True

            running = [inspect(docker, row['container_id']) for row in report['scenarios'] if row['desired']=='running']
            assert len(running) == 2 and all(row['State']['Running'] for row in running)
            prod_ip = next(iter(running[1]['NetworkSettings']['Networks'].values()))['IPAddress']
            command([docker, 'exec', running[0]['Id'], 'python', '-c', "import socket; s=socket.socket();s.settimeout(2);assert s.connect_ex((%r,8080))!=0" % prod_ip])
            report['cross_environment_connection_denied'] = True
            first = managed_services.list_services(db, PROJECT, 'development')
            updated = next(row for row in first if row['name']=='fixture-running')
            assert updated['binding']['container_id'] == running[0]['Id']
            assert running[0]['HostConfig']['Memory'] == 80*1048576 and running[0]['HostConfig']['NanoCpus'] == 300000000
            report['add_service_after_limits_recreation'] = 'passed'
            observed = []
            for identifier in containers:
                row = inspect(docker, identifier); labels = row['Config']['Labels']
                observed.append({'ID':identifier,'Names':row['Name'].lstrip('/'),'State':row['State']['Status'],
                    'ComposeProject':PROJECT,'ComposeService':labels['com.docker.compose.service'],
                    'ComposeConfigFiles':labels['com.docker.compose.project.config_files']})
            snapshot(db,'containers',observed); snapshot(db,'worker',{'state':'running'})
            for environment in registry.ENVIRONMENTS:
                view = workspace.read(db,PROJECT,environment)
                assert view['services'] and all(row['capabilities']['manage'] for row in view['services'])
            with patch.object(control,'DATABASE',str(root/'state.sqlite')),patch.object(control,'CONFIG',str(policy)),patch.object(control,'ROOT',managed),patch.object(control,'DOCKER',docker),patch.object(control,'COMPOSE',compose):
                for row in observed: assert control.inspect_target(PROJECT,row['Names'])[0]['Id']==row['ID']
            selected = next(row for row in managed_services.list_services(db, PROJECT, 'development') if row['name']=='fixture-running')
            old_binding = selected['binding']; old_definition_id=selected['definition_id']
            initial_policy=resource_policy.read(db,PROJECT)
            peer_ids=[row['binding']['container_id'] for env in registry.ENVIRONMENTS for row in managed_services.list_services(db,PROJECT,env) if row['id']!=selected['id']]
            peer_before={identifier:inspect(docker,identifier) for identifier in peer_ids}
            source=root/'repository'; source.mkdir(); work=root/'work';work.mkdir();state=root/'worker-state';state.mkdir()
            command(['git','init','--initial-branch=main',str(source)])
            (source/'Dockerfile').write_text('FROM python:3.13-slim\nCOPY marker /marker\n')
            (source/'marker').write_text(str(uuid.uuid4()))
            command(['git','-C',str(source),'add','Dockerfile','marker'])
            command(['git','-C',str(source),'-c','user.name=Local fixture','-c','user.email=fixture@invalid','commit','-m','Managed pipeline fixture'])
            sha=command(['git','-C',str(source),'rev-parse','HEAD']).strip()
            values={'dockerfile':'Dockerfile','context':'.','target':None,'test_command':['python','-c',"from pathlib import Path;assert Path('/marker').read_text()"],
                    'command':['python','-c',APP],'healthcheck':['python','-c',HEALTH],'user':'65534:65534'}
            current=build_profiles.get(db,PROJECT,'development')
            saved=build_profiles.save(db,PROJECT,'development',current['project_revision'],current['environment_revision'],current['profile_revision'],values)
            # Fresh actual ownership observations are required by the real public
            # target-capture path; no state or authorization helpers are patched.
            observed=[]
            for identifier in containers:
                row=inspect(docker,identifier);labels=row['Config']['Labels']
                observed.append({'ID':identifier,'Names':row['Name'].lstrip('/'),'State':row['State']['Status'],
                    'ComposeProject':PROJECT,'ComposeService':labels['com.docker.compose.service'],
                    'ComposeConfigFiles':labels['com.docker.compose.project.config_files']})
            snapshot(db,'containers',observed);snapshot(db,'worker',{'state':'running'})
            target=delivery_target.capture_registered_target(db,PROJECT,'main',sha,'deploy',environment='development',
                project_revision=saved['project_revision'],environment_revision=saved['environment_revision'],source_revision=saved['source_revision'])
            assert target['recipe']['schema_kind']=='managed-build'
            job_id,_=enqueue(db,'managed-pipeline-build',PROJECT,sha,True,'main','deploy','fixture',target_factory=lambda db:target)
            job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
            project=worker.project_for_job(db,job)
            real_run=worker.run
            def run(argv,**kwargs):
                if argv[:2]==['git','clone']:
                    argv=list(argv);argv[-2]=str(source)
                return real_run(argv,**kwargs)
            with patch.object(worker,'DOCKER',docker),patch.object(worker,'COMPOSE',compose),patch.object(worker,'ARTIFACT_WORK',work),patch.object(worker,'ARTIFACT_BUILD_NETWORK','none'),patch.object(worker,'STATE',state),patch.object(worker,'run',side_effect=run),patch.object(control,'JOURNAL',root/'pipeline-control.json'):
                worker.deploy(db,job,project,validate_only=False)
                built_job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
                assert built_job['status']=='deploying',built_job
                published=[managed_services.artifact_for_service(db,PROJECT,row['id']) for row in db.execute('SELECT id FROM service_artifacts WHERE job_id=?',(job_id,))]
                assert len(published)==1
                rollout_images.append(published[0]['image'])
                assert inspect(docker,old_binding['container_id'])['Id']==old_binding['container_id']
                if recovery:
                    class InterruptedProcess(BaseException): pass
                    with patch.object(managed_delivery, 'complete_service', side_effect=InterruptedProcess('crash before metadata CAS')):
                        try: worker.process_managed_delivery(db,job_id)
                        except InterruptedProcess: pass
                        else: raise AssertionError('Fixture did not interrupt at metadata CAS')
                    journal=json.loads(db.execute('SELECT document FROM managed_rollout_journals WHERE operation_id=?',(str(job_id)+'-'+selected['id'],)).fetchone()['document'])
                    assert journal['phase']=='delivery-verified',journal
                    candidate=journal['candidate_binding']
                    assert inspect(docker,candidate['container_id'])['State']['Health']['Status']=='healthy'
                    assert managed_services.get(db,selected['id'])['binding']['container_id']==old_binding['container_id']
                    db.execute('UPDATE managed_deliveries SET expires=0 WHERE job_id=?',(job_id,))
                    recovery_calls=[]
                    real_command=service_provisioning.command
                    def observe_command(argv, **kwargs):
                        recovery_calls.append(argv)
                        return real_command(argv, **kwargs)
                    with patch.object(service_provisioning,'command',side_effect=observe_command):
                        worker.recover_managed_deliveries(db)
                        if managed_delivery.public(db,job_id)['status']=='recovery-required':
                            db.execute('UPDATE managed_deliveries SET recovery_attempted=0 WHERE job_id=?',(job_id,))
                            worker.recover_managed_deliveries(db)
                    assert all(argv[1] in ('inspect','network') for argv in recovery_calls),recovery_calls
                    assert inspect(docker,candidate['container_id'])['Id']==candidate['container_id']
                    assert managed_services.get(db,selected['id'])['binding']['container_id']==candidate['container_id']
                    report['interrupted_delivery_recovery']={'phase':'delivery-verified','candidate_id':candidate['container_id'],'read_only_runtime_recovery':True,'metadata_cas_recovered':True}
                    succeeded=managed_delivery.public(db,job_id)['status']=='succeeded'
                else:
                    succeeded=worker.process_managed_delivery(db,job_id)
                final=dict(db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
                assert succeeded and final['status']=='succeeded',(final,managed_delivery.public(db,job_id))
            replacement=managed_services.get(db,selected['id'])
            result=replacement['binding'];containers.discard(old_binding['container_id']);containers.add(result['container_id'])
            assert result['container_id']!=old_binding['container_id'] and result['image_id']==published[0]['image']
            assert replacement['definition_id']!=old_definition_id
            assert replacement['definition']['artifact']['source_job']==job_id
            actual=inspect(docker,result['container_id'])
            assert actual['State']['Health']['Status']=='healthy'
            assert actual['HostConfig']['Memory']==80*1048576 and actual['HostConfig']['NanoCpus']==300000000
            assert resource_policy.read(db,PROJECT)==initial_policy
            for identifier,prior in peer_before.items():
                current=inspect(docker,identifier)
                for field in ('Id','Image','Config','HostConfig','Mounts','NetworkSettings'):assert current[field]==prior[field],(identifier,field)
                assert current['State']['Running']==prior['State']['Running']
            public=managed_delivery.public(db,job_id)
            report['pipeline']={'source_sha':sha,'job_id':job_id,'build_status':'deploying','final_status':final['status'],
                'old_container_id':old_binding['container_id'],'container_id':result['container_id'],'image_id':result['image_id'],
                'definition_cas_persisted':True,'effective_caps_preserved':True,'stopped_and_other_environment_peers_unchanged':True,'delivery':public}
            db.close(); db = connect(root/'state.sqlite')
            assert len(managed_services.list_services(db,PROJECT,'development')) == 2
            assert len(managed_services.list_services(db,PROJECT,'production')) == 1
            report['sqlite_reconnect_workspace_control'] = 'passed'
        finally:
            # Include outcomes whose response was interrupted, but only this
            # temporary manifest's exact Compose file label authorizes cleanup.
            for env in registry.ENVIRONMENTS:
                path = str(managed/PROJECT/env/'compose.json')
                identifiers = command([docker,'ps','-aq','--no-trunc','--filter','label=com.docker.compose.project.config_files='+path]).split()
                containers.update(identifiers)
            existing_ids=set(command([docker,'ps','-aq','--no-trunc']).split())
            for identifier in containers & existing_ids: command([docker,'rm','-f',identifier])
            for env in registry.ENVIRONMENTS:
                name = PROJECT+'-'+env+'-managed'
                identifiers = command([docker,'network','ls','-q','--filter','name=^'+name+'$']).split()
                for identifier in identifiers:
                    row=json.loads(command([docker,'network','inspect',identifier]))[0]
                    if row['Labels'].get(service_provisioning.PREFIX+'project')==PROJECT and row['Labels'].get(service_provisioning.PREFIX+'environment')==env:
                        command([docker,'network','rm',identifier])
            db.close()
            for image in rollout_images: command([docker,'image','rm',image])
            if built and built in command([docker,'image','ls','-aq','--no-trunc']).split(): command([docker,'image','rm',built])
    after_peers = peers(docker)
    if after_peers != baseline:
        changes = {name: [field for field in set(baseline.get(name, {})) | set(after_peers.get(name, {})) if baseline.get(name, {}).get(field) != after_peers.get(name, {}).get(field)] for name in set(baseline) | set(after_peers) if baseline.get(name) != after_peers.get(name)}
        print('Peer identity changes:', changes)
        raise AssertionError('Existing peers changed')
    report['existing_peers_unchanged']=True;report['cleanup']='passed'
    (runtime/('managed-pipeline-recovery-last.json' if recovery else 'managed-pipeline-last.json')).write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main(recovery='--recovery' in sys.argv)
