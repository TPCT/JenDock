"""Private fixed-image migration helper with bounded duplex authorization."""
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import time
import uuid
from pathlib import Path
from database_helper import exchange

MAX_REQUEST=24*1024*1024
MAX_RESPONSE=4*1024*1024

class RunnerError(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__('Migration helper could not complete: '+code+'.')

HelperError=RunnerError


def decode(raw):
    def pairs(items):
        result={}
        for k,v in items:
            if k in result:raise ValueError()
            result[k]=v
        return result
    def invalid(_):raise ValueError()
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=invalid)


def dialogue(argv,payload,*,authorize,timeout=120,env=None):
    if not callable(authorize) or len(payload)>MAX_REQUEST or not payload.endswith(b'\n'):raise RunnerError('invalid_request')
    process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,start_new_session=True,close_fds=True)
    selector=selectors.DefaultSelector();pending=bytearray(payload);buffer=bytearray();size=0;errors=0;sequence=0;terminal=None
    deadline=time.monotonic()+timeout
    try:
        for stream in (process.stdin,process.stdout,process.stderr):os.set_blocking(stream.fileno(),False)
        selector.register(process.stdin,selectors.EVENT_WRITE)
        selector.register(process.stdout,selectors.EVENT_READ);selector.register(process.stderr,selectors.EVENT_READ)
        while selector.get_map():
            if time.monotonic()>=deadline:raise RunnerError('timeout')
            for key,_ in selector.select(min(.1,max(0,deadline-time.monotonic()))):
                if key.fileobj is process.stdin:
                    try:written=os.write(key.fd,pending[:8192]);del pending[:written]
                    except BrokenPipeError:raise RunnerError('protocol_failed') from None
                    if not pending:selector.unregister(process.stdin)
                    continue
                chunk=os.read(key.fd,8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.fileobj is process.stderr:
                    errors+=len(chunk)
                    if errors>65536:raise RunnerError('output_limit')
                    continue
                size+=len(chunk);buffer.extend(chunk)
                if size>MAX_RESPONSE+10000*64:raise RunnerError('output_limit')
                if len(buffer)>MAX_RESPONSE:raise RunnerError('output_limit')
                while b'\n' in buffer:
                    line,_,remaining=buffer.partition(b'\n');buffer=bytearray(remaining)
                    try:frame=decode(line)
                    except (ValueError,UnicodeError):raise RunnerError('invalid_response') from None
                    if not isinstance(frame,dict) or terminal is not None:raise RunnerError('invalid_response')
                    if set(frame)=={'event','sequence'} and frame['event']=='authorize':
                        if type(frame['sequence']) is not int or frame['sequence']!=sequence+1 or sequence>=10000 or pending:raise RunnerError('invalid_response')
                        try:authorize()
                        except Exception:raise RunnerError('authority_changed') from None
                        sequence+=1;pending.extend(json.dumps({'sequence':sequence,'authorized':True}).encode()+b'\n')
                        selector.register(process.stdin,selectors.EVENT_WRITE)
                    elif set(frame)=={'result'} and isinstance(frame['result'],dict) and sequence:
                        terminal=frame
                    elif set(frame)=={'error'} and isinstance(frame['error'],str) and re.fullmatch('[a-z_]{1,80}',frame['error']):terminal=frame
                    else:raise RunnerError('invalid_response')
        try:exit_code=process.wait(timeout=max(.001,deadline-time.monotonic()))
        except subprocess.TimeoutExpired:raise RunnerError('timeout') from None
        if exit_code!=0 or buffer or pending or terminal is None:raise RunnerError('protocol_failed')
        if 'error' in terminal:raise RunnerError(terminal['error'])
        return terminal['result']
    except BaseException:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.wait(timeout=5)
        raise
    finally:
        selector.close()
        for stream in (process.stdin,process.stdout,process.stderr):stream.close()


class MigrationHelper:
    def __init__(self, *, docker, socket, image, state_dir, runner=exchange, session_runner=dialogue):
        if (not isinstance(docker, str) or not docker.startswith('/') or
                not isinstance(socket, str) or not socket.startswith('unix:///') or
                not isinstance(image, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image)):
            raise HelperError('invalid_configuration')
        self.state_dir = Path(state_dir)
        info = self.state_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise HelperError('invalid_state_directory')
        self.docker, self.image, self.runner = docker, image, runner
        self.session_runner=session_runner
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
                    row['Config']['Labels'].get('net.shiftcodes.database-migration') != nonce):
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
                        config.get('Cmd') != ['/app/migration_helper_entry.py']):
                    raise HelperError('helper_configuration_changed')
            if ended and (row['State'].get('Running') or row['State'].get('Status') != 'exited' or row['State'].get('ExitCode') != 0 or row['State'].get('OOMKilled')):
                raise HelperError('helper_not_completed')
            return identity
        except (ValueError, KeyError, TypeError, IndexError):
            raise HelperError('helper_identity_unavailable') from None

    def invoke(self, container_id, request, *, revalidate, authorize):
        if not isinstance(container_id, str) or not re.fullmatch('[a-f0-9]{64}', container_id):
            raise HelperError('invalid_target')
        try: payload = json.dumps(request, allow_nan=False, separators=(',', ':')).encode()+b'\n'
        except (ValueError, TypeError): raise HelperError('invalid_request') from None
        if len(payload) > MAX_REQUEST: raise HelperError('request_limit')
        revalidate(container_id)
        if any(self.state_dir.iterdir()): raise HelperError('helper_recovery_required')
        nonce = uuid.uuid4().hex; name = 'deploy-monitor-db-migrate-' + nonce
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
            self._run(['create', '--name', name, '--label', 'net.shiftcodes.database-migration=' + nonce,
                '--network', 'container:' + container_id, '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges:true', '--user', '65534:65534',
                '--memory', '128m', '--cpus', '0.25', '--pids-limit', '32', '--log-driver', 'none',
                '--interactive', '--entrypoint', 'python', self.image, '/app/migration_helper_entry.py'])
            identity = self._owned(name, nonce, target=container_id)
            revalidate(container_id)
            result=self.session_runner([self.docker,'start','--attach','--interactive',identity],payload,timeout=120,env=self.env,authorize=authorize)
            self._owned(name,nonce,target=container_id,ended=True)
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
                    value['name']!='deploy-monitor-db-migrate-'+value['nonce'] or
                    not re.fullmatch('[a-f0-9]{64}',value['target'])): raise ValueError()
        except (ValueError,TypeError,KeyError): raise HelperError('invalid_recovery_record') from None
        identity = self._owned(value['name'], value['nonce'])
        self._remove(identity, journal)
        return True
