"""Owned HTTPS transitions, sharing the HTTP adapter's host lock and authority.

Certificate paths come only from the installed certificate capability. Requests
never contain paths, snippets, listeners, upstream URLs or executable arguments.
"""
import ipaddress
import json
import re
import secrets
import time
from pathlib import Path

from ingress_host import _digest


class HostTLSAdapter:
    def __init__(self, http, certificates, *, listener, authorize_certificate=None):
        if (not isinstance(listener, dict) or set(listener) != {'address', 'port'}
                or type(listener['port']) is not int or not 1 <= listener['port'] <= 65535):
            raise ValueError('Invalid administrator TLS listener')
        ipaddress.IPv4Address(listener['address'])
        self.http, self.certificates, self.listener = http, certificates, dict(listener)
        self.http.related_configs = self._related_configs
        self.authorize_certificate = authorize_certificate

    def _related_configs(self, http_path):
        route = http_path.stem
        if not re.fullmatch('[a-f0-9]{32}', route): return set()
        policy = self.http._policy()
        tls_path, journal = self._paths(policy, route)
        state = self.http._state(journal)
        if not state: return set()
        current = self.http._read(tls_path)
        if current is None: return set()
        _, http_journal = self.http._paths(policy, route)
        http_state = self.http._state(http_journal)
        if (not http_state or state.get('route_id') != route
                or state.get('operation_id') != http_state.get('operation_id')
                or _digest(state['candidate']) != state['digest']
                or (_digest(state['previous']) if state['previous'] is not None else None) != state['expected_digest']
                or current not in (state['candidate'], state['previous'])):
            raise ValueError('Related TLS configuration ownership changed')
        return {str(tls_path)}

    def _paths(self, policy, route):
        return Path(policy['managed_dir'])/(route+'-tls.conf'), Path(policy['state_dir'])/(route+'-tls.json')

    def _context(self, policy, request):
        grant = self.http._grant(policy, request['route_id'])
        self.http._authorize_route(request['route_id'], grant)
        _, state = self.http._paths(policy, request['route_id'])
        http_state = self.http._state(state)
        if not http_state or http_state['operation_id'] != request['operation_id']:
            raise ValueError('TLS must belong to the existing HTTP operation')
        self.http._verify(policy, request['route_id'])
        return grant

    def _collision(self, policy, route, hostname, owned):
        http_path, _ = self.http._paths(policy, route)
        tls_path, _ = self._paths(policy, route)
        # The HTTP file has already been strongly verified by _context.
        import fnmatch
        for entry in self.http.executor.inventory():
            if entry['path'] == str(http_path) or owned and entry['path'] == str(tls_path):
                continue
            if any(fnmatch.fnmatchcase(hostname, name.lower().rstrip('.')) for name in entry['names']):
                raise ValueError('TLS hostname conflicts with an existing Apache vhost')

    def _certificate(self, request, grant):
        value = self.certificates.generation_files(request['route_id'], request['generation'])
        if (value.get('route_id') != request['route_id'] or value.get('hostname') != grant['hostname'] or value.get('generation') != request['generation']
                or value.get('fingerprint') != request['fingerprint']
                or not isinstance(value.get('not_after'), (int, float)) or value['not_after'] <= time.time()):
            raise ValueError('Verified current certificate generation is required')
        return value

    def _challenge(self, policy, request, grant):
        if self.http.challenge_root is None:
            raise ValueError('HTTP challenge routing is not configured')
        path, _ = self.http._paths(policy, request['route_id'])
        if self.http._read(path) != self.http._config(grant, policy['listener']):
            raise ValueError('Upgrade the owned HTTP route to enable challenge routing before issuance')
        root = self.http.challenge_root
        if self.certificates.paths['challenge_root'] != root:
            raise ValueError('HTTP and certificate challenge roots differ')
        self.http._safe(root, directory=True)
        for directory in (root/'.well-known', root/'.well-known'/'acme-challenge'):
            if not directory.exists(): directory.mkdir(mode=0o755)
            self.http._safe(directory, directory=True)
        token = secrets.token_urlsafe(32)
        probe = directory/token
        text = secrets.token_urlsafe(48)
        # Exclusive creation: a probe never replaces an existing challenge.
        with probe.open('x') as stream: stream.write(text)
        probe.chmod(0o644)
        try:
            self.http._safe(probe)
            if self.http.executor.verify_challenge(grant['hostname'], token, text.encode()) is not True:
                raise ValueError('HTTP challenge routing is not verified')
        finally:
            if self.http._read(probe) == text: probe.unlink()

    def request_certificate(self, request):
        self.http._request(request, ('route_id', 'operation_id'))
        with self.http._locked() as policy:
            grant = self._context(policy, request)
            if not callable(self.authorize_certificate):
                raise ValueError('Certificate authority verification is not configured')
            def authorize():
                self._context(policy, request)
                self.authorize_certificate(request['route_id'], grant)
            authorize()
            self._challenge(policy, request, grant)
            return self.certificates.request(request['route_id'], request['operation_id'], grant['hostname'],
                authorize=authorize)

    def _config(self, grant, certificate, files):
        # Internal capability output is still checked before entering config text.
        if not isinstance(files, dict) or not {'fullchain', 'privkey'} <= set(files):
            raise ValueError('Invalid certificate file capability')
        for name in (files['fullchain'], files['privkey']):
            path = Path(name)
            if not path.is_absolute() or any(c in str(path) for c in '\r\n"\\'):
                raise ValueError('Unsafe immutable certificate path')
            self.http._safe(path)
        token = _digest(json.dumps({'grant': grant, 'listener': self.listener,
            'generation': certificate['generation'], 'fingerprint': certificate['fingerprint']}, sort_keys=True))
        address, port = self.listener['address'], self.listener['port']
        host, upstream = grant['hostname'], grant['port']
        return f'''# deploy-monitor managed HTTPS route
<VirtualHost {address}:{port}>
    ServerName {host}
    SSLEngine on
    SSLProtocol -all +TLSv1.2 +TLSv1.3
    SSLCertificateFile "{files['fullchain']}"
    SSLCertificateKeyFile "{files['privkey']}"
    Header always set X-Deploy-Monitor-Route "{token}"
    ProxyRequests Off
    ProxyPreserveHost On
    ProxyAddHeaders Off
    RequestHeader unset Forwarded
    RequestHeader unset X-Forwarded-For
    RequestHeader unset X-Forwarded-Host
    RequestHeader unset X-Forwarded-Proto
    RequestHeader set X-Forwarded-Proto "https"
    ProxyPass / http://127.0.0.1:{upstream}/ connectiontimeout=5 timeout=30
    ProxyPassReverse / http://127.0.0.1:{upstream}/
</VirtualHost>
''', token

    def stage_tls(self, request):
        self.http._request(request, ('route_id', 'operation_id', 'expected_digest', 'generation', 'fingerprint'))
        with self.http._locked() as policy:
            grant = self._context(policy, request)
            certificate = self._certificate(request, grant)
            files = self.certificates.generation_files(request['route_id'], request['generation'])
            candidate, token = self._config(grant, certificate, files)
            path, journal = self._paths(policy, request['route_id'])
            state = self.http._state(journal)
            digest = _digest(candidate)
            if state and state['operation_id'] == request['operation_id'] and state['generation'] == request['generation']:
                if any(state.get(k) != v for k, v in request.items()) or state['digest'] != digest:
                    raise ValueError('TLS operation replay differs')
                return self._public(state)
            if state and state['status'] != 'active':
                raise ValueError('Prior TLS transition needs recovery')
            previous = self.http._read(path)
            if previous is not None and (not state or state['digest'] != _digest(previous)):
                raise ValueError('TLS file is unowned or changed')
            if request['expected_digest'] != (_digest(previous) if previous is not None else None):
                raise ValueError('Stale TLS configuration')
            self._collision(policy, request['route_id'], grant['hostname'], state is not None)
            state = dict(request, digest=digest, candidate=candidate, token=token,
                grant_digest=_digest(json.dumps(grant, sort_keys=True)), listener=self.listener,
                previous=previous, previous_token=state['token'] if state else None,
                previous_fingerprint=state['fingerprint'] if state else None, status='staged')
            self.http._save(journal, state)
            return self._public(state)

    def _state(self, policy, request, fields):
        path, journal = self._paths(policy, request['route_id'])
        state = self.http._state(journal)
        if (not state or any(state.get(k) != request[k] for k in fields)
                or _digest(state['candidate']) != state['digest'] or state['listener'] != self.listener
                or (_digest(state['previous']) if state['previous'] is not None else None) != state['expected_digest']):
            raise ValueError('TLS transition identity changed')
        return path, journal, state

    def _verify(self, policy, request, grant):
        path, _, state = self._state(policy, request, ('route_id', 'operation_id'))
        self._certificate(state, grant)
        if (state['status'] != 'active' or self.http._read(path) != state['candidate']
                or state['grant_digest'] != _digest(json.dumps(grant, sort_keys=True)) or not self.http.executor.running()):
            raise ValueError('TLS configuration is not current')
        self._collision(policy, request['route_id'], grant['hostname'], True)
        if self.http.executor.observe_tls(grant['hostname'], state['token'], state['fingerprint']) is not True:
            raise ValueError('Served TLS identity is not verified')
        return dict(self._public(state), observed_at=time.time())

    def activate_tls(self, request):
        self.http._request(request, ('route_id', 'operation_id', 'digest'))
        with self.http._locked() as policy:
            grant = self._context(policy, request)
            path, journal, state = self._state(policy, request, tuple(request))
            if state['status'] == 'active':
                return self._verify(policy, request, grant)
            self._certificate(state, grant)
            if state['status'] != 'staged' or self.http._read(path) != state['previous']:
                raise ValueError('TLS transition needs recovery')
            self._collision(policy, request['route_id'], grant['hostname'], state['previous'] is not None)
            if not self.http.executor.running():
                raise ValueError('Apache is not running')
            state['status'] = 'installing'; self.http._save(journal, state)
            try:
                self.http._write(path, state['candidate'])
                self.http.executor.configtest()
                self._context(policy, request)
                self._certificate(state, grant)
                if not self.http.executor.running(): raise ValueError('Apache stopped before TLS reload')
            except Exception:
                self._restore_file(path, journal, state)
                raise
            state['status'] = 'reload-intent'; self.http._save(journal, state)
            self.http.executor.reload()
            if self.http.executor.observe_tls(grant['hostname'], state['token'], state['fingerprint']) is not True:
                raise ValueError('TLS reload outcome is uncertain; inspect before retrying')
            state['status'] = 'active'; self.http._save(journal, state)
            return self._verify(policy, request, grant)

    def _restore_file(self, path, journal, state):
        if state['status'] not in ('installing', 'restoring-file') or self.http._read(path) not in (state['candidate'], state['previous']):
            raise ValueError('TLS compensation cannot establish file ownership')
        state['status'] = 'restoring-file'; self.http._save(journal, state)
        self.http._restore(path, state['previous'])
        state['status'] = 'staged'; self.http._save(journal, state)

    def recover_tls(self, request):
        self.http._request(request, ('route_id', 'operation_id'))
        with self.http._locked() as policy:
            path, journal, state = self._state(policy, request, tuple(request))
            if state['status'] in ('installing', 'restoring-file'):
                self._restore_file(path, journal, state)
                return self._public(state)
            grant = self._context(policy, request)
            if state['status'] == 'active': return self._verify(policy, request, grant)
            if state['status'] != 'reload-intent' or self.http._read(path) != state['candidate']:
                raise ValueError('TLS transition is not recoverable by observation')
            self._certificate(state, grant)
            if not self.http.executor.running() or self.http.executor.observe_tls(grant['hostname'], state['token'], state['fingerprint']) is not True:
                raise ValueError('TLS outcome remains unknown; no repeated reload is allowed')
            state['status'] = 'active'; self.http._save(journal, state)
            return self._verify(policy, request, grant)

    def verify_tls(self, request):
        self.http._request(request, ('route_id', 'operation_id'))
        with self.http._locked() as policy:
            return self._verify(policy, request, self._context(policy, request))

    def inspect_tls(self, request):
        self.http._request(request, ('route_id', 'operation_id'))
        with self.http._locked() as policy:
            self._context(policy, request)
            _, journal = self._paths(policy, request['route_id'])
            state = self.http._state(journal)
            if not state: return {'route_id': request['route_id'], 'status': 'absent'}
            if state['operation_id'] != request['operation_id']: raise ValueError('TLS operation ownership changed')
            return self._public(state)

    @staticmethod
    def _public(state):
        return {key: state[key] for key in ('route_id', 'operation_id', 'generation', 'fingerprint', 'digest', 'status')}
