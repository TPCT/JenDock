"""Local Docker proof: two independent environment recipes, no legacy binding."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import control
import registry
import workspace
from store import connect, snapshot
from live_adoption import command, inspect, peers

PROJECT = 'deploy-monitor'
SERVICES = {'development': 'ownership-check-dev', 'production': 'ownership-check-prod'}


def main():
    docker = shutil.which('docker')
    image = json.loads(command([docker, 'image', 'inspect', 'python:3.13-slim']))[0]
    plugins = json.loads(command([docker, 'info', '--format', '{{json .ClientInfo.Plugins}}']))
    compose = next(item['Path'] for item in plugins if item['Name'] == 'compose')
    for service in SERVICES.values():
        if command([docker, 'ps', '-aq', '--filter', 'name=^/' + PROJECT + '-' + service + '-1$']).strip():
            raise RuntimeError('Refusing to alter an existing fixture container')
    baseline = peers(docker)
    runtime = ROOT / '.runtime'; runtime.mkdir(exist_ok=True)
    report = {'project': PROJECT, 'environments': {}}
    with tempfile.TemporaryDirectory(prefix='ownership-check-', dir=runtime) as directory:
        root = Path(directory)
        db = connect(root / 'state.sqlite')
        registry.import_legacy(db, {}, [])
        registry.create_project(db, PROJECT, {'repository': 'TPCT/deploy-monitor', 'repository_id': 12})
        policy = root / 'projects.json'; policy.write_text('{}')
        created = []
        try:
            for env, service in SERVICES.items():
                file = root / (env + '.yml')
                file.write_text(json.dumps({'services': {service: {
                    'image': image['Id'], 'pull_policy': 'never', 'network_mode': 'none',
                    'read_only': True, 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
                    'command': ['python', '-c', 'import time; time.sleep(3600)'],
                    'stop_grace_period': '1s'}}}))
                args = [compose, '-p', PROJECT, '-f', str(file)]
                command([*args, 'create', '--no-build', '--pull', 'never', service])
                name = PROJECT + '-' + service + '-1'
                actual = inspect(docker, name); created.append(actual['Id'])
                assert not actual['State']['Running'] and not actual['Mounts']
                assert actual['HostConfig']['NetworkMode'] == 'none' and not actual['HostConfig']['PortBindings']
                recipe = {'compose_project': PROJECT, 'compose_files': [str(file)], 'services': [service], 'owned_services': [service]}
                document = registry.canonical(recipe)
                row = db.execute('INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,0)',
                                 (PROJECT, hashlib.sha256(document.encode()).hexdigest(), document))
                db.execute('UPDATE registry_environments SET recipe_revision=? WHERE project=? AND name=?', (row.lastrowid, PROJECT, env))
                report['environments'][env] = {'container_id': actual['Id'], 'name': name, 'service': service}
            observed = []
            with patch.object(control, 'DATABASE', str(root / 'state.sqlite')), patch.object(control, 'CONFIG', str(policy)), \
                 patch.object(control, 'ROOT', root), patch.object(control, 'DOCKER', docker), \
                 patch.object(control, 'COMPOSE', compose), patch.object(control, 'JOURNAL', root / 'journal.json'):
                for env, item in report['environments'].items():
                    actual, service, files, args = control.inspect_target(PROJECT, item['name'])
                    assert actual['_monitor_environment'] == env and service == item['service']
                    assert files == [root / (env + '.yml')]
                    for action in ('container-start', 'container-stop'):
                        result = control.execute({'action': action, 'project': PROJECT, 'container': item['name'],
                            'container_id': item['container_id'], 'request_id': str(uuid.uuid4()), 'actor': 'local-fixture'})
                        assert result['status'] == 'succeeded', result
                    after = inspect(docker, item['container_id'])
                    assert after['Id'] == item['container_id'] and not after['State']['Running']
                    labels = after['Config']['Labels']
                    observed.append({'ID': after['Id'], 'Names': item['name'], 'State': 'exited',
                        'ComposeProject': PROJECT, 'ComposeService': service,
                        'ComposeConfigFiles': labels['com.docker.compose.project.config_files']})
                    item['inspect_start_stop'] = 'passed'
                snapshot(db, 'containers', observed); snapshot(db, 'worker', {'state': 'running'})
                for env in SERVICES:
                    view = workspace.read(db, PROJECT, env)
                    assert len(view['services']) == 1 and view['services'][0]['capabilities']['manage'] is True
                assert registry.get_project(db, PROJECT)['legacy_binding'] is None
                report['legacy_binding'] = None
                report['workspace_management_both_environments'] = 'passed'
        finally:
            db.close()
            for identifier in created:
                command([docker, 'rm', '-f', identifier])
    assert peers(docker) == baseline, 'Existing peer identity/configuration/lifecycle changed'
    report['existing_peers_unchanged'] = True
    report['fixture_cleanup'] = True
    (runtime / 'ownership-check-last.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
