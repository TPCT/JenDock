"""Real local stopped-container proof advancement; no deployed monitor calls."""
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
import adoption
import compose_review
import control
import registry
from store import connect

PROJECT, SERVICE, IMAGE = 'deploy-monitor', 'provenance-check', 'python:3.13-slim'
CONTAINER = PROJECT + '-' + SERVICE + '-1'


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=120,
                            env={**os.environ, 'COMPOSE_IGNORE_ORPHANS': 'true'})
    if result.returncode:
        raise RuntimeError('Local provenance fixture command failed: ' + result.stderr)
    return result.stdout


def inspect(docker, name): return json.loads(command([docker, 'inspect', name]))[0]


def peers(docker):
    ids = command([docker, 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + PROJECT]).split()
    result = {}
    for identifier in ids:
        row = inspect(docker, identifier)
        if row['Name'].lstrip('/') == CONTAINER: continue
        result[row['Name']] = {key: row[key] for key in ('Id', 'Image', 'Mounts', 'HostConfig', 'Config')}
        # Docker inspection returns mount entries in nondeterministic order.
        result[row['Name']]['Mounts'] = sorted(row['Mounts'], key=lambda mount: mount['Destination'])
        result[row['Name']]['State'] = {key: row['State'].get(key) for key in
            ('Running', 'Paused', 'Restarting', 'OOMKilled', 'Dead', 'Pid', 'ExitCode', 'StartedAt', 'FinishedAt')}
    return result


def verify_peers(docker, before):
    after = peers(docker)
    differences = []
    for name in before.keys() | after.keys():
        if name not in before or name not in after:
            differences.append(name + ':container-added-or-removed')
            continue
        for key in before[name]:
            if before[name][key] != after[name][key]:
                if key == 'State':
                    differences.extend(name + ':State.' + field for field in before[name][key]
                                       if before[name][key][field] != after[name][key][field])
                else: differences.append(name + ':' + key)
    assert not differences, 'Existing peer changed: ' + ', '.join(differences)


