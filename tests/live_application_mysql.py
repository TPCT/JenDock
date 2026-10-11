"""Opt-in real two-environment MySQL-family application runtime acceptance.

All resources belong to disposable deploy-monitor fixtures. Uses real adoption,
managed provisioning, encrypted credentials, identity helper and bridge creation.
Source/artifact publication is supplied by the existing synthetic application
fixture; this does not test the build pipeline. The deliberate restart-race
injection runs actual authentication before restarting the exact fixture DB.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cryptography.fernet import Fernet
import adopted_workloads
import adoption
import application_credentials
import application_database_bindings as bindings
import application_database_probe as probe
import application_operations as ops
import live_ingress_runtime as live
import managed_lifecycle
import managed_services
import registry
import resource_policy
import service_runtime
import service_runtime_operations
from store import connect

PROJECT = 'deploy-monitor'
MODULES = ('adoption.py', 'application_credentials.py', 'application_database_bindings.py',
           'application_database_identity.py', 'application_database_probe.py',
           'application_database_networks.py', 'database_target.py', 'database_candidates.py',
           'service_runtime.py', 'service_runtime_operations.py', 'managed_lifecycle.py',
           'service_provisioning.py', 'database_helper.py', 'database_inspector_entry.py',
           'application_operations.py', 'managed_services.py', 'build_profiles.py', 'registry.py',
           'tests/live_ingress_runtime.py', 'tests/live_application_mysql.py')


def command(argv, *, payload=None, env=None, timeout=40):
    result = subprocess.run(argv, input=payload, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, timeout=timeout)
    if result.returncode:
        # Driver output and Docker configuration can contain credentials.
        raise RuntimeError('Fixture command failed (exit %d).' % result.returncode)
    return result.stdout.decode().strip()


def until(check, seconds=100):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.4)
    raise AssertionError('Fixture readiness deadline exceeded.')


def peers(docker):
    ids = command([docker, 'ps', '-aq', '--no-trunc']).split()
    return {r['Id']: (r['Image'], r['State']['Running']) for r in
            json.loads(command([docker, 'inspect', *ids]))} if ids else {}


def application_command(body):
    source = '''import http.server,json,os,pymysql
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  result={'version':VERSION,'message':os.environ.get('APP_MESSAGE')}
  if self.path in ('/write','/read'):
   db=pymysql.connect(host=os.environ['DB_HOST'],port=int(os.environ['DB_PORT']),database=os.environ['DB_DATABASE'],user=os.environ['DB_USERNAME'],password=os.environ['DB_PASSWORD'],connect_timeout=3,read_timeout=4,write_timeout=3,autocommit=True)
   try:
    with db.cursor() as cursor:
     if self.path=='/write':cursor.execute('INSERT INTO app_data(id,value) VALUES(1,%s) ON DUPLICATE KEY UPDATE value=%s',(VERSION,VERSION))
     cursor.execute('SELECT id,value FROM app_data ORDER BY id');result['rows']=cursor.fetchall()
     cursor.execute('SELECT DATABASE(),VERSION()');result['identity']=cursor.fetchone()
   finally:db.close()
  data=json.dumps(result).encode();self.send_response(200);self.end_headers();self.wfile.write(data)
 def log_message(self,*args):pass
http.server.HTTPServer(('0.0.0.0',8080),Handler).serve_forever()
'''
    source = 'VERSION=' + repr(body) + '\n' + source
    return ['python', '-u', '-c', 'exec(' + repr(source) + ')']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--engine', choices=('mysql', 'mariadb'), required=True)
    parser.add_argument('--database-image', required=True)
    parser.add_argument('--helper-image', required=True)
    parser.add_argument('--evidence', type=Path)
    args = parser.parse_args()
    if not args.run:
        raise SystemExit('Explicit --run required.')
    docker = shutil.which('docker')
    if not docker:
        raise SystemExit('Docker is required.')
    socket = os.environ.get('DOCKER_HOST', 'unix:///var/run/docker.sock')
    if not socket.startswith('unix:///'):
        raise SystemExit('A local Unix Docker socket is required.')
    compose = next(p['Path'] for p in json.loads(command([docker, 'info', '--format', '{{json .ClientInfo.Plugins}}'])) if p['Name'] == 'compose')
    for image in (args.database_image, args.helper_image):
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
            raise SystemExit('Immutable image IDs are required.')
        assert command([docker, 'image', 'inspect', image, '--format', '{{.Id}}']) == image
    info = json.loads(command([docker, 'image', 'inspect', args.database_image]))[0]
    reference = next(ref for ref in info['RepoDigests'] if ref.split('@')[0].split('/')[-1] == args.engine)
    assert command([docker, 'image', 'inspect', reference, '--format', '{{.Id}}']) == args.database_image
    for environment in registry.ENVIRONMENTS:
        assert not command([docker, 'network', 'ls', '-q', '--filter', 'name=^' + PROJECT + '-' + environment + '-managed$']), 'Managed environment already in use.'
    baseline = peers(docker)
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in MODULES}
    nonce = uuid.uuid4().hex
    root = Path(tempfile.mkdtemp(prefix='deploy-monitor-application-mysql-'))
    root.chmod(0o700)
    (root / 'managed').mkdir()
    state = root / 'state'; state.mkdir(mode=0o700)
    containers, networks, services = set(), set(), []
    volumes, databases, secrets, attachments, apps = {}, {}, {}, {}, {}
    db = None
    app_image = None
    success = False
    checks = {}
    prefix = 'MYSQL' if args.engine == 'mysql' else 'MARIADB'
    client = 'mysql' if args.engine == 'mysql' else 'mariadb'

    def inspect(cid):
        return json.loads(command([docker, 'inspect', cid]))[0]

    def sql(cid, statement):
        # Credentials are resolved inside the exact database container; no
        # secret appears in argv, diagnostic output or evidence.
        script = 'MYSQL_PWD="${MYSQL_ROOT_PASSWORD:-$MARIADB_ROOT_PASSWORD}" exec ' + client + ' --protocol=TCP -h127.0.0.1 -uroot -N -B fixture'
        return command([docker, 'exec', '-i', cid, 'sh', '-c', script], payload=statement.encode())

    def ready(cid):
        try:
            return sql(cid, 'SELECT 1;') == '1'
        except (RuntimeError, subprocess.TimeoutExpired):
            return False

    def start_database(environment, record):
        keys = ('ROOT_PASSWORD', 'DATABASE', 'USER', 'PASSWORD')
        env = dict(os.environ, **{prefix + '_' + k: record[k] for k in keys})
        options = [value for key in keys for value in ('--env', prefix + '_' + key)]
        cid = command([docker, 'run', '-d', '--name', record['name'],
            '--label', 'com.docker.compose.project=' + PROJECT,
            '--label', 'com.docker.compose.service=' + environment + '-db',
            '--label', 'com.docker.compose.project.config_files=' + str(root / 'database.json'),
            '--label', 'net.shiftcodes.application-fixture=' + nonce,
            '--network', record['network'], '--network-alias', environment + '-db',
            '--memory', '768m', '--cpus', '1', '--mount',
            'type=volume,src=' + record['volume'] + ',dst=/var/lib/mysql', *options, reference], env=env)
        containers.add(cid)
        until(lambda: ready(cid))
        return cid

    def current(environment):
        return managed_services.get(db, apps[environment]['id'])

    def request(environment, path):
        cid = current(environment)['binding']['container_id']; containers.add(cid)
        code = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/" + path + "',timeout=8).read().decode())"
        return json.loads(command([docker, 'exec', cid, 'python', '-c', code]))

    def context_definition(environment):
        return {'project': PROJECT, 'environment': environment, 'service_id': apps[environment]['id'],
                'runtime': {'database_attachment': attachments[environment]['pin']}}

    def lifecycle(environment, action):
        saved = current(environment); cid = saved['binding']['container_id']
        operation = uuid.uuid4().hex
        managed_lifecycle.accept(db, operation, saved['id'], action, expected_container_id=cid,
                                 docker=docker, authorize=lambda: None)
        outcome = managed_lifecycle.apply(db, operation, docker=docker, authorize=lambda: None)
        assert outcome['status'] == 'succeeded', outcome
        assert current(environment)['binding']['container_id'] == cid
        assert inspect(cid)['State']['Running'] is (action != 'stop')

    try:
        build = root / 'image'; build.mkdir()
        (build / 'Dockerfile').write_text('FROM ' + args.helper_image + '\nENTRYPOINT []\nCMD []\nCOPY marker /fixture-marker\n')
        (build / 'marker').write_text(nonce)
        app_image = command([docker, 'build', '--network', 'none', '-q', str(build)], timeout=120)
        assert re.fullmatch(r'sha256:[a-f0-9]{64}', app_image)
        db = connect(root / 'registry.sqlite')
        cfg = root / 'database.json'
        cfg.write_text(json.dumps({'services': {e + '-db': {'image': reference} for e in registry.ENVIRONMENTS}}))
        recipe = {'compose_project': PROJECT, 'compose_files': [str(cfg)], 'services': [e + '-db' for e in registry.ENVIRONMENTS]}
        registry.import_legacy(db, {PROJECT: {'repository': 'TPCT/deploy-monitor', 'repository_id': 12,
            'branch': 'main', 'container_prefixes': ['deploy-monitor-application-' + nonce], 'recipe': recipe}}, [])
        resource_policy.save(db, PROJECT, 0, {'budget': {'memory_mib': 512, 'cpu': 2},
            'environments': {e: {'budget': {'memory_mib': 256, 'cpu': 1}, 'services': {}} for e in registry.ENVIRONMENTS}})
        key = root / 'vault'; key.write_bytes(Fernet.generate_key()); key.chmod(0o600)
        principal = ops.Principal('fixture-owner', 'console', frozenset({'databases:manage'}))
        for environment in registry.ENVIRONMENTS:
            name = 'deploy-monitor-application-' + nonce + '-' + environment
            network = command([docker, 'network', 'create', '--label', 'net.shiftcodes.application-fixture=' + nonce, name]); networks.add(network)
            volume = command([docker, 'volume', 'create', '--label', 'net.shiftcodes.application-fixture=' + nonce, name + '-data'])
            volumes[volume] = json.loads(command([docker, 'volume', 'inspect', volume]))[0]
            record = {'name': name, 'network': network, 'volume': volume, 'ROOT_PASSWORD': uuid.uuid4().hex,
                      'DATABASE': 'fixture', 'USER': 'fixture_app', 'PASSWORD': uuid.uuid4().hex}
            databases[environment] = record
            record['cid'] = start_database(environment, record)
            sql(record['cid'], 'CREATE TABLE app_data(id INTEGER PRIMARY KEY,value VARCHAR(255) NOT NULL);')
            def fixture_inventory_command(argv):
                if argv[1:] == ['ps', '-aq', '--no-trunc']:
                    return '\n'.join(r['cid'] for r in databases.values() if 'cid' in r)
                return command(argv)
            inventory = adoption.collect(docker, fixture_inventory_command)
            preview = adoption.preview(db, PROJECT, environment, inventory, services=[environment + '-db'])
            adoption.adopt(db, PROJECT, environment, preview['fingerprint'], inventory, 'fixture-owner', services=[environment + '-db'])
            record['revision'] = adopted_workloads.get(db, PROJECT, environment)['recipe_revision']
            secrets[environment] = application_credentials.save_credential(db, principal, PROJECT, environment,
                {'username': 'fixture_app', 'password': record['PASSWORD']}, expected_revision=0, key_path=key)
        print(json.dumps({'stage': 'two-databases-adopted', 'engine': args.engine}), flush=True)
        with patch.dict(os.environ, {'DATABASE_INSPECTION_KEY_FILE': str(key), 'DATABASE_INSPECTOR_IMAGE': args.helper_image,
                                     'DOCKER_HOST': socket, 'STATE_DIR': str(state)}), patch.object(live, 'app_command', application_command):
            for environment in registry.ENVIRONMENTS:
                apps[environment] = live.create_application(db, docker, compose, root, environment,
                    args.engine + '-' + environment, app_image, containers, networks, name='database-fixture')
                sid = apps[environment]['id']; services.append(sid)
                saved = bindings.save_binding(db, principal, PROJECT, environment,
                    dict(service_id=sid, engine=args.engine, ownership={'kind': 'adopted-compose',
                         'reference': environment + '-db', 'revision': databases[environment]['revision']},
                         database_name='fixture', database_port=3306, secret_id=secrets[environment]['id'], secret_revision=1,
                         environment_mapping={'host': 'DB_HOST', 'port': 'DB_PORT', 'database': 'DB_DATABASE',
                                              'username': 'DB_USERNAME', 'password': 'DB_PASSWORD'}), expected_revision=0)
                attachments[environment] = {'id': saved['id'], 'pin': {'binding_id': saved['id'], 'revision': 1, 'digest': saved['definition_digest']}}
                if environment == 'development':
                    actual_verify = probe.verify
                    def restart_after_authentication(*values, **options):
                        proof = actual_verify(*values, **options)
                        cid = databases[environment]['cid']
                        before = inspect(cid)['State']['StartedAt']
                        command([docker, 'restart', '--time', '5', cid]); until(lambda: ready(cid))
                        assert inspect(cid)['State']['StartedAt'] != before
                        return proof
                    with patch.object(probe, 'verify', side_effect=restart_after_authentication):
                        try:
                            service_runtime.Context(db, lambda: None, key).attachment(context_definition(environment), docker, ensure=True)
                        except probe.ProbeError as error:
                            assert error.code == 'identity_changed'
                        else:
                            raise AssertionError('Restart between authentication and attachment was accepted.')
                    # No durable attachment record or extra database endpoint is allowed.
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='application_database_networks'").fetchone():
                        assert not db.execute('SELECT 1 FROM application_database_networks WHERE binding_id=?', (saved['id'],)).fetchone()
                    assert len(inspect(databases[environment]['cid'])['NetworkSettings']['Networks']) == 1
                    checks['restart_after_probe_rejected_before_attachment'] = True
                operation = uuid.uuid4().hex
                service_runtime_operations.accept(db, operation, sid, {'APP_MESSAGE': environment + '-persisted'},
                    expected_container_id=current(environment)['binding']['container_id'], key_path=key,
                    authorize=lambda: None, database_attachment=attachments[environment]['pin'])
                outcome = service_runtime_operations.apply(db, operation, docker=docker, compose=compose, authorize=lambda: None)
                assert outcome['status'] == 'succeeded', outcome
                containers.add(current(environment)['binding']['container_id'])
                expected = [[1, args.engine + '-' + environment]]
                assert request(environment, 'write')['rows'] == expected
                assert request(environment, 'read')['rows'] == expected
                assert request(environment, 'read')['message'] == environment + '-persisted'
                assert sql(databases[environment]['cid'], 'SELECT id,value FROM app_data;') == '1\t' + args.engine + '-' + environment
                for action in ('stop', 'start', 'restart'):
                    before = inspect(current(environment)['binding']['container_id'])['State']['StartedAt']
                    lifecycle(environment, action)
                    if action != 'stop':
                        until(lambda: inspect(current(environment)['binding']['container_id']).get('State', {}).get('Health', {}).get('Status') == 'healthy', 30)
                        assert request(environment, 'read')['rows'] == expected
                    if action == 'restart':
                        assert inspect(current(environment)['binding']['container_id'])['State']['StartedAt'] != before
                print(json.dumps({'stage': 'application-write-read-lifecycle', 'environment': environment, 'engine': args.engine}), flush=True)
            checks['both_environment_http_write_read_and_app_lifecycle'] = True
            env_networks = {}
            for environment in registry.ENVIRONMENTS:
                row = inspect(current(environment)['binding']['container_id'])
                env_networks[environment] = {n['NetworkID'] for n in row['NetworkSettings']['Networks'].values()}
                assert len(env_networks[environment]) == 2
                assert databases[environment]['network'] not in env_networks[environment]
            assert not (env_networks['development'] & env_networks['production'])
            checks['disjoint_dedicated_application_bridges'] = True
            db.close(); db = connect(root / 'registry.sqlite')
            for environment in registry.ENVIRONMENTS:
                assert request(environment, 'read')['rows'] == [[1, args.engine + '-' + environment]]
            checks['persistence_after_registry_reconnect'] = True
            # Restart and replacement must invalidate the retained development identity.
            original = databases['development']['cid']
            command([docker, 'restart', '--time', '5', original]); until(lambda: ready(original))
            try:
                service_runtime.Context(db, lambda: None, key).attachment(context_definition('development'), docker)
            except ValueError:
                checks['established_database_restart_rejected'] = True
            else:
                raise AssertionError('Restarted database retained accepted network identity.')
            row = inspect(original)
            assert row['Config']['Labels']['net.shiftcodes.application-fixture'] == nonce
            command([docker, 'rm', '-f', original]); containers.remove(original)
            databases['development']['cid'] = start_database('development', databases['development'])
            assert databases['development']['cid'] != original
            try:
                service_runtime.Context(db, lambda: None, key).attachment(context_definition('development'), docker)
            except ValueError:
                checks['replacement_database_rejected'] = True
            else:
                raise AssertionError('Replacement database bypassed adoption identity.')
            # Production remains usable while development fails closed.
            assert request('production', 'read')['rows'] == [[1, args.engine + '-production']]
            checks['production_unaffected_by_development_identity_failure'] = True
            application_credentials.revoke_credential(db, principal, PROJECT, 'production', secrets['production']['id'], expected_revision=1)
            bindings.disable_binding(db, principal, PROJECT, 'production', attachments['production']['id'], expected_revision=1)
            command([docker, 'stop', '--time', '5', databases['production']['cid']])
            lifecycle('production', 'stop')
            cid = current('production')['binding']['container_id']
            try:
                managed_lifecycle.accept(db, uuid.uuid4().hex, apps['production']['id'], 'start', expected_container_id=cid, docker=docker, authorize=lambda: None)
            except ValueError:
                pass
            else:
                raise AssertionError('Revoked application attachment allowed startup.')
            assert inspect(cid)['State']['Running'] is False
            checks['revoked_disabled_attachment_stopped_database_app_stop_and_start_rejection'] = True
            success = True
    finally:
        cleanup_errors = []
        helper_state = state / 'application-database-probe'
        if helper_state.exists():
            from database_helper import InspectorHelper
            try:
                InspectorHelper(docker=docker, socket=socket, image=args.helper_image,
                                state_dir=helper_state).reconcile_pending()
            except Exception:
                cleanup_errors.append('identity-helper-reconciliation')
        # The helper can fail after accepting a service but before returning it.
        # This private registry contains only this fixture's accepted services.
        if db and db.execute("SELECT 1 FROM sqlite_master WHERE name='managed_services'").fetchone():
            for row in db.execute('SELECT id,environment FROM managed_services WHERE project=? AND name=?', (PROJECT, 'database-fixture')):
                if row['id'] not in services: services.append(row['id'])
                found = command([docker, 'network', 'ls', '-q', '--no-trunc', '--filter',
                                 'name=^' + PROJECT + '-' + row['environment'] + '-managed$']).split()
                for network in found:
                    observed = json.loads(command([docker, 'network', 'inspect', network]))[0]
                    assert observed['Labels'].get('net.shiftcodes.managed.project') == PROJECT
                    assert observed['Labels'].get('net.shiftcodes.managed.environment') == row['environment']
                    networks.add(network)
        # Include only replacement containers tied to exact recorded fixture service IDs.
        for sid in services:
            containers.update(command([docker, 'ps', '-aq', '--no-trunc', '--filter', 'label=net.shiftcodes.managed.service=' + sid]).split())
        for cid in containers:
            if not command([docker, 'ps', '-aq', '--no-trunc', '--filter', 'id=' + cid]):
                continue
            row = inspect(cid); labels = row['Config']['Labels']
            assert labels.get('net.shiftcodes.application-fixture') == nonce or labels.get('net.shiftcodes.managed.service') in services
            command([docker, 'rm', '-f', '-v', cid])
        if db and db.execute("SELECT 1 FROM sqlite_master WHERE name='application_database_networks'").fetchone():
            for record in db.execute('SELECT binding_id,document FROM application_database_networks'):
                journal = json.loads(record['document'])
                network = journal.get('network_id')
                if network: networks.add(network)
                # An interrupted create can precede persistence of its ID.
                found = command([docker, 'network', 'ls', '-q', '--no-trunc', '--filter',
                                 'label=net.shiftcodes.application-database.binding=' + record['binding_id']]).split()
                for network in found:
                    row = json.loads(command([docker, 'network', 'inspect', network]))[0]
                    assert row['Labels'].get('net.shiftcodes.application-database.nonce') == journal['nonce']
                    assert row['Labels'].get('net.shiftcodes.application-database.project') == PROJECT
                    networks.add(network)
        for network in networks:
            command([docker, 'network', 'rm', network])
        for volume, original in volumes.items():
            row = json.loads(command([docker, 'volume', 'inspect', volume]))[0]
            assert row['Labels']['net.shiftcodes.application-fixture'] == nonce
            assert all(row.get(k) == original.get(k) for k in ('Name', 'Driver', 'Mountpoint', 'CreatedAt', 'Options', 'Labels'))
            assert not command([docker, 'ps', '-aq', '--filter', 'volume=' + volume])
            command([docker, 'volume', 'rm', volume])
        if app_image:
            command([docker, 'image', 'rm', app_image])
        if db: db.close()
        assert peers(docker) == baseline, 'Peer containers changed.'
        assert not cleanup_errors, 'Helper cleanup requires recovery: ' + ','.join(cleanup_errors)
        assert hashes == {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in MODULES}, 'Source changed during acceptance.'
        evidence = {'passed': success, 'engine': args.engine, 'database_image': args.database_image,
                    'helper_image': args.helper_image, 'checks': checks, 'exact_cleanup': True,
                    'peers_unchanged': True, 'source_hashes': hashes,
                    'scope': 'Managed Python HTTP application runtime; not Laravel queue/scheduler or browser acceptance'}
        if args.evidence:
            args.evidence.write_text(json.dumps(evidence, indent=2) + '\n')
        print(json.dumps(evidence), flush=True)
        if success:
            shutil.rmtree(root)
        else:
            print(json.dumps({'fixture_state_retained': str(root)}), flush=True)


if __name__ == '__main__':
    main()
