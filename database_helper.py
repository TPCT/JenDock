"""Bounded private transport for the trusted database inspector image.

This is internal worker infrastructure, not a public inspection API. Callers must
resolve authorization and database ownership before and after every invocation.
"""
import json
import os
import re
import selectors
import signal
import subprocess
import time
import uuid
import stat
from pathlib import Path

MAX_REQUEST = 32768
MAX_RESPONSE = 262144

class HelperError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('Database inspection helper could not complete: ' + code + '.')


def exchange(argv, payload=b'', *, timeout=20, env=None, limit=MAX_RESPONSE):
    """Bound both streams and stdin without buffering arbitrary process output."""
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env, start_new_session=True, close_fds=True)
    selector = selectors.DefaultSelector()
    output = bytearray(); errors = 0; offset = 0
    deadline = time.monotonic() + timeout
    try:
        for stream in (process.stdin, process.stdout, process.stderr): os.set_blocking(stream.fileno(), False)
        if payload: selector.register(process.stdin, selectors.EVENT_WRITE)
        else: process.stdin.close()
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise HelperError('timeout')
            for key, event in selector.select(min(.1, remaining)):
                if key.fileobj is process.stdin:
                    try: offset += os.write(key.fd, payload[offset:offset+4096])
                    except BrokenPipeError: offset = len(payload)
                    if offset == len(payload):
                        selector.unregister(key.fileobj); process.stdin.close()
                else:
                    chunk = os.read(key.fd, 8192)
                    if not chunk: selector.unregister(key.fileobj)
                    elif key.fileobj is process.stdout:
                        output.extend(chunk)
                        if len(output) > limit: raise HelperError('response_limit')
                    else:
                        errors += len(chunk)
                        if errors > 65536: raise HelperError('response_limit')
        try: code = process.wait(timeout=max(.001, deadline-time.monotonic()))
        except subprocess.TimeoutExpired: raise HelperError('timeout') from None
        if code != 0:
            raise HelperError('execution_failed')
        return bytes(output)
    except BaseException:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.wait(timeout=5)
        raise
    finally:
        selector.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed: stream.close()


