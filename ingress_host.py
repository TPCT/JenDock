"""Restricted Apache HTTP lifecycle. Executor is a trusted host-side capability.

The daemon must authenticate Unix peers before exposing this adapter. Policy is
administratively installed; requests cannot grant hostnames, ports or paths.
Certificate/TLS operations are deliberately absent until their adapter exists.
"""
import contextlib
import fcntl
import fnmatch
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile

_ID = re.compile(r'[a-f0-9]{32}\Z')
_HOST = re.compile(r'(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class HostAdapter:
    """executor.inventory/running/configtest/reload are trusted, no caller args.

    inventory yields {names: [ServerName and aliases], path: effective config}.
    running must verify the configured daemon's PID AND executable identity.
    configtest tests the complete host configuration, including managed_dir.
    reload must signal that same existing daemon without starting a stopped one.
    observe(hostname, token) verifies the HTTP response identity header; True
    means verified current config, False means confirmed other config, and None
    means unknown. Unreachable/timeouts MUST return None, not False.
    observe_absence(hostname) must confirm a reachable response without a managed
    route identity for initial-install rollback. Missing/unknown evidence cannot
    complete restoration; restoring retries never issue another reload.
    Static route grants are bootstrap capabilities, not the final domain UX;
    their dynamic issuer must verify gateway and reserved-port ownership.
    """
    def __init__(self, policy_path, executor, *, owner_uid=0):
        self.policy_path = Path(policy_path)
        self.executor = executor
        self.owner_uid = owner_uid

    def _safe(self, path, *, directory=False):
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('Host paths must be absolute and normalized')
        for parent in path.parents:
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError('Host paths cannot traverse symlinks')
            # Sticky root-owned /tmp prevents other users replacing a trusted
            # owned child. No other writable ancestor receives this exception.
            sticky_tmp = parent == Path('/tmp') and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o1777
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, self.owner_uid)
                    or info.st_mode & 0o022 and not sticky_tmp):
                raise ValueError('Unsafe host filesystem ancestor ownership or permissions')
        s = path.lstat()
        if s.st_uid != self.owner_uid or s.st_mode & 0o022 or stat.S_ISLNK(s.st_mode):
            raise ValueError('Unsafe host filesystem ownership or permissions')
        if not (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode)):
            raise ValueError('Unexpected host filesystem type')
        if not directory and s.st_nlink != 1:
            raise ValueError('Host files must not have hard links')

    def _policy(self):
        self._safe(self.policy_path)
        p = json.loads(self.policy_path.read_text())
        if set(p) != {'managed_dir', 'state_dir', 'routes', 'listener'}:
            raise ValueError('Invalid host policy')
        listener = p['listener']
        if not isinstance(listener, dict) or set(listener) != {'address', 'port'} or type(listener['port']) is not int or not 1 <= listener['port'] <= 65535:
            raise ValueError('Invalid trusted Apache listener')
        ipaddress.IPv4Address(listener['address'])
        for key in ('managed_dir', 'state_dir'):
            path = Path(p[key])
            if not path.is_absolute() or '..' in path.parts:
                raise ValueError('Invalid policy path')
            self._safe(path, directory=True)
        if p['managed_dir'] == p['state_dir']:
            raise ValueError('Configuration and state must be separate')
        return p

    @contextlib.contextmanager
    def _locked(self):
        p = self._policy()
        lock = Path(p['state_dir']) / 'host.lock'
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            self._safe(lock)
            fcntl.flock(fd, fcntl.LOCK_EX)
            fresh = self._policy()
            if fresh['state_dir'] != p['state_dir'] or fresh['managed_dir'] != p['managed_dir']:
                raise ValueError('Host policy roots changed')
            yield fresh
        finally:
            os.close(fd)

    def _request(self, request, fields):
        if not isinstance(request, dict) or set(request) != set(fields):
            raise ValueError('Invalid host operation fields')
        for field in ('route_id', 'operation_id'):
            if field in request and (not isinstance(request[field], str) or not _ID.fullmatch(request[field])):
                raise ValueError('Invalid opaque identifier')

    def _grant(self, p, route):
        g = p['routes'].get(route)
        if not isinstance(g, dict) or set(g) != {'hostname', 'zone', 'port', 'revision'}:
            raise ValueError('Route has no trusted host grant')
        h, z = g['hostname'], g['zone']
        if not isinstance(h, str) or not isinstance(z, str) or not _HOST.fullmatch(h) or not _HOST.fullmatch(z) or not (h == z or h.endswith('.'+z)):
            raise ValueError('Invalid normalized allowed hostname')
        if type(g['port']) is not int or not 1024 <= g['port'] <= 65535 or type(g['revision']) is not int or g['revision'] < 1:
            raise ValueError('Invalid reserved host port or revision')
        if any(k != route and v.get('hostname') == h for k, v in p['routes'].items()):
            raise ValueError('Duplicate host grant')
        return g

    def _paths(self, p, route):
        return Path(p['managed_dir'])/(route+'.conf'), Path(p['state_dir'])/(route+'.json')

    def _read(self, path):
        if not path.exists() and not path.is_symlink():
            return None
        self._safe(path)
        return path.read_text()

    def _write(self, path, text):
        self._read(path)  # Reject links even when replacing atomically.
        fd, name = tempfile.mkstemp(dir=path.parent, prefix='.host-')
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(text); f.flush(); os.fsync(f.fileno())
            os.replace(name, path)
            d = os.open(path.parent, os.O_DIRECTORY)
            try: os.fsync(d)
            finally: os.close(d)
        finally:
            if os.path.exists(name): os.unlink(name)

    def _restore(self, path, old):
        if old is not None:
            self._write(path, old)
        elif self._read(path) is not None:
            path.unlink()
            d = os.open(path.parent, os.O_DIRECTORY)
            try: os.fsync(d)
            finally: os.close(d)

    def _state(self, path):
        raw = self._read(path)
        return json.loads(raw) if raw else None

    def _save(self, path, s):
        self._write(path, json.dumps(s, sort_keys=True))

    def _collision(self, hostname, own, *, owned=False):
        for vhost in self.executor.inventory():
            if owned and vhost['path'] == str(own):
                continue
            for name in vhost['names']:
                if fnmatch.fnmatchcase(hostname, name.lower().rstrip('.')):
                    raise ValueError('Hostname conflicts with an existing Apache vhost')

    def _config(self, g, listener):
        h, port = g['hostname'], g['port']
        token = _digest(json.dumps({'grant':g, 'listener':listener}, sort_keys=True))
        return f'''# deploy-monitor managed HTTP route
<VirtualHost {listener['address']}:{listener['port']}>
    ServerName {h}
    Header always set X-Deploy-Monitor-Route "{token}"
    ProxyRequests Off
    ProxyPreserveHost On
    ProxyAddHeaders Off
    RequestHeader unset Forwarded
    RequestHeader unset X-Forwarded-For
    RequestHeader unset X-Forwarded-Host
    RequestHeader unset X-Forwarded-Proto
    RequestHeader set X-Forwarded-Proto "http"
    ProxyPass / http://127.0.0.1:{port}/ connectiontimeout=5 timeout=30
    ProxyPassReverse / http://127.0.0.1:{port}/
</VirtualHost>
'''

    def stage_http(self, request):
        self._request(request, ('route_id', 'operation_id', 'expected_digest'))
        with self._locked() as p:
            g = self._grant(p, request['route_id'])
            path, state = self._paths(p, request['route_id'])
            s = self._state(state)
            candidate = self._config(g, p['listener'])
            digest = _digest(candidate)
            grant_digest = _digest(json.dumps({'grant':g, 'listener':p['listener']}, sort_keys=True))
            if s and s['operation_id'] == request['operation_id']:
                if s['digest'] != digest or s['grant_digest'] != grant_digest or s['expected_digest'] != request['expected_digest']:
                    raise ValueError('Operation replay differs')
                return self._public(s)
            if s and s['status'] != 'active':
                raise ValueError('Prior host operation requires recovery')
            current = self._read(path)
            if current is not None and (not s or s['digest'] != _digest(current)):
                raise ValueError('Unowned or changed managed configuration')
            if request['expected_digest'] != (_digest(current) if current is not None else None):
                raise ValueError('Stale host configuration')
            self._collision(g['hostname'], path, owned=s is not None)
            s = dict(request, digest=digest, grant_digest=grant_digest, candidate=candidate,
                     previous=current, previous_grant_digest=s['grant_digest'] if s and current is not None else None, status='staged')
            self._save(state, s)
            return self._public(s)

    def activate_http(self, request):
        self._request(request, ('route_id', 'operation_id', 'expected_digest', 'digest'))
        with self._locked() as p:
            g = self._grant(p, request['route_id'])
            path, state = self._paths(p, request['route_id'])
            s = self._state(state)
            if not s or any(s[k] != request[k] for k in request) or s['grant_digest'] != _digest(json.dumps({'grant':g, 'listener':p['listener']}, sort_keys=True)):
                raise ValueError('Stale staged host operation')
            if s['status'] == 'active':
                return self._verify(p, request['route_id'])
            if s['status'] != 'staged' or self._read(path) != s['previous']:
                raise ValueError('Host operation requires recovery')
            self._collision(g['hostname'], path, owned=s['previous'] is not None)
            if not self.executor.running():
                raise ValueError('Configured Apache daemon is not running')
            s['status'] = 'installing'; self._save(state, s)
            try:
                self._write(path, s['candidate'])
                self.executor.configtest()
            except Exception:
                self._restore(path, s['previous'])
                s['status'] = 'staged'; self._save(state, s)
                raise
            s['status'] = 'reloading'; self._save(state, s)
            try:
                if not self.executor.running(): raise ValueError('Apache stopped before reload')
                self.executor.reload()
            except Exception:
                s['status'] = 'recovery_required'; self._save(state, s)
                raise
            if self.executor.observe(g['hostname'], s['grant_digest']) is not True:
                s['status'] = 'recovery_required'; self._save(state, s)
                raise ValueError('Apache activation outcome is unverified')
            s['status'] = 'active'; self._save(state, s)
            return self._public(s)

    def _restored(self, hostname, path, state, s):
        if self._read(path) != s['previous']:
            raise ValueError('Prior configuration has not been restored on disk')
        previous = s['previous']
        if previous is None:
            observe_absence = getattr(self.executor, 'observe_absence', None)
            observed = observe_absence(hostname) if callable(observe_absence) else None
        else:
            tokens = re.findall(r'^    Header always set X-Deploy-Monitor-Route "([a-f0-9]{64})"$', previous, re.MULTILINE)
            if (_digest(previous) != s['expected_digest'] or len(tokens) != 1
                    or s.get('previous_grant_digest', tokens[0]) != tokens[0]):
                raise ValueError('Prior managed configuration identity is unavailable')
            observed = self.executor.observe(hostname, tokens[0])
        if observed is not True:
            raise ValueError('Apache restoration outcome remains unknown; no repeated reload is allowed')
        s['status'] = 'staged'; self._save(state, s)
        return self._public(s)

    def recover(self, request):
        self._request(request, ('route_id',))
        with self._locked() as p:
            path, state = self._paths(p, request['route_id']); s = self._state(state)
            if not s or s['status'] not in ('installing', 'reloading', 'recovery_required', 'restoring'):
                raise ValueError('No interrupted host operation')
            if self._read(path) not in (s['candidate'], s['previous']):
                raise ValueError('Managed configuration changed externally')
            if not self.executor.running(): raise ValueError('Apache is not running')
            g = self._grant(p, request['route_id'])
            if s['grant_digest'] != _digest(json.dumps({'grant':g, 'listener':p['listener']}, sort_keys=True)):
                raise ValueError('Host policy changed during interrupted operation')
            if s['status'] == 'restoring':
                return self._restored(g['hostname'], path, state, s)
            observed = self.executor.observe(g['hostname'], s['grant_digest'])
            if observed is None:
                raise ValueError('Apache outcome remains unknown; no recovery effects allowed')
            if observed is True and self._read(path) == s['candidate']:
                s['status'] = 'active'; self._save(state, s)
                return self._public(s)
            s['status'] = 'restoring'; self._save(state, s)
            self._restore(path, s['previous'])
            self.executor.configtest()
            if not self.executor.running(): raise ValueError('Apache stopped before reload')
            self.executor.reload()
            return self._restored(g['hostname'], path, state, s)

    def _public(self, s):
        return {k:s[k] for k in ('route_id', 'operation_id', 'digest', 'status')}

    def inspect(self, request):
        self._request(request, ('route_id',))
        with self._locked() as p:
            _, state = self._paths(p, request['route_id']); s = self._state(state)
            return self._public(s) if s else {'route_id':request['route_id'], 'status':'absent'}

    def _verify(self, p, route):
        g = self._grant(p, route)
        path, state = self._paths(p, route); s = self._state(state)
        if not s or s['status'] != 'active' or self._read(path) != s['candidate'] or s['grant_digest'] != _digest(json.dumps({'grant':g, 'listener':p['listener']}, sort_keys=True)) or not self.executor.running():
            raise ValueError('HTTP configuration is not current')
        self._collision(g['hostname'], path, owned=True)
        if self.executor.observe(g['hostname'], s['grant_digest']) is not True:
            raise ValueError('Apache HTTP configuration identity is unverified')
        return self._public(s)

    def verify(self, request):
        self._request(request, ('route_id',))
        with self._locked() as p:
            return self._verify(p, request['route_id'])
