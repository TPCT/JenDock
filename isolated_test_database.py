"""Ephemeral PostgreSQL for internal tests; no host ports or production mounts."""
from contextlib import contextmanager
import secrets
import subprocess
import time

POSTGRES_IMAGE = 'postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea'


@contextmanager
def database(test, test_name, docker, run):
    kind = test.get('database')
    if kind is None:
        yield 'none', {}
        return
    if kind != 'postgres':
        raise ValueError('Unsupported isolated test database')
    if not test_name.startswith('deploy-test-') or not all(c.isalnum() or c == '-' for c in test_name):
        raise ValueError('Invalid isolated test resource name')
    network, name = test_name + '-net', test_name + '-db'
    password = secrets.token_hex(24)
    created_network = created_database = False
    try:
        run([docker, 'network', 'create', '--internal', network])
        created_network = True
        run([docker, 'create', '--name', name, '--network', network, '--network-alias', 'test-postgres',
             '--user', '70:70', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
             '--memory=384m', '--cpus=.5', '--pids-limit=128',
             '--tmpfs', '/var/lib/postgresql/data:rw,nosuid,uid=70,gid=70,mode=0700,size=384m',
             '--tmpfs', '/var/run/postgresql:rw,nosuid,uid=70,gid=70,mode=0775,size=8m',
             '--tmpfs', '/tmp:rw,nosuid,size=16m',
             '-e', 'POSTGRES_DB=deploy_test', '-e', 'POSTGRES_USER=deploy_test',
             '-e', 'POSTGRES_PASSWORD=' + password, POSTGRES_IMAGE])
        created_database = True
        run([docker, 'start', name])
        for _ in range(45):
            ready = subprocess.run([docker, 'exec', name, 'pg_isready', '-U', 'deploy_test', '-d', 'deploy_test'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('Isolated PostgreSQL did not become ready')
        yield network, {'DB_CONNECTION': 'pgsql', 'DB_HOST': 'test-postgres', 'DB_PORT': '5432',
                        'DB_DATABASE': 'deploy_test', 'DB_USERNAME': 'deploy_test', 'DB_PASSWORD': password,
                        'DATABASE_URL': '', 'DB_URL': ''}
    finally:
        if created_database:
            subprocess.run([docker, 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if created_network:
            subprocess.run([docker, 'network', 'rm', network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
