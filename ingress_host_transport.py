"""Bounded Linux Unix-socket transport for an injected trusted HostAdapter.

One connection carries one request. No retry, TCP, dynamic dispatch, filesystem
paths or executable selection is accepted from the caller. Socket setup belongs
to the administrator; this module neither installs nor starts a daemon.
"""
import json
import math
import os
from pathlib import Path
import socket
import stat
import struct
import time

from ingress_host_service import HostService

MAX_FRAME = 65536
METHODS = frozenset({'stage_http', 'activate_http', 'inspect', 'verify', 'recover'})
SERVICE_METHODS = METHODS | {'issue_grant'}
ERRORS = frozenset({'unauthorized', 'invalid_request', 'operation_failed'})


class TransportError(RuntimeError):
    """A lost reply may follow a committed effect. Never automatically retry."""


def _timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 60:
        raise ValueError('Invalid transport deadline')
    return value


def _remaining(sock, deadline):
    remaining = deadline-time.monotonic()
    if remaining <= 0: raise TimeoutError('Transport deadline expired')
    sock.settimeout(remaining)


def _read_exact(sock, size, deadline):
    result = bytearray()
    while len(result) < size:
        _remaining(sock, deadline)
        chunk = sock.recv(size-len(result))
        if not chunk: raise ValueError('Truncated frame')
        result.extend(chunk)
    return bytes(result)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