def main():
    docker = shutil.which('docker')
    if not docker: raise RuntimeError('Docker is required')
    image = json.loads(command([docker, 'image', 'inspect', IMAGE]))[0]
    plugins = json.loads(command([docker, 'info', '--format', '{{json .ClientInfo.Plugins}}']))
    compose = next(p['Path'] for p in plugins if p['Name'] == 'compose')
    if command([docker, 'ps', '-aq', '--filter', 'name=^/' + CONTAINER + '$']).strip():
        raise RuntimeError('Refusing to modify an existing provenance-check container')
    before = peers(docker)
    constants = {key: getattr(control, key) for key in ('DOCKER', 'COMPOSE', 'ROOT', 'CONFIG', 'DATABASE', 'JOURNAL')}
    runtime = REPOSITORY / '.runtime'; runtime.mkdir(exist_ok=True)
    report = {'project': PROJECT, 'service': SERVICE, 'checks': []}
    with tempfile.TemporaryDirectory(prefix='provenance-check-', dir=runtime) as directory:
        root = Path(directory); base = root / 'base.yml'; original = root / 'original.yml'; canonical = root / 'canonical.yml'
        base.write_text(json.dumps({'services': {SERVICE: {'image': IMAGE, 'pull_policy': 'never',
            'network_mode': 'none', 'read_only': True, 'user': '65534:65534', 'cap_drop': ['ALL'],
            'security_opt': ['no-new-privileges:true'], 'command': ['python', '-c', 'import time; time.sleep(3600)']}}}))
        original.write_bytes(base.read_bytes()); canonical.write_bytes(base.read_bytes())
        original_bytes = original.read_bytes()
        args = [compose, '-p', PROJECT, '-f', str(original)]
        database = root / 'registry.sqlite'; config = root / 'projects.json'
        recipe = {'compose_project': PROJECT, 'compose_files': [str(canonical)], 'services': [SERVICE]}
        config.write_text(json.dumps({PROJECT: {'repository': 'TPCT/deploy-monitor', 'repository_id': 12,
            'branch': 'main', 'automatic': False, 'container_prefixes': [PROJECT + '-' + SERVICE + '-'], 'recipe': recipe}}))
        db = None
        try:
            command([*args, 'create', '--no-build', '--pull', 'never', SERVICE])
            initial = inspect(docker, CONTAINER)
            assert not initial['State']['Running'] and initial['Image'] == image['Id']
            def inventory_command(argv):
                if argv[1] == 'ps': argv = [*argv, '--filter', 'label=com.docker.compose.project=' + PROJECT, '--filter', 'label=com.docker.compose.service=' + SERVICE]
                elif argv[1] != 'inspect': raise AssertionError('Unexpected inventory command')
                return command(argv)
            resolver = lambda recipe, service, files: compose_review.review(recipe, service, files, root, command, compose)
            db = connect(database); registry.import_legacy(db, json.loads(config.read_text()), [])
            inventory = adoption.collect(docker, inventory_command)
            preview = adoption.preview(db, PROJECT, 'production', inventory, resolver=resolver)
            adoption.adopt(db, PROJECT, 'production', preview['fingerprint'], inventory, 'local-provenance-check', resolver=resolver)
            for key, value in {'DOCKER': docker, 'COMPOSE': compose, 'ROOT': root, 'CONFIG': config,
                               'DATABASE': str(database), 'JOURNAL': root / 'journal.json'}.items(): setattr(control, key, value)
            adopted_recipe_id = registry.get_environment(db, PROJECT, 'production')['recipe_revision']
            fingerprints = [preview['service_provenance'][SERVICE]['fingerprint']]
            ids = [initial['Id']]
            for memory, cpu in [(96, 0.2), (160, 0.4)]:
                detail = control.details(PROJECT, CONTAINER)
                body = {'action': 'container-limits-save', 'project': PROJECT, 'container': CONTAINER,
                        'container_id': detail['container_id'], 'revision': detail['revision'],
                        'request_id': str(uuid.uuid4()), 'actor': 'local-provenance-check',
                        'limits': {'memory_mib': memory, 'cpu': cpu}}
                result = control.execute(body)
                if result['status'] != 'succeeded':
                    restored = inspect(docker, CONTAINER)
                    assert result['status'] == 'rolled-back'
                    assert not restored['State']['Running'] and restored['Image'] == image['Id']
                    assert restored['HostConfig']['Memory'] == detail['resources']['memory_mib'] * 1048576
                    assert canonical.read_bytes() == original.read_bytes()
                    assert registry.get_environment(db, PROJECT, 'production')['recipe_revision'] == adopted_recipe_id
                    report['failure'] = {'status': result['status'], 'stopped_state_restored': True,
                        'previous_resources_restored': True, 'canonical_file_restored': True,
                        'registry_binding_unchanged': True}
                    raise AssertionError('Independent original configuration prevents proof advancement; mutation rolled back safely.')
                current = inspect(docker, CONTAINER); ids.append(current['Id'])
                assert not current['State']['Running'] and current['HostConfig']['Memory'] == memory * 1048576
                assert current['HostConfig']['MemorySwap'] == memory * 1048576
                assert current['HostConfig']['NanoCpus'] == int(cpu * 1e9)
                assert current['HostConfig']['NetworkMode'] == 'none' and not current['Mounts']
                assert current['Config']['Labels']['com.docker.compose.project.config_files'].split(',') == recipe['compose_files']
                env = registry.get_environment(db, PROJECT, 'production')
                saved = json.loads(db.execute('SELECT document FROM registry_recipes WHERE id=?', (env['recipe_revision'],)).fetchone()[0])
                assert saved['service_provenance'][SERVICE]['observed_files'] == recipe['compose_files']
                assert original.read_bytes() == original_bytes
                fingerprint = saved['service_provenance'][SERVICE]['fingerprint']; fingerprints.append(fingerprint)
                assert fingerprint != fingerprints[-2]
                assert control.details(PROJECT, CONTAINER)['container_id'] == current['Id']
                assert control.execute(body)['status'] == 'succeeded'
                assert inspect(docker, CONTAINER)['Id'] == current['Id']
                report['checks'].append({'memory_mib': memory, 'cpu': cpu, 'stopped_preserved': True,
                    'proof_advanced': True, 'details_after_save': True, 'replay_no_recreation': True})
            assert len(set(ids)) == 3 and len(set(fingerprints)) == 3
            assert not control.JOURNAL.exists()
            db.close(); db = connect(database)
            assert registry.get_environment(db, PROJECT, 'production')['recipe_revision'] == env['recipe_revision']
            report.update(registry_reopened=True, independent_original_unchanged=True, proof_rebased_to_canonical=True)
        finally:
            if db is not None: db.close()
            command([docker, 'rm', '-f', CONTAINER])
            for key, value in constants.items(): setattr(control, key, value)
            verify_peers(docker, before)
            assert not command([docker, 'ps', '-aq', '--filter', 'name=^/' + CONTAINER + '$']).strip()
            report.update(cleanup_verified=True, existing_peers_unchanged=True)
            (runtime / 'provenance-check-last.json').write_text(json.dumps(report, indent=2) + '\n')
    verify_peers(docker, before)
    assert not command([docker, 'ps', '-aq', '--filter', 'name=^/' + CONTAINER + '$']).strip()
    report.update(cleanup_verified=True, existing_peers_unchanged=True)
    (runtime / 'provenance-check-last.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__': main()
