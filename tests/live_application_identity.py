"""Opt-in fixed application identity probe against disposable real engines.

Uses already local immutable images, no published ports, tmpfs database storage,
and the production isolated helper transport. Does not test full app deployment.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database_helper import InspectorHelper


def command(args, **kwargs):
    return subprocess.check_output(args, stderr=subprocess.DEVNULL, timeout=30, **kwargs).decode().strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    for name in ('helper', 'mysql', 'mariadb'):
        parser.add_argument('--' + name + '-image', required=True)
    args = parser.parse_args()
    if not args.run:
        raise SystemExit('Explicit --run required')
    docker = shutil.which('docker')
    socket = os.environ.get('DOCKER_HOST', 'unix:///var/run/docker.sock')
    images = {engine: getattr(args, engine + '_image') for engine in ('mysql', 'mariadb')}
    for image in [args.helper_image, *images.values()]:
        assert len(image) == 71 and image.startswith('sha256:')
        assert command([docker, 'image', 'inspect', image, '--format', '{{.Id}}']) == image
    before = set(command([docker, 'ps', '-aq', '--no-trunc']).splitlines())
    token = uuid.uuid4().hex
    created = []
    results = []
    try:
        with tempfile.TemporaryDirectory(prefix='deploy-monitor-identity-') as directory:
            state = Path(directory) / 'helper'
            state.mkdir(mode=0o700)
            helper = InspectorHelper(docker=docker, socket=socket, image=args.helper_image, state_dir=state)
            for engine, image in images.items():
                password = uuid.uuid4().hex
                prefix = 'MYSQL' if engine == 'mysql' else 'MARIADB'
                env = dict(os.environ, **{prefix + '_ROOT_PASSWORD': uuid.uuid4().hex,
                    prefix + '_DATABASE': 'fixture', prefix + '_USER': 'application', prefix + '_PASSWORD': password})
                options = []
                for key in ('ROOT_PASSWORD', 'DATABASE', 'USER', 'PASSWORD'):
                    options.extend(['--env', prefix + '_' + key])
                cid = command([docker, 'create', '--name', 'deploy-monitor-identity-' + token[:8] + '-' + engine,
                    '--label', 'com.docker.compose.project=deploy-monitor',
                    '--label', 'com.docker.compose.service=application-identity-fixture',
                    '--label', 'net.shiftcodes.fixture=' + token, '--network', 'none',
                    '--memory', '768m', '--cpus', '1', '--tmpfs', '/var/lib/mysql:rw,size=536870912',
                    *options, image], env=env)
                created.append(cid)
                command([docker, 'start', cid])

                def revalidate(target):
                    row = json.loads(command([docker, 'inspect', target]))[0]
                    assert target == cid and row['Id'] == cid and row['Image'] == image
                    assert row['Config']['Labels']['net.shiftcodes.fixture'] == token and row['State']['Running']

                request = dict(action='application-identity', engine=engine, database='fixture',
                               user='application', password=password, port=3306)
                deadline = time.monotonic() + 100
                while True:
                    result = helper.inspect(cid, request, revalidate=revalidate)
                    if 'result' in result:
                        break
                    assert result == {'error': 'connection_failed'}, result
                    assert time.monotonic() < deadline, 'Database readiness deadline exceeded'
                    time.sleep(.5)
                assert result['result']['engine'] == engine
                assert result['result']['database'] == 'fixture'
                # Use the real writable application login separately from the
                # identity-only helper, never extend the helper with arbitrary SQL.
                client = 'mysql' if engine == 'mysql' else 'mariadb'
                sql = b"CREATE TABLE identity_sentinel(id INT PRIMARY KEY); INSERT INTO identity_sentinel VALUES (7); SELECT id FROM identity_sentinel;"
                written = command([docker, 'exec', '-i', '--env', 'MYSQL_PWD', cid,
                    client, '--protocol=TCP', '-h', '127.0.0.1', '-u', 'application',
                    '--batch', '--skip-column-names', 'fixture'],
                    input=sql, env=dict(os.environ, MYSQL_PWD=password))
                assert written == '7', 'Writable application account verification failed'
                wrong = helper.inspect(cid, request | {'engine': 'mariadb' if engine == 'mysql' else 'mysql'}, revalidate=revalidate)
                assert wrong == {'error': 'identity_changed'}, wrong
                denied = helper.inspect(cid, request | {'password': uuid.uuid4().hex}, revalidate=revalidate)
                assert denied == {'error': 'connection_failed'}, denied
                invalid = helper.inspect(cid, request | {'sql': 'DROP DATABASE fixture'}, revalidate=revalidate)
                assert invalid == {'error': 'invalid_request'}, invalid
                assert not list(state.iterdir()), 'Helper journal was not cleaned'
                results.append({'engine': engine, 'image': image, 'identity': result['result'],
                    'application_account_insert_read_verified': True, 'wrong_engine_rejected': True,
                    'invalid_credentials_rejected': True, 'arbitrary_sql_rejected': True})
                revalidate(cid)
                command([docker, 'rm', '-f', '-v', cid])
                created.remove(cid)
    finally:
        for cid in created:
            row = json.loads(command([docker, 'inspect', cid]))[0]
            assert row['Id'] == cid and row['Config']['Labels']['net.shiftcodes.fixture'] == token
            command([docker, 'rm', '-f', '-v', cid])
    assert set(command([docker, 'ps', '-aq', '--no-trunc']).splitlines()) == before
    print(json.dumps({'helper_image': args.helper_image, 'results': results, 'cleanup_verified': True,
                      'scope': 'Real database engines and isolated helper; not application cutover acceptance'}))


if __name__ == '__main__':
    main()
