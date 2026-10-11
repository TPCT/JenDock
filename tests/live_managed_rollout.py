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


def main():
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
    with tempfile.TemporaryDirectory(prefix='managed-rollout-', dir=runtime) as directory:
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
                      'images': [{'name': 'fixture-app'}], 'service_profiles': {'fixture-http': profile}}
            job = db.execute("INSERT INTO jobs(delivery,project,sha,created,updated,status,branch,mode,actor) VALUES(?,?,?,?,?,?,?,?,?)",
                             ('fixture-build', PROJECT, 'a'*40, time.time(), time.time(), 'validated', 'main', 'validate', 'fixture')).lastrowid
            target = job_target.capture(PROJECT, 'development', 'TPCT/deploy-monitor', 12, 'main', 'a'*40, 'validate', 1, recipe)
            job_target.initialize(db); job_target.store(db, job, target)
            artifact = managed_services.publish_artifact(db, job, 'fixture-http', built)
            resource_policy.save(db, PROJECT, 0, {'budget': {'memory_mib': 512, 'cpu': 2}, 'environments': {
                env: {'budget': {'memory_mib': 256, 'cpu': 1}, 'services': {}} for env in registry.ENVIRONMENTS}})
            principal = operations.Principal('fixture-owner', 'console', frozenset({'services:create','services:start','services:read','operations:read'}))
            for environment, desired, name in [('development', 'running', 'fixture-running'), ('production', 'running', 'fixture-running'), ('development', 'stopped', 'fixture-stopped')]:
                db.execute('INSERT INTO telemetry VALUES(?,?)', (time.time(), json.dumps({'memory_total': 8*1024**3, 'memory_used': 0, 'cpu_count': 8})))
                values = {'name': name, 'artifact_id': artifact['id'], 'template_id': 'fixture-http', 'template_revision': 1,
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
            initial_policy = resource_policy.read(db, PROJECT)
            peer_ids = [row['binding']['container_id'] for env in registry.ENVIRONMENTS for row in managed_services.list_services(db, PROJECT, env) if row['id'] != selected['id']]
            peer_before = {identifier: inspect(docker, identifier) for identifier in peer_ids}
            peer_manifests = {env: (managed/PROJECT/env/'compose.json').read_bytes() for env in registry.ENVIRONMENTS if env != 'development'}
            for number, unhealthy in [(1, False), (2, True)]:
                (root/'Dockerfile').write_text('FROM python:3.13-slim\nWORKDIR /tmp\nLABEL fixture.rollout="'+str(uuid.uuid4())+'"\n')
                image = command([docker,'build','--pull=false','--network=none','-q',str(root)]).strip()
                rollout_images.append(image)
                next_recipe = json.loads(json.dumps(recipe))
                if unhealthy: next_recipe['service_profiles']['fixture-http']['healthcheck'] = ['python','-c','raise SystemExit(1)']
                build_job = db.execute("INSERT INTO jobs(delivery,project,sha,created,updated,status,branch,mode,actor) VALUES(?,?,?,?,?,?,?,?,?)",
                    ('rollout-'+str(number), PROJECT, str(number)*40, time.time(), time.time(), 'validated','main','validate','fixture')).lastrowid
                build_target = job_target.capture(PROJECT,'development','TPCT/deploy-monitor',12,'main',str(number)*40,'validate',1,next_recipe)
                job_target.store(db,build_job,build_target)
                new_artifact = managed_services.publish_artifact(db,build_job,'fixture-http',image)
                definition = dict(selected['definition'], artifact_id=new_artifact['id'], artifact=new_artifact)
                item = {'definition':selected['definition'],'binding':selected['binding'],'resources':{'memory_mib':80,'cpu':.3},
                        'artifact':new_artifact,'next_definition':definition,'operation_id':selected['operation_id']}
                old_id=selected['binding']['container_id']
                try:
                    result = managed_rollout.rollout(db,'rollout-'+str(number),item,docker=docker,compose=compose,authorize=lambda:None,health_timeout=8)
                    assert not unhealthy
                    next_id=str(uuid.uuid4())
                    db.execute('INSERT INTO service_definitions VALUES(?,?,?,?)',(next_id,registry.canonical(definition),managed_services.digest(definition),time.time()))
                    db.execute('UPDATE managed_services SET definition_id=? WHERE id=?',(next_id,selected['id']))
                except managed_rollout.RolloutError as error:
                    assert unhealthy and error.restored_binding is not None, str(error)
                    result=error.restored_binding
                    assert result['image_id']==selected['binding']['image_id']
                db.execute('UPDATE service_bindings SET document=?,updated=? WHERE service_id=?',(registry.canonical(result),time.time(),selected['id']))
                containers.discard(old_id); containers.add(result['container_id'])
                selected=managed_services.get(db,selected['id'])
                actual=inspect(docker,result['container_id'])
                assert actual['State']['Health']['Status']=='healthy'
                assert actual['HostConfig']['Memory']==80*1048576 and actual['HostConfig']['NanoCpus']==300000000
                assert resource_policy.read(db,PROJECT)==initial_policy
                for identifier in peer_ids:
                    current=inspect(docker,identifier); prior=peer_before[identifier]
                    for field in ('Id','Image','Config','HostConfig','Mounts','NetworkSettings'):
                        assert current[field]==prior[field], (identifier,field)
                    assert current['State']['Running']==prior['State']['Running']
                for env, content in peer_manifests.items(): assert (managed/PROJECT/env/'compose.json').read_bytes()==content
                report['scenarios'].append({'rollout':number,'health_failure':unhealthy,'old_container_id':old_id,'container_id':result['container_id'],
                    'image_id':result['image_id'],'effective_caps_preserved':True,'stopped_and_other_environment_peers_unchanged':True})
            report['real_rollout_and_rollback']='passed'
            db.close(); db = connect(root/'state.sqlite')
            assert len(managed_services.list_services(db,PROJECT,'development')) == 2
            assert len(managed_services.list_services(db,PROJECT,'production')) == 1
            report['sqlite_reconnect_workspace_control'] = 'passed'
        finally:
            # Include outcomes whose response was interrupted, but only this
            # temporary manifest's exact Compose file label authorizes cleanup.
            for env in registry.ENVIRONMENTS:
                path = str(managed/PROJECT/env/'compose.json')
                identifiers = command([docker,'ps','-aq','--filter','label=com.docker.compose.project.config_files='+path]).split()
                containers.update(identifiers)
            for identifier in containers: command([docker,'rm','-f',identifier])
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
    (runtime/'managed-rollout-last.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
