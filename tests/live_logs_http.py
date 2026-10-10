"""Real local authenticated HTTP -> Unix worker -> Docker logs acceptance.

Run: python3 tests/live_logs_http.py
Creates only deploy-monitor/logs-check using a cached worker image, no network,
ports or mounts. Configuration paths and the receiver's hard-coded Unix address
are redirected to a private temporary fixture. No API, Docker, ownership check,
log output, authentication or redaction result is mocked. No browser is driven.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import control
from server import Application, handler

PROJECT, SERVICE = 'deploy-monitor', 'logs-check'
NAME = f'{PROJECT}-{SERVICE}-1'
IMAGE = 'deploy-monitor-worker:local-current'


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=90,
                            env={**os.environ, 'COMPOSE_IGNORE_ORPHANS': 'true'})
    if result.returncode:
        raise RuntimeError(f'Local fixture command failed: {args[0]} ({result.returncode})')
    return result.stdout


def peers(docker):
    ids = run(docker, 'ps', '-aq', '--filter', f'label=com.docker.compose.project={PROJECT}').split()
    return {row['Id']: (row['State']['Status'], row['State']['StartedAt'])
            for row in json.loads(run(docker, 'inspect', *ids))} if ids else {}


def main():
    docker = shutil.which('docker')
    if not docker:
        raise RuntimeError('Docker is required')
    image = json.loads(run(docker, 'image', 'inspect', IMAGE))[0]
    plugins = json.loads(run(docker, 'info', '--format', '{{json .ClientInfo.Plugins}}'))
    compose = next(plugin['Path'] for plugin in plugins if plugin['Name'] == 'compose')
    if run(docker, 'ps', '-aq', '--filter', f'name=^/{NAME}$').strip():
        raise RuntimeError(f'Refusing to alter existing container {NAME}')
    before = peers(docker)
    report = {'project': PROJECT, 'service': SERVICE, 'image_id': image['Id']}
    original = {key: getattr(control, key) for key in ('DOCKER', 'COMPOSE', 'ROOT', 'CONFIG', 'DATABASE', 'JOURNAL', 'SOCKET')}
    with tempfile.TemporaryDirectory(prefix='logs-http-') as directory:
        root = Path(directory)
        path = root / 'compose.json'
        path.write_text(json.dumps({'services': {SERVICE: {
            'image': IMAGE, 'network_mode': 'none', 'read_only': True,
            'user': '65534:65534', 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
            'environment': {'API_TOKEN': 'fixture-environment-secret'},
            'entrypoint': ['python', '-u', '-c', 'import os,sys; print("stdout-log-sentinel"); print("password=fixture-password-secret",file=sys.stderr); print(os.environ["API_TOKEN"])'],
        }}}))
        config = root / 'projects.json'
        config.write_text(json.dumps({PROJECT: {'repository': 'TPCT/deploy-monitor', 'branch': 'main',
            'container_prefixes': [f'{PROJECT}-{SERVICE}-'],
            'recipe': {'compose_project': PROJECT, 'compose_files': [str(path)], 'services': [SERVICE]}}}))
        credentials = root / 'credentials.json'
        credentials.write_text(json.dumps({'username': 'admin', 'salt': 'ab' * 16,
            'password_hash': hashlib.pbkdf2_hmac('sha256', b'local-test-only', bytes.fromhex('ab' * 16), 260000).hex(),
            'webhook_secret': 'local-fixture-only'}))
        for key, value in {'DOCKER': docker, 'COMPOSE': compose, 'ROOT': root, 'CONFIG': str(config),
                           'DATABASE': str(root / 'state.sqlite'), 'JOURNAL': root / 'journal.json',
                           'SOCKET': str(root / 'control.sock')}.items():
            setattr(control, key, value)
        base = [compose, '-p', PROJECT, '-f', str(path)]
        worker = http = None
        threads = []
        created = False
        try:
            created = True
            run(*base, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', SERVICE)
            cid = run(*base, 'ps', '-aq', SERVICE).strip()
            run(docker, 'wait', cid)
            inspected = json.loads(run(docker, 'inspect', cid))[0]
            assert inspected['Image'] == image['Id'] and not inspected['Mounts']
            assert inspected['HostConfig']['NetworkMode'] == 'none'
            worker = control.start()
            app = Application(config, control.DATABASE, credentials)
            http = ThreadingHTTPServer(('127.0.0.1', 0), handler(app))
            app.origin = f'http://127.0.0.1:{http.server_port}'
            app.secure = False
            app.cookie_name = 'monitor_session'
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start(); threads.append(thread)
            real_socket = socket.socket

            class LocalSocket(real_socket):
                def connect(self, address):
                    if self.family == socket.AF_UNIX and address == '/state/control.sock':
                        address = control.SOCKET
                    return super().connect(address)

            def post(endpoint, body, cookie='', csrf=''):
                request = Request(app.origin + '/_deploy/api/' + endpoint, data=json.dumps(body).encode(),
                    headers={'Content-Type': 'application/json', 'Origin': app.origin,
                             'Cookie': cookie, 'X-CSRF-Token': csrf})
                try:
                    with urlopen(request, timeout=20) as response:
                        return response.status, json.load(response), response.headers.get('Set-Cookie', '').split(';')[0]
                except HTTPError as error:
                    with error:
                        return error.code, json.load(error), ''

            body = {'action': 'container-logs', 'project': PROJECT, 'container': NAME, 'container_id': cid, 'tail': 200}
            with patch('server.socket.socket', LocalSocket):
                assert post('action', body)[0] == 401
                code, session, cookie = post('login', {'username': 'admin', 'password': 'local-test-only'})
                assert code == 200 and cookie and session['csrf']
                assert post('action', body, cookie, 'wrong-csrf')[0] == 403
                code, logs, _ = post('action', body, cookie, session['csrf'])
                assert code == 200 and logs['container_id'] == cid
                assert 'stdout-log-sentinel' in logs['text'] and 'password=[redacted]' in logs['text']
                assert 'fixture-password-secret' not in logs['text'] and 'fixture-environment-secret' not in logs['text']
                assert logs['truncated'] is False and logs['tail'] == 200 and logs['observed'] > 0
                assert post('action', dict(body, container='unowned-container'), cookie, session['csrf'])[0] == 409
                run(*base, 'up', '-d', '--force-recreate', '--no-deps', '--no-build', '--pull', 'never', SERVICE)
                replacement = run(*base, 'ps', '-aq', SERVICE).strip()
                run(docker, 'wait', replacement)
                assert replacement != cid
                code, rejected, _ = post('action', body, cookie, session['csrf'])
                assert code == 409 and 'replaced' in rejected['error'] and 'text' not in rejected
                code, fresh, _ = post('action', dict(body, container_id=replacement), cookie, session['csrf'])
                assert code == 200 and fresh['container_id'] == replacement and 'stdout-log-sentinel' in fresh['text']
                report.update(authenticated_http=True, csrf_enforced=True, real_unix_worker=True,
                    real_docker_stdout_stderr=True, redaction=True, stopped_container_logs=True,
                    replacement_rejected=True, refreshed_identity_recovers=True)
        finally:
            for server in (http, worker):
                if server:
                    server.shutdown(); server.server_close()
            for thread in threads:
                thread.join(timeout=5)
            try:
                if created:
                    run(*base, 'rm', '-sf', SERVICE)
            finally:
                for key, value in original.items():
                    setattr(control, key, value)
                assert peers(docker) == before, 'Existing Compose peers changed'
    report.update(cleanup_verified=True, peers_unchanged=True, passed=True)
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