class InspectorHelper:
    def __init__(self, *, docker, socket, image, state_dir, runner=exchange):
        if (not isinstance(docker, str) or not docker.startswith('/') or
                not isinstance(socket, str) or not socket.startswith('unix:///') or
                not isinstance(image, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image)):
            raise HelperError('invalid_configuration')
        self.state_dir = Path(state_dir)
        info = self.state_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise HelperError('invalid_state_directory')
        self.docker, self.image, self.runner = docker, image, runner
        self.env = {'PATH':'/usr/local/bin:/usr/bin:/bin', 'DOCKER_HOST':socket}

    def _run(self, args, payload=b'', timeout=10):
        return self.runner([self.docker, *args], payload, timeout=timeout, env=self.env)

    def _owned(self, name, nonce, *, target=None, ended=False):
        try:
            values = json.loads(self._run(['inspect', name]))
            row = values[0]
            identity = row['Id']
            if (len(values) != 1 or not re.fullmatch('[a-f0-9]{64}', identity) or
                    row['Name'] != '/' + name or row['Image'] != self.image or
                    row['Config']['Labels'].get('net.shiftcodes.database-inspection') != nonce):
                raise HelperError('helper_identity_changed')
            if target is not None:
                config, host = row['Config'], row['HostConfig']
                if (host.get('NetworkMode') != 'container:' + target or host.get('ReadonlyRootfs') is not True or
                        host.get('CapDrop') != ['ALL'] or host.get('CapAdd') or
                        host.get('Privileged') or host.get('Binds') or row.get('Mounts') or
                        host.get('SecurityOpt') != ['no-new-privileges:true'] or
                        host.get('Memory') != 128*1024*1024 or host.get('NanoCpus') != 250000000 or
                        host.get('PidsLimit') != 32 or host.get('LogConfig',{}).get('Type') != 'none' or
                        config.get('User') != '65534:65534' or config.get('Entrypoint') != ['python'] or
                        config.get('Cmd') != ['/app/database_inspector_entry.py']):
                    raise HelperError('helper_configuration_changed')
            if ended and (row['State'].get('Running') or row['State'].get('Status') != 'exited' or row['State'].get('ExitCode') != 0 or row['State'].get('OOMKilled')):
                raise HelperError('helper_not_completed')
            return identity
        except (ValueError, KeyError, TypeError, IndexError):
            raise HelperError('helper_identity_unavailable') from None

    def inspect(self, container_id, request, *, revalidate):
        if not isinstance(container_id, str) or not re.fullmatch('[a-f0-9]{64}', container_id):
            raise HelperError('invalid_target')
        try: payload = json.dumps(request, allow_nan=False, separators=(',', ':')).encode()
        except (ValueError, TypeError): raise HelperError('invalid_request') from None
        if len(payload) > MAX_REQUEST: raise HelperError('request_limit')
        revalidate(container_id)
        if any(self.state_dir.iterdir()): raise HelperError('helper_recovery_required')
        nonce = uuid.uuid4().hex; name = 'deploy-monitor-db-inspect-' + nonce
        journal = self.state_dir / 'active.json'
        try: fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError: raise HelperError('helper_recovery_required') from None
        with os.fdopen(fd, 'w') as stream:
            json.dump({'name':name,'nonce':nonce,'image':self.image,'target':container_id}, stream)
            stream.flush(); os.fsync(stream.fileno())
        directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
        attempted = False; identity = None; result = None
        try:
            attempted = True
            self._run(['create', '--name', name, '--label', 'net.shiftcodes.database-inspection=' + nonce,
                '--network', 'container:' + container_id, '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges:true', '--user', '65534:65534',
                '--memory', '128m', '--cpus', '0.25', '--pids-limit', '32', '--log-driver', 'none',
                '--interactive', '--entrypoint', 'python', self.image, '/app/database_inspector_entry.py'])
            identity = self._owned(name, nonce, target=container_id)
            revalidate(container_id)
            raw = self._run(['start', '--attach', '--interactive', identity], payload, timeout=20)
            self._owned(name, nonce, target=container_id, ended=True)
            def pairs(items):
                value = {}
                for key, item in items:
                    if key in value: raise ValueError()
                    value[key] = item
                return value
            def constant(_): raise ValueError()
            try: result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
            except (ValueError, UnicodeError): raise HelperError('invalid_response') from None
            if not isinstance(result, dict): raise HelperError('invalid_response')
            revalidate(container_id)
        finally:
            if attempted:
                # Reconcile an ambiguous create by the private nonce before removal.
                owned = self._owned(name, nonce)
                if identity is not None and identity != owned: raise HelperError('helper_identity_changed')
                self._remove(owned, journal)
        return result

    def _remove(self, identity, journal):
        self._run(['rm', '--force', identity])
        if self._run(['ps', '--all', '--quiet', '--no-trunc', '--filter', 'id=' + identity]).strip():
            raise HelperError('cleanup_unverified')
        journal.unlink()
        directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)

    def reconcile_pending(self):
        """Remove only an observable exact owned helper; missing/ambiguous stays pending."""
        journal = self.state_dir / 'active.json'
        if not journal.exists(): return False
        fd = os.open(journal, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
                raise HelperError('invalid_recovery_record')
            raw = stream.read(4097)
        try:
            value = json.loads(raw)
            if (len(raw)>4096 or set(value)!={'name','nonce','image','target'} or value['image']!=self.image or
                    not re.fullmatch('[a-f0-9]{32}',value['nonce']) or
                    value['name']!='deploy-monitor-db-inspect-'+value['nonce'] or
                    not re.fullmatch('[a-f0-9]{64}',value['target'])): raise ValueError()
        except (ValueError,TypeError,KeyError): raise HelperError('invalid_recovery_record') from None
        identity = self._owned(value['name'], value['nonce'])
        self._remove(identity, journal)
        return True
