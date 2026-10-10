"""Signed GitHub receiver and authenticated read-only dashboard; no Docker access."""
import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from store import connect, enqueue, projects

PREFIX = '/_deploy'
MAX_BODY = 1024 * 1024


class Application:
    def __init__(self, config, database, credentials):
        self.config = config
        self.database = database
        self.credentials = json.loads(Path(credentials).read_text())
        self.failures = {}
        self.lock = threading.Lock()

    def authorized(self, header, peer):
        now = time.monotonic()
        with self.lock:
            self.failures = {k: v for k, v in self.failures.items() if v[1] > now - 60}
            if self.failures.get(peer, (0, 0))[0] >= 10:
                return False
        good = False
        try:
            scheme, value = header.split(' ', 1)
            if scheme.lower() != 'basic' or len(value) > 1024:
                return False
            user, password = base64.b64decode(value, validate=True).decode().split(':', 1)
            digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(self.credentials['salt']), 260000)
            good = hmac.compare_digest(user, self.credentials['username']) and hmac.compare_digest(digest.hex(), self.credentials['password_hash'])
        except (ValueError, UnicodeError, TypeError):
            pass
        with self.lock:
            if not good:
                self.failures[peer] = (self.failures.get(peer, (0, now))[0] + 1, now)
        return good

    def webhook(self, headers, body):
        signature = headers.get('X-Hub-Signature-256', '')
        if not re.fullmatch(r'sha256=[0-9a-f]{64}', signature):
            return 401, {'error': 'Invalid signature'}
        expected = 'sha256=' + hmac.new(self.credentials['webhook_secret'].encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return 401, {'error': 'Invalid signature'}
        delivery = headers.get('X-GitHub-Delivery', '')
        if not re.fullmatch(r'[A-Za-z0-9-]{1,100}', delivery):
            return 400, {'error': 'Invalid delivery identifier'}
        event = headers.get('X-GitHub-Event', '')
        if event == 'ping':
            return 200, {'accepted': 'ping'}
        if event != 'push':
            return 202, {'ignored': 'event'}
        try:
            payload = json.loads(body)
            repo = payload['repository']['full_name']
            repo_id = payload['repository']['id']
            ref, sha = payload['ref'], payload['after']
            if payload.get('deleted') or sha == '0' * 40:
                return 202, {'ignored': 'deleted ref'}
            if not re.fullmatch('[0-9a-f]{40}', sha):
                raise ValueError()
            matches = [(name, p) for name, p in projects(self.config).items()
                       if p['repository'] == repo and p['repository_id'] == repo_id
                       and ref == 'refs/heads/' + p['branch']]
        except (ValueError, KeyError, TypeError):
            return 400, {'error': 'Invalid push payload'}
        if not matches:
            return 202, {'ignored': 'repository or branch not approved'}
        db = connect(self.database)
        try:
            results = [enqueue(db, delivery + '-' + name, name, sha, p.get('enabled', False))
                       for name, p in matches]
            return 202, {'jobs': [r[0] for r in results], 'new': any(r[1] for r in results)}
        finally:
            db.close()

    def state(self):
        db = connect(self.database)
        try:
            allowed = ('repository', 'branch', 'enabled', 'reason', 'label')
            return {'observed_at': time.time(),
                    'projects': {k: {x: v[x] for x in allowed if x in v} for k, v in projects(self.config).items()},
                    'jobs': [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY id DESC LIMIT 100')],
                    'snapshots': {r['name']: {'updated': r['updated'], 'value': json.loads(r['value'])}
                                  for r in db.execute('SELECT * FROM snapshots')}}
        finally:
            db.close()


def handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'DeployMonitor'
        sys_version = ''

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            # Do not log credentials, request bodies, query parameters or user content.
            pass

        def respond(self, code, value, mime='application/json; charset=utf-8', auth=False):
            data = json.dumps(value).encode() if isinstance(value, dict) else value
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            if auth:
                self.send_header('WWW-Authenticate', 'Basic realm="Deployment monitor", charset="UTF-8"')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == '/healthz':
                return self.respond(200, {'status': 'ok'})
            if not app.authorized(self.headers.get('Authorization', ''), self.client_address[0]):
                return self.respond(401, {'error': 'Authentication required'}, auth=True)
            if self.path == PREFIX + '/api/state':
                return self.respond(200, app.state())
            files = {PREFIX + '/': ('index.html', 'text/html; charset=utf-8'),
                     PREFIX + '/NotoSansArabic-Regular.ttf': ('NotoSansArabic-Regular.ttf', 'font/ttf'),
                     PREFIX + '/FONT-LICENSE.txt': ('FONT-LICENSE.txt', 'text/plain; charset=utf-8'),
                     PREFIX + '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     PREFIX + '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if self.path not in files:
                return self.respond(404, {'error': 'Not found'})
            name, mime = files[self.path]
            return self.respond(200, (Path(__file__).parent / 'web' / name).read_bytes(), mime)

        def do_POST(self):
            if self.path != PREFIX + '/github':
                return self.respond(404, {'error': 'Not found'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length <= 0 or length > MAX_BODY or self.headers.get('Transfer-Encoding'):
                    return self.respond(413, {'error': 'Invalid body length'})
                body = self.rfile.read(length)
                if len(body) != length:
                    return self.respond(400, {'error': 'Incomplete body'})
                code, response = app.webhook(self.headers, body)
                self.respond(code, response)
            except (ValueError, TimeoutError):
                self.respond(400, {'error': 'Invalid request'})
    return Handler


if __name__ == '__main__':
    application = Application(os.environ.get('PROJECTS_FILE', '/config/projects.json'),
                              os.environ.get('DATABASE', '/state/state.sqlite'),
                              os.environ.get('CREDENTIALS_FILE', '/config/credentials.json'))
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), handler(application)).serve_forever()