def _read(sock, deadline):
    size = struct.unpack('!I', _read_exact(sock, 4, deadline))[0]
    if not 0 < size <= MAX_FRAME: raise ValueError('Invalid frame size')
    return json.loads(_read_exact(sock, size, deadline).decode('utf-8'), object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON constant')))


def _send(sock, value, deadline):
    payload = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    if len(payload) > MAX_FRAME: raise ValueError('Response exceeds frame bound')
    _remaining(sock, deadline)
    sock.sendall(struct.pack('!I', len(payload))+payload)


class HostServer:
    def __init__(self, path, adapter, *, allowed_uids, owner_uid=0, group_gid=None, timeout=5):
        self.path = Path(path)
        self.adapter = adapter
        self.methods = SERVICE_METHODS if isinstance(adapter, HostService) else METHODS
        self.allowed_uids = frozenset(allowed_uids)
        if not self.allowed_uids or any(type(uid) is not int or uid < 0 for uid in self.allowed_uids):
            raise ValueError('Exact peer UID allowlist is required')
        if type(owner_uid) is not int or owner_uid < 0 or (group_gid is not None and (type(group_gid) is not int or group_gid < 0)):
            raise ValueError('Invalid socket owner/group')
        self.owner_uid, self.group_gid = owner_uid, group_gid
        self.timeout = _timeout(timeout)
        self.socket = None
        self.identity = None

    def open(self):
        if self.socket is not None: raise ValueError('Server already open')
        if not self.path.is_absolute() or '..' in self.path.parts: raise ValueError('Invalid configured socket path')
        for p in self.path.parents:
            info = p.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid not in (0, self.owner_uid):
                raise ValueError('Socket ancestors must be trusted directories')
            sticky_root = p == Path('/tmp') and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o1777
            if info.st_mode & 0o022 and not sticky_root:
                raise ValueError('Socket ancestors cannot be writable by other users')
        parent = self.path.parent.stat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != self.owner_uid or parent.st_mode & 0o022:
            raise ValueError('Socket directory must have trusted ownership and permissions')
        if os.path.lexists(self.path): raise ValueError('Refusing to replace existing socket path')
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Parent is not writable by peers. Before listen(), apply restrictive
            # permissions; an unprivileged peer cannot connect during installation.
            listener.bind(str(self.path))
            s = self.path.lstat(); self.identity = (s.st_dev, s.st_ino)
            if s.st_uid != self.owner_uid: raise ValueError('Socket owner differs from trusted owner')
            if self.group_gid is not None: os.chown(self.path, -1, self.group_gid)
            os.chmod(self.path, 0o660 if self.group_gid is not None else 0o600)
            listener.listen(8)
            listener.settimeout(self.timeout)
            self.socket = listener
        except Exception:
            listener.close(); self._unlink_owned(); raise
        return self

    def _unlink_owned(self):
        try: s = self.path.lstat()
        except FileNotFoundError: return
        if self.identity == (s.st_dev, s.st_ino) and stat.S_ISSOCK(s.st_mode):
            self.path.unlink()
        self.identity = None

    def close(self):
        if self.socket is not None: self.socket.close(); self.socket = None
        self._unlink_owned()

    def serve_once(self):
        if self.socket is None: raise ValueError('Server is not open')
        try: connection, _ = self.socket.accept()
        except socket.timeout: return False
        with connection:
            deadline = time.monotonic()+self.timeout
            try:
                _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                if uid not in self.allowed_uids:
                    response = {'ok':False, 'error':'unauthorized'}
                else:
                    try:
                        envelope = _read(connection, deadline)
                        if not isinstance(envelope, dict) or set(envelope) != {'method', 'request'} or not isinstance(envelope['method'], str) or envelope['method'] not in self.methods or not isinstance(envelope['request'], dict):
                            raise ValueError('Invalid request envelope')
                    except (ValueError, UnicodeError, RecursionError, TimeoutError):
                        response = {'ok':False, 'error':'invalid_request'}
                    else:
                        try:
                            # Closed capability mapping, never arbitrary attribute access.
                            method = envelope['method']
                            if method == 'inspect': result = self.adapter.inspect(envelope['request'])
                            elif method == 'verify': result = self.adapter.verify(envelope['request'])
                            elif method == 'stage_http': result = self.adapter.stage_http(envelope['request'])
                            elif method == 'activate_http': result = self.adapter.activate_http(envelope['request'])
                            elif method == 'recover': result = self.adapter.recover(envelope['request'])
                            elif method == 'issue_grant' and isinstance(self.adapter, HostService): result = self.adapter.issue_grant(envelope['request'])
                            else: raise ValueError('Unsupported host capability')
                            response = {'ok':True, 'result':result}
                            if len(json.dumps(response, allow_nan=False).encode()) > MAX_FRAME: raise ValueError('Oversized response')
                        except Exception:
                            response = {'ok':False, 'error':'operation_failed'}
                _send(connection, response, deadline)
            except (OSError, ValueError, TimeoutError):
                pass  # Caller may be gone after a committed mutation; no retry.
        return True


class HostClient:
    def __init__(self, path, *, timeout=5, expected_server_uid=0):
        if type(expected_server_uid) is not int or expected_server_uid < 0:
            raise ValueError('Exact trusted server UID is required')
        self.expected_server_uid = expected_server_uid
        self.path = str(path)
        self.timeout = _timeout(timeout)

    def call(self, method, request):
        if not isinstance(method, str) or method not in SERVICE_METHODS or not isinstance(request, dict):
            raise ValueError('Invalid host method or request')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            deadline = time.monotonic()+self.timeout
            try:
                _remaining(connection, deadline); connection.connect(self.path)
                _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                if uid != self.expected_server_uid:
                    raise TransportError('Untrusted host helper identity')
                _send(connection, {'method':method, 'request':request}, deadline)
                response = _read(connection, deadline)
                if not isinstance(response, dict): raise ValueError('Invalid response')
                if response.get('ok') is True and set(response) == {'ok', 'result'}:
                    return response['result']
                if response.get('ok') is False and set(response) == {'ok', 'error'} and response['error'] in ERRORS:
                    raise TransportError(response['error'])
                raise ValueError('Invalid response')
            except TransportError: raise
            except (OSError, ValueError, UnicodeError, RecursionError, TypeError):
                raise TransportError('Host reply unavailable; outcome unknown. Inspect before retrying.') from None
