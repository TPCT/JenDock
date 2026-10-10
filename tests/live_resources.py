"""Real local Docker policy and direct container limits check; never calls a deployed monitor.

Run: python3 tests/live_resources.py
Uses one disposable service in the existing deploy-monitor Compose project,
network none, no ports or volumes, and an already cached python:3.13-slim image.
Only control module configuration constants are overridden; Docker inspection,
Compose resolution/recreation, SQLite policies, and verification are real.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))
import control
import resource_policy
from store import connect

PROJECT = 'deploy-monitor'
SERVICE = 'resource-check'
CONTAINER = f'{PROJECT}-{SERVICE}-1'
IMAGE = 'python:3.13-slim'


def run(*args):
    process = subprocess.run(args, text=True, capture_output=True, timeout=180,
                             env={**os.environ, 'COMPOSE_IGNORE_ORPHANS': 'true'})
    if process.returncode:
        raise RuntimeError(f'Local test command failed: {args[0]} ({process.returncode})\n{process.stderr}')
    return process.stdout


def inspect(docker, name):
    return json.loads(run(docker, 'inspect', name))[0]


def peers(docker):
    identifiers = run(docker, 'ps', '-aq', '--filter', f'label=com.docker.compose.project={PROJECT}').split()
    return {record['Name']: (record['Id'], record['State']['Running'], record['State']['StartedAt'])
            for identifier in identifiers
            for record in [inspect(docker, identifier)]
            if record['Name'].lstrip('/') != CONTAINER}


def verify(docker, compose_file, memory, cpu, running):
    observed = inspect(docker, CONTAINER)
    assert observed['State']['Running'] is running, observed['State']
    assert observed['HostConfig']['Memory'] == memory * 1048576
    assert observed['HostConfig']['MemorySwap'] == memory * 1048576
    assert observed['HostConfig']['NanoCpus'] == int(cpu * 1_000_000_000)
    assert observed['HostConfig']['NetworkMode'] == 'none'
    assert not observed['Mounts'], 'Test service unexpectedly acquired a mount'
    resolved = json.loads(run(control.COMPOSE, '-p', PROJECT, '-f', str(compose_file), 'config', '--format', 'json'))
    settings = resolved['services'][SERVICE]
    assert int(settings['mem_limit']) == memory * 1048576
    assert int(settings['memswap_limit']) == memory * 1048576
    assert float(settings['cpus']) == cpu
    proof = {'container_id': observed['Id'], 'running': running, 'memory_mib': memory,
             'cpu': cpu, 'memory_swap_total_mib': observed['HostConfig']['MemorySwap'] / 1048576,
             'compose_persisted': True}
    if running:
        # Check actual cgroup v2 kernel controls, not only Docker metadata.
        script = "import json,pathlib; p=pathlib.Path('/sys/fs/cgroup'); print(json.dumps({n:(p/n).read_text().strip() for n in ['memory.max','memory.swap.max','cpu.max']}))"
        kernel = json.loads(run(docker, 'exec', CONTAINER, 'python', '-c', script))
        assert int(kernel['memory.max']) == memory * 1048576
        assert int(kernel['memory.swap.max']) == 0
        quota, period = map(int, kernel['cpu.max'].split())
        assert abs(quota / period - cpu) < 1e-8
        proof['kernel_cgroup_v2'] = kernel
    return proof


def main():
    docker = shutil.which('docker')
    if not docker:
        raise RuntimeError('Docker is required')
    # Fail before any mutation when the image or Compose plugin is absent.
    image = json.loads(run(docker, 'image', 'inspect', IMAGE))[0]
    plugins = json.loads(run(docker, 'info', '--format', '{{json .ClientInfo.Plugins}}'))
    compose = next((plugin['Path'] for plugin in plugins if plugin['Name'] == 'compose'), None)
    if not compose or not Path(compose).is_file():
        raise RuntimeError('An installed Docker Compose plugin is required')
    existing = run(docker, 'ps', '-aq', '--filter', f'name=^/{CONTAINER}$').strip()
    if existing:
        raise RuntimeError(f'Refusing to alter existing container {CONTAINER}')
    before_peers = peers(docker)
    report = {'image_id': image['Id'], 'docker': run(docker, 'version', '--format', '{{.Server.Version}}').strip(),
              'compose': run(compose, 'version', '--short').strip(), 'project': PROJECT, 'service': SERVICE,
              'checks': []}
    runtime = REPOSITORY / '.runtime'
    runtime.mkdir(exist_ok=True)
    original = {name: getattr(control, name) for name in ('DOCKER', 'COMPOSE', 'ROOT', 'CONFIG', 'DATABASE', 'JOURNAL')}
    with tempfile.TemporaryDirectory(prefix='resource-check-', dir=runtime) as directory:
        root = Path(directory)
        compose_file = root / 'compose.yml'
        config_file = root / 'projects.json'
        compose_file.write_text(json.dumps({'services': {SERVICE: {
            'image': IMAGE, 'pull_policy': 'never', 'network_mode': 'none', 'read_only': True, 'stop_grace_period': '1s',
            'user': '65534:65534', 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
            'command': ['python', '-c', 'import time; time.sleep(3600)'],
            'mem_limit': '64m', 'memswap_limit': '64m', 'cpus': 0.1,
        }}}))
        config_file.write_text(json.dumps({PROJECT: {'repository': 'TPCT/deploy-monitor', 'branch': 'main',
                                                   'container_prefixes': [f'{PROJECT}-{SERVICE}-'], 'recipe': {'compose_project':PROJECT,'compose_files':[str(compose_file)],'services':[SERVICE]}}}))
        control.DOCKER = docker
        control.COMPOSE = compose
        control.ROOT = root
        control.CONFIG = str(config_file)
        control.DATABASE = str(root / 'policy.sqlite')
        control.JOURNAL = root / 'transaction.json'
        args = [compose, '-p', PROJECT, '-f', str(compose_file)]
        created = False
        try:
            created = True  # Even a partially successful up is cleaned up below.
            run(*args, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', SERVICE)
            current = inspect(docker, CONTAINER)
            assert current['Config']['Labels']['com.docker.compose.project'] == PROJECT
            assert current['Config']['Labels']['com.docker.compose.service'] == SERVICE
            assert current['Image'] == image['Id']
            previous_id = current['Id']
            for action, running, memory, cpu in [
                ('container-limits-save', True, 96, 0.2),
                ('container-limits-save', False, 160, 0.4),
                ('resource-apply', True, 128, 0.25),
                ('resource-apply', False, 192, 0.5),
            ]:
                if running and not inspect(docker, CONTAINER)['State']['Running']:
                    run(docker, 'start', CONTAINER)
                if not running:
                    run(docker, 'stop', '--time', '3', CONTAINER)
                limits = {'memory_mib': memory, 'cpu': cpu}
                if action == 'resource-apply':
                    db = connect(control.DATABASE)
                    try:
                        policy = resource_policy.read(db, PROJECT)
                        document = copy.deepcopy(policy['document'])
                        document['budget'] = dict(limits)
                        document['environments']['production'] = {'budget': dict(limits), 'services': {CONTAINER: dict(limits)}}
                        saved = resource_policy.save(db, PROJECT, policy['revision'], document)
                    finally:
                        db.close()
                detail = control.execute({'action': 'resource-details', 'project': PROJECT, 'container': CONTAINER})
                request = {'action': action, 'project': PROJECT, 'container': CONTAINER,
                           'container_id': detail['container_id'], 'revision': detail['revision'],
                           'request_id': str(uuid.uuid4()), 'actor': 'local-resource-integration'}
                if action == 'resource-apply':
                    request.update(environment='production', policy_revision=saved['revision'])
                else:
                    assert detail['resource_policy']['configured'] is False
                    assert detail['resource_policy']['editable'] is True
                    request['limits'] = limits
                failures = []
                def trace(frame, event, arg):
                    if event == 'exception' and frame.f_code.co_filename == control.__file__ and frame.f_code.co_name in ('execute', 'command'):
                        failures.append({'line': frame.f_lineno, 'type': arg[0].__name__, 'message': str(arg[1]), 'diagnostic': frame.f_locals['result'].stderr.decode() if frame.f_code.co_name == 'command' and 'result' in frame.f_locals else {'before': control.without_resource_limits(frame.f_locals['before_config'], SERVICE), 'after': control.without_resource_limits(frame.f_locals['after'], SERVICE)} if frame.f_code.co_name == 'execute' and 'after' in frame.f_locals else ''})
                    return trace
                previous_trace = sys.gettrace()
                sys.settrace(trace)
                try:
                    result = control.execute(request)
                finally:
                    sys.settrace(previous_trace)
                assert result['status'] == 'succeeded', {'result': result, 'exceptions': failures}
                proof = verify(docker, compose_file, memory, cpu, running)
                assert proof['container_id'] != previous_id
                assert control.execute(request) == result
                assert inspect(docker, CONTAINER)['Id'] == proof['container_id'], 'Same request recreated the service twice'
                proof['same_request_replayed_without_recreate'] = True
                report['checks'].append({'action': action, 'phase': 'running-apply' if running else 'stopped-apply', **proof})
                # Recreate directly from the persisted Compose configuration.
                control.recreate(args, SERVICE, running)
                persisted = verify(docker, compose_file, memory, cpu, running)
                assert persisted['container_id'] != proof['container_id']
                report['checks'].append({'action': action, 'phase': 'running-recreate' if running else 'stopped-recreate', **persisted})
                if action == 'container-limits-save':
                    # A new request must not act on a same-name replacement using the old identity.
                    stale_request = {**request, 'request_id': str(uuid.uuid4())}
                    before_file = compose_file.read_bytes()
                    try:
                        control.execute(stale_request)
                    except control.ControlError as error:
                        assert 'replaced' in str(error).lower(), str(error)
                    else:
                        raise AssertionError('Stale container ID was accepted')
                    assert inspect(docker, CONTAINER)['Id'] == persisted['container_id']
                    assert compose_file.read_bytes() == before_file
                    report['checks'].append({'action': action, 'phase': 'stale-id-rejected',
                                             'running': running, 'no_recreation_or_file_change': True})
                    if not running:
                        # Starting the persisted stopped service proves its kernel controls too.
                        run(docker, 'start', CONTAINER)
                        kernel_proof = verify(docker, compose_file, memory, cpu, True)
                        report['checks'].append({'action': action, 'phase': 'stopped-start-kernel', **kernel_proof})
                        run(docker, 'stop', '--time', '3', CONTAINER)
                        verify(docker, compose_file, memory, cpu, False)
                previous_id = persisted['container_id']
                assert not control.JOURNAL.exists()
                print(f'PASS: {action}: {"running" if running else "stopped"} apply and persisted recreate: {memory} MiB, {cpu} CPU', flush=True)
        finally:
            try:
                if created:
                    run(*args, 'rm', '-s', '-f', SERVICE)
                assert not run(docker, 'ps', '-aq', '--filter', f'name=^/{CONTAINER}$').strip()
                assert peers(docker) == before_peers, 'Existing deploy-monitor containers changed during this test'
                report['cleanup'] = 'test service removed; existing project containers unchanged'
            finally:
                for name, value in original.items():
                    setattr(control, name, value)
    report['temporary_files_removed'] = True
    evidence = runtime / 'resource-check-last.json'
    evidence.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'result': 'passed', 'evidence': str(evidence), 'cleanup': report['cleanup']}))


if __name__ == '__main__':
    main()
