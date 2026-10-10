"""Real Docker adoption proof using only a disposable local SQLite workload.

Run: python3 tests/live_adoption.py
No deployed monitor calls; all registry writes use a temporary local SQLite file.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))
import adoption
import registry
from store import connect

PROJECT = 'deploy-monitor'
SERVICE = 'adoption-check'
CONTAINER = PROJECT + '-' + SERVICE + '-1'
IMAGE = 'python:3.13-slim'


def command(args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=90,
                            env={**os.environ, 'COMPOSE_IGNORE_ORPHANS': 'true'})
    if result.returncode:
        raise RuntimeError(f'Fixture command failed ({result.returncode}): {result.stderr}')
    return result.stdout


def inspect(docker, identifier):
    return json.loads(command([docker, 'inspect', identifier]))[0]


def identity(row):
    return {key: row[key] for key in ('Id', 'Image', 'Mounts', 'State', 'HostConfig', 'Config')}


def peers(docker):
    ids = command([docker, 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + PROJECT]).split()
    return {row['Name']: identity(row) for identifier in ids for row in [inspect(docker, identifier)]
            if row['Name'].lstrip('/') != CONTAINER}


def main():
    docker = shutil.which('docker')
    if not docker:
        raise RuntimeError('Docker is required')
    image = json.loads(command([docker, 'image', 'inspect', IMAGE]))[0]
    plugins = json.loads(command([docker, 'info', '--format', '{{json .ClientInfo.Plugins}}']))
    compose = next((plugin['Path'] for plugin in plugins if plugin['Name'] == 'compose'), None)
    if not compose or not Path(compose).is_file():
        raise RuntimeError('Docker Compose is required')
    if command([docker, 'ps', '-aq', '--filter', f'name=^/{CONTAINER}$']).strip():
        raise RuntimeError('Refusing to alter an existing adoption-check container')
    before_peers = peers(docker)
    runtime = REPOSITORY / '.runtime'
    runtime.mkdir(exist_ok=True)
    report = {'project': PROJECT, 'service': SERVICE, 'image_id': image['Id']}
    with tempfile.TemporaryDirectory(prefix='adoption-check-', dir=runtime) as directory:
        root = Path(directory)
        storage = root / 'storage'; storage.mkdir()
        synthetic = storage / 'synthetic.sqlite'
        fixture = sqlite3.connect(synthetic)
        fixture.execute('CREATE TABLE proof(value TEXT)')
        fixture.execute("INSERT INTO proof VALUES('synthetic adoption fixture')")
        fixture.commit(); fixture.close()
        database_hash = hashlib.sha256(synthetic.read_bytes()).hexdigest()
        compose_file = root / 'compose.yml'
        compose_file.write_text(json.dumps({'services': {SERVICE: {
            'image': IMAGE, 'pull_policy': 'never', 'network_mode': 'none', 'read_only': True,
            'user': '65534:65534', 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
            'stop_grace_period': '1s', 'command': ['python', '-c', 'import time; time.sleep(3600)'],
            'environment': {'DB_CONNECTION': 'sqlite', 'DB_DATABASE': '/fixture/synthetic.sqlite'},
            'volumes': [{'type': 'bind', 'source': str(storage), 'target': '/fixture', 'read_only': True}],
        }}}))
        compose_bytes = compose_file.read_bytes()
        args = [compose, '-p', PROJECT, '-f', str(compose_file)]
        db = None
        try:
            command([*args, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', SERVICE])
            before = identity(inspect(docker, CONTAINER))
            assert before['Image'] == image['Id'] and before['State']['Running']
            assert before['HostConfig']['NetworkMode'] == 'none'
            assert not before['HostConfig']['PortBindings']
            assert len(before['Mounts']) == 1 and before['Mounts'][0]['Source'] == str(storage)
            assert before['Mounts'][0]['RW'] is False
            def inventory_command(argv):
                # Execute the real collector against exactly our fixture, without
                # running unrelated database engine probes on this workstation.
                if argv[1] == 'ps':
                    argv = [*argv, '--filter', 'label=com.docker.compose.project=' + PROJECT,
                            '--filter', 'label=com.docker.compose.service=' + SERVICE]
                elif argv[1] != 'inspect':
                    raise AssertionError('Unexpected SQLite inventory command')
                return command(argv)
            inventory = adoption.collect(docker, inventory_command)
            assert len(inventory['containers']) == 1
            state_file = root / 'registry.sqlite'
            db = connect(state_file)
            registry.import_legacy(db, {PROJECT: {
                'repository': 'TPCT/deploy-monitor', 'repository_id': 12,
                'branch': 'main', 'automatic': False, 'container_prefixes': [PROJECT + '-' + SERVICE + '-'],
                'recipe': {'compose_project': PROJECT, 'compose_files': [str(compose_file)], 'services': [SERVICE]},
            }}, [])
            development = registry.get_environment(db, PROJECT, 'development')
            mismatch = copy.deepcopy(inventory)
            mismatch['containers'][0]['compose_files'] = [str(root / 'wrong-compose.yml')]
            try:
                adoption.preview(db, PROJECT, 'production', mismatch)
            except ValueError as error:
                assert 'ownership conflicts' in str(error)
            else:
                raise AssertionError('Mismatched Compose identity was accepted')
            assert registry.get_project(db, PROJECT)['legacy_binding'] is None
            preview = adoption.preview(db, PROJECT, 'production', inventory)
            public = adoption.public(preview)
            assert str(root) not in json.dumps(public)
            assert public['databases'][0]['engine'] == 'sqlite'
            assert public['databases'][0]['database'] == '/fixture/synthetic.sqlite'
            fresh = adoption.collect(docker, inventory_command)
            adopted = adoption.adopt(db, PROJECT, 'production', preview['fingerprint'], fresh, 'local-adoption-integration')
            assert adoption.adopt(db, PROJECT, 'production', preview['fingerprint'], fresh, 'local-adoption-integration') == adopted
            db.close(); db = connect(state_file)
            reread = registry.get_project(db, PROJECT)
            assert reread == adopted and reread['legacy_binding'] == 'production'
            assert reread['environments']['development'] == development
            target = reread['environments']['production']
            assert target['state'] == 'active'
            recipe = json.loads(db.execute('SELECT document FROM registry_recipes WHERE id=?', (target['recipe_revision'],)).fetchone()[0])
            assert recipe['owned_services'] == [SERVICE] and recipe['services'] == [SERVICE]
            assert db.execute('SELECT fingerprint FROM workload_adoptions').fetchone()[0] == preview['fingerprint']
            assert identity(inspect(docker, CONTAINER)) == before
            assert synthetic.read_bytes() and hashlib.sha256(synthetic.read_bytes()).hexdigest() == database_hash
            assert compose_file.read_bytes() == compose_bytes
            assert sorted(path.name for path in storage.iterdir()) == ['synthetic.sqlite']
            report.update(metadata_persisted_after_reconnect=True, exact_retry_idempotent=True,
                          mismatched_compose_rejected=True, container_and_mounts_unchanged=True,
                          database_bytes_unchanged=True, compose_bytes_unchanged=True,
                          public_host_paths_omitted=True, container_id=before['Id'], fingerprint=preview['fingerprint'])
        finally:
            if db is not None: db.close()
            command([*args, 'rm', '-s', '-f', SERVICE])
            assert not command([docker, 'ps', '-aq', '--filter', f'name=^/{CONTAINER}$']).strip()
            assert peers(docker) == before_peers, 'Existing project containers changed'
            report['cleanup'] = 'Fixture removed; existing project containers unchanged'
    report['temporary_files_removed'] = True
    evidence = runtime / 'adoption-check-last.json'
    evidence.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'result': 'passed', 'evidence': str(evidence), 'checks': report}))


if __name__ == '__main__':
    main()
