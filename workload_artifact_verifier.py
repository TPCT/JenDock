"""Worker-owned isolated image verification. Never accepts candidate proof flags.

The filesystem journal is private, fsynced and serialized across processes. Failed
immutable candidates are terminal; unknown Docker creates remain fenced even when
not currently observable. Recovery only cleans exact owned resources, never starts
or retries application code. No SQLite transaction may span a Docker operation.
"""
import base64
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import uuid

from database_helper import exchange
import managed_services
import workload_package_verifier as package
import workload_protocol

VERSION = 1
LABEL = 'net.shiftcodes.workload-verifier'
ID = r'[a-f0-9]{64}'
PIN = r'(?:[A-Za-z0-9][A-Za-z0-9._:/-]*@)?sha256:[a-f0-9]{64}'
MAX_JOURNAL = 32768
SQL_SCHEMA = """
CREATE TABLE jobs(id bigserial PRIMARY KEY,queue varchar(255) NOT NULL,payload text NOT NULL,attempts smallint NOT NULL,reserved_at integer,available_at integer NOT NULL,created_at integer NOT NULL);
CREATE INDEX jobs_queue ON jobs(queue);
CREATE TABLE failed_jobs(id bigserial PRIMARY KEY,uuid varchar(255) NOT NULL UNIQUE,connection text NOT NULL,queue text NOT NULL,payload text NOT NULL,exception text NOT NULL,failed_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE dm_workload_heartbeats(scope_id varchar(64) NOT NULL,instance_id varchar(100) NOT NULL,observed_at timestamptz NOT NULL,last_outcome varchar(16),PRIMARY KEY(scope_id,instance_id));
CREATE TABLE dm_workload_registry(scope_id varchar(64) PRIMARY KEY,registered_at timestamptz NOT NULL);
CREATE TABLE dm_workload_schedules(scope_id varchar(64) NOT NULL,schedule_id varchar(64) NOT NULL,name varchar(100) NOT NULL,expression varchar(100) NOT NULL,timezone varchar(100) NOT NULL,without_overlapping boolean NOT NULL,last_outcome varchar(16),PRIMARY KEY(scope_id,schedule_id));
CREATE TABLE dm_workload_events(id bigserial PRIMARY KEY,scope_id varchar(64) NOT NULL,instance_id varchar(100) NOT NULL,schedule_id varchar(64),job_id varchar(100),outcome varchar(16) NOT NULL,observed_at timestamptz NOT NULL);
CREATE INDEX dm_workload_events_scope ON dm_workload_events(scope_id,id);
CREATE TABLE dm_workload_retries(scope_id varchar(64) NOT NULL,request_id varchar(80) NOT NULL,request_hash varchar(64) NOT NULL,failure_id varchar(36) NOT NULL UNIQUE,result jsonb NOT NULL,PRIMARY KEY(scope_id,request_id));
CREATE TABLE verifier_sentinel(value text PRIMARY KEY);
"""
TABLES = ('jobs', 'failed_jobs', 'dm_workload_heartbeats', 'dm_workload_registry',
          'dm_workload_schedules', 'dm_workload_events', 'dm_workload_retries')


class VerificationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('Workload image verification could not complete: ' + code + '.')


def require(ok, code='invalid_configuration'):
    if not ok:
        raise VerificationError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise VerificationError('invalid_response') from None


def safe_path(path, *, directory=False):
    p = Path(path)
    require(p.is_absolute() and str(p) == str(path) and '..' not in p.parts)
    for parent in reversed(p.parents):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, os.geteuid()))
        require(not info.st_mode & 0o022 or str(parent) == '/tmp' and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o1777)
    info = p.lstat()
    sticky_parent = directory and str(p) == '/tmp' and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o1777
    require(info.st_uid in (0, os.geteuid()) and (not info.st_mode & 0o022 or sticky_parent))
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    return p


def single_json_archive(raw):
    """One bounded regular USTAR file from the fixed Composer metadata path."""
    require(type(raw) is bytes and 1024 <= len(raw) <= 2 * 1024 * 1024 and len(raw) % 512 == 0, 'invalid_framework_metadata')
    h = raw[:512]
    try:
        require(h[257:265] == b'ustar\x0000' and h[156:157] in (b'0', b'\0') and not any(h[157:257]) and not any(h[345:]), 'invalid_framework_metadata')
        require(package._text(h[:100]) == 'installed.json', 'invalid_framework_metadata')
        require(package._number(h[148:156]) == sum(h[:148]) + 256 + sum(h[156:]), 'invalid_framework_metadata')
        size = package._number(h[124:136])
        end = 512 + ((size + 511) // 512) * 512
        require(size <= 1024 * 1024 and end + 1024 <= len(raw) and not any(raw[512 + size:]), 'invalid_framework_metadata')
        return decode(raw[512:512 + size])
    except package.PackageVerificationError:
        raise VerificationError('invalid_framework_metadata') from None


class WorkloadArtifactVerifier:
    def __init__(self, *, docker, socket, postgres_image, state_dir, runner=exchange):
        try:
            safe_path(docker)
            require(os.access(docker, os.X_OK))
            require(type(socket) is str and socket.startswith('unix:///'))
            sp = Path(socket[7:])
            # Standard host alias only; arbitrary ancestor links remain forbidden.
            if str(sp).startswith('/var/run/'):
                alias = Path('/var/run').lstat()
                require(stat.S_ISLNK(alias.st_mode) and alias.st_uid == 0 and os.readlink('/var/run') in ('/run', '../run'))
                sp = Path('/run') / sp.relative_to('/var/run')
                socket = 'unix://' + str(sp)
            safe_path(sp.parent, directory=True)
            si = sp.lstat()
            require(stat.S_ISSOCK(si.st_mode) and si.st_uid in (0, os.geteuid()) and not si.st_mode & 0o002)
            require(type(postgres_image) is str and re.fullmatch(PIN, postgres_image))
            state = Path(state_dir)
            safe_path(state.parent, directory=True)
            try:
                state.mkdir(mode=0o700)
                parent = os.open(state.parent, os.O_DIRECTORY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            except FileExistsError:
                pass
            self.root = safe_path(state, directory=True)
            require(self.root.stat().st_uid == os.geteuid() and not self.root.stat().st_mode & 0o077)
        except (OSError, TypeError):
            raise VerificationError('invalid_configuration') from None
        self.docker, self.postgres_image, self.runner = docker, postgres_image, runner
        self.env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'DOCKER_HOST': socket}
        self.deadline = None

    @contextlib.contextmanager
    def _lock(self):
        fd = os.open(self.root / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077 and info.st_nlink == 1)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise VerificationError('verifier_busy') from None
            yield
        finally:
            os.close(fd)

    def _write(self, journal):
        raw = canonical(journal).encode()
        require(len(raw) <= MAX_JOURNAL, 'journal_limit')
        path = self.root / (journal['key'] + '.json')
        temp = self.root / (journal['key'] + '.' + uuid.uuid4().hex + '.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, path)
            directory = os.open(self.root, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temp.exists():
                temp.unlink()

    def _load(self, path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077 and info.st_nlink == 1 and info.st_size <= MAX_JOURNAL, 'invalid_journal')
            j = decode(stream.read(MAX_JOURNAL + 1))
        require(type(j) is dict and type(j.get('key')) is str and j['key'] + '.json' == path.name and j.get('version') == VERSION, 'invalid_journal')
        require(re.fullmatch(ID, j['key']) and re.fullmatch('[a-f0-9]{32}', j.get('nonce', '')), 'invalid_journal')
        require(j.get('state') in ('running', 'cleanup-required', 'failed', 'verified') and type(j.get('resources')) is list and len(j['resources']) <= 4, 'invalid_journal')
        for r in j['resources']:
            require(type(r) is dict and r.get('role') in ('inspection', 'database', 'capabilities', 'observation') and r.get('name') == 'deploy-monitor-verify-' + j['nonce'] + '-' + r['role'] and re.fullmatch('sha256:' + ID, r.get('image', '')), 'invalid_journal')
            require(r.get('state') in ('creating', 'created', 'removing', 'removed') and (r.get('id') is None or re.fullmatch(ID, r['id'])), 'invalid_journal')
        return j

    def _run(self, args, payload=b'', *, timeout=15, limit=262144, extra_env=None):
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
        require(timeout > 0, 'verification_timeout')
        try:
            output = self.runner([self.docker, *args], payload, timeout=timeout,
                                 limit=limit, env=dict(self.env, **(extra_env or {})))
            require(type(output) is bytes and len(output) <= limit, 'response_limit')
            return output
        except VerificationError:
            raise
        except Exception:
            raise VerificationError('docker_operation_failed') from None

    def _inspect_image(self, image):
        rows = decode(self._run(['image', 'inspect', image], limit=1048576))
        require(type(rows) is list and len(rows) == 1 and re.fullmatch('sha256:' + ID, rows[0].get('Id', '')), 'image_identity_unavailable')
        row = rows[0]
        require(row['Id'] == image if image.startswith('sha256:') else image in row.get('RepoDigests', []), 'image_identity_changed')
        require(type(row.get('Config')) is dict, 'image_identity_unavailable')
        return row

    def _ids(self, name):
        raw = self._run(['ps', '--all', '--quiet', '--no-trunc', '--filter', 'name=^/' + name + '$'], limit=1024)
        ids = raw.decode('ascii').split()
        require(len(ids) <= 1 and all(re.fullmatch(ID, i) for i in ids), 'resource_ambiguous')
        return ids

    def _owned(self, j, r):
        ids = self._ids(r['name'])
        if not ids:
            return None
        rows = decode(self._run(['inspect', ids[0]], limit=1048576))
        require(type(rows) is list and len(rows) == 1, 'resource_identity_changed')
        row = rows[0]
        labels = row.get('Config', {}).get('Labels', {})
        require(row.get('Id') == ids[0] and (r['id'] is None or ids[0] == r['id']) and row.get('Name') == '/' + r['name'] and row.get('Image') == r['image'] and all(labels.get(k) == v for k, v in self._labels(j, r['role']).items()), 'resource_identity_changed')
        return row

    def _labels(self, j, role):
        return {'com.docker.compose.project': 'deploy-monitor', LABEL: j['nonce'], LABEL + '.key': j['key'], LABEL + '.role': role}

    def _create(self, j, role, image, args, env=None):
        r = {'role': role, 'name': 'deploy-monitor-verify-' + j['nonce'] + '-' + role,
             'image': image, 'id': None, 'state': 'creating'}
        # Name must be demonstrably absent before creation intent.
        require(not self._ids(r['name']), 'resource_name_conflict')
        j['resources'].append(r); self._write(j)
        labels = [a for k, v in self._labels(j, role).items() for a in ('--label', k + '=' + v)]
        raw = self._run(['create', '--name', r['name'], *labels, *args], extra_env=env)
        identity = raw.decode('ascii').strip()
        require(re.fullmatch(ID, identity), 'create_outcome_unknown')
        row = self._owned(j, r)
        require(row is not None and row['Id'] == identity, 'create_outcome_unknown')
        r.update(id=identity, state='created'); self._write(j)
        return r

    def _security(self, j, r, *, network, user, entrypoint, command, tmpfs, memory, cpus, pids, running=None):
        row = self._owned(j, r)
        require(row is not None, 'resource_missing')
        c, h = row['Config'], row['HostConfig']
        require(h.get('NetworkMode') == network and h.get('ReadonlyRootfs') is True and h.get('CapDrop') == ['ALL'] and not h.get('CapAdd') and not h.get('Privileged') and not h.get('Binds') and not h.get('Devices') and not h.get('DeviceRequests') and not h.get('PortBindings') and not h.get('PublishAllPorts') and not h.get('Links') and not h.get('ExtraHosts') and h.get('SecurityOpt') == ['no-new-privileges:true'] and h.get('Memory') == memory and h.get('NanoCpus') == cpus and h.get('PidsLimit') == pids and h.get('LogConfig', {}).get('Type') == 'none', 'unsafe_resource_configuration')
        require(c.get('User') == user and c.get('Entrypoint') == entrypoint and c.get('Cmd') == command and c.get('Healthcheck', {}).get('Test') == ['NONE'] and h.get('Tmpfs', {}) == tmpfs, 'unsafe_resource_configuration')
        require(all(m.get('Type') == 'tmpfs' and m.get('Destination') in tmpfs for m in row.get('Mounts', [])), 'unsafe_resource_configuration')
        ns = row.get('NetworkSettings', {})
        networks = ns.get('Networks')
        require(type(networks) is dict and set(networks) == ({'none'} if network == 'none' else set()), 'unsafe_resource_networks')
        for entry in [ns, *networks.values()]:
            require(type(entry) is dict and not any(entry.get(k) for k in ('IPAddress', 'GlobalIPv6Address', 'Gateway', 'IPv6Gateway')), 'unsafe_resource_networks')
        require(not ns.get('Ports') or all(v is None for v in ns['Ports'].values()), 'unsafe_resource_configuration')
        if running is not None:
            require(row['State'].get('Running') is running, 'unexpected_resource_state')
        return row

    def _cleanup(self, j):
        # Separate bounded cleanup budget even if the verification deadline elapsed.
        self.deadline = time.monotonic() + 90
        j['state'] = 'cleanup-required'; self._write(j)
        for r in reversed(j['resources']):
            if r['state'] == 'removed':
                require(not self._ids(r['name']), 'removed_resource_reappeared')
                continue
            row = self._owned(j, r)
            if row is None:
                # A lost create response can arrive later: absence is not completion.
                require(r['state'] == 'removing' and r['id'] is not None, 'create_outcome_unknown')
            else:
                r.update(id=row['Id'], state='removing'); self._write(j)
                self._run(['rm', '--force', r['id']], timeout=20)
                require(not self._ids(r['name']), 'cleanup_unverified')
                require(not self._run(['ps', '--all', '--quiet', '--no-trunc', '--filter', 'id=' + r['id']], limit=1024).strip(), 'cleanup_unverified')
            r['state'] = 'removed'; self._write(j)
        j['state'] = 'failed'; self._write(j)

    def protected_images(self):
        with self._lock():
            return sorted({r['image'] for p in self.root.glob('*.json') for j in [self._load(p)] if j['state'] in ('running', 'cleanup-required') for r in j['resources']})

    def has_pending_cleanup(self):
        with self._lock():
            return any(self._load(p)['state'] in ('running', 'cleanup-required') for p in self.root.glob('*.json'))

    def recover_pending(self, db):
        require(not db.in_transaction, 'transaction_open')
        with self._lock():
            for path in self.root.glob('*.json'):
                j = self._load(path)
                if j['state'] in ('running', 'cleanup-required'):
                    self._cleanup(j)
                    j['error'] = 'interrupted_verification'; self._write(j)

    def verify(self, db, document):
        require(not db.in_transaction, 'transaction_open')
        try:
            doc = copy.deepcopy(document)
            require(type(doc) is dict and type(doc.get('source_job')) is int and doc['source_job'] > 0 and re.fullmatch(PIN, doc.get('image', '')) and re.fullmatch('[1-9][0-9]{0,8}:[0-9]{1,9}', doc.get('user', '')), 'invalid_artifact')
            expected = managed_services._role_evidence(doc)
            require(expected['package_digest'] == package.PACKAGE_DIGEST and expected['package_version'] == package.PACKAGE_VERSION and expected['framework_version'] == package.FRAMEWORK_VERSION and expected['adapter_revision'] == 1 and expected['role'] in ('queue', 'scheduler'), 'untrusted_package')
            # Exact accepted profile schema and fixed commands, never caller argv.
            from build_profiles import validate_role_profile
            validate_role_profile(doc['template_id'], {
                'revision': doc['template_revision'], 'image_name': doc['image_name'],
                'command': doc['command'], 'healthcheck': doc['healthcheck'],
                'user': doc['user'], 'workload': doc['workload']})
            workload_protocol.validate_request({'protocol': 1, 'operation': 'capabilities',
                'scope': {'project': doc['project'], 'environment': doc['source_environment'],
                          'service_id': str(uuid.uuid4()), 'definition_digest': digest(doc)}})
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, VerificationError):
                raise
            raise VerificationError('invalid_artifact') from None
        identity = {'version': VERSION, 'postgres_image': self.postgres_image, 'source_job': doc['source_job'], 'image': doc['image'], 'workload_digest': digest(doc['workload']), 'document_digest': digest(doc)}
        key = digest(identity)
        with self._lock():
            path = self.root / (key + '.json')
            if path.exists():
                j = self._load(path)
                require(j.get('identity') == identity, 'journal_identity_changed')
                if j['state'] in ('running', 'cleanup-required'):
                    self._cleanup(j)
                    j['error'] = 'interrupted_verification'; self._write(j)
                if j['state'] == 'verified':
                    require(j.get('evidence') == expected and all(r['state'] == 'removed' for r in j['resources']), 'journal_identity_changed')
                    return copy.deepcopy(expected)
                raise VerificationError(j.get('error', 'verification_failed'))
            # Do not allocate a new fixture while any prior uncertain cleanup exists.
            require(not any(self._load(p)['state'] in ('running', 'cleanup-required') for p in self.root.glob('*.json')), 'cleanup_pending')
            j = {'version': VERSION, 'key': key, 'identity': identity, 'nonce': uuid.uuid4().hex, 'state': 'running', 'resources': [], 'created': time.time()}
            self._write(j)
            self.deadline = time.monotonic() + 180
            result = None
            try:
                self._verify_image(j, doc)
                result = expected
            except Exception as exc:
                j['error'] = exc.code if isinstance(exc, (VerificationError, package.PackageVerificationError, workload_protocol.ProtocolError)) else 'verification_failed'
                self._write(j)
            finally:
                self._cleanup(j)
            if result is None:
                raise VerificationError(j.get('error', 'verification_failed'))
            j.update(state='verified', evidence=result, verified_at=time.time()); self._write(j)
            return copy.deepcopy(result)

    @staticmethod
    def _base(network, user, memory, cpus, pids, tmpfs):
        return ['--network', network, '--user', user, '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--memory', str(memory), '--cpus', str(cpus), '--pids-limit', str(pids), '--log-driver', 'none', '--no-healthcheck', *[a for path, options in tmpfs.items() for a in ('--tmpfs', path + ':' + options)]]

    def _sql(self, dbid, statement):
        return self._run(['exec', '--user', 'postgres', '--interactive', dbid, 'psql', '-X', '-q', '-t', '-A', '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', 'fixture'], statement.encode(), timeout=10, limit=65536)

    def _verify_image(self, j, doc):
        image = self._inspect_image(doc['image'])
        pg = self._inspect_image(self.postgres_image)
        cfg = image['Config']
        wd = cfg.get('WorkingDir')
        require(type(wd) is str and wd.startswith('/') and wd != '/' and str(Path(wd)) == wd and '..' not in Path(wd).parts and not any(ord(c) < 32 for c in wd), 'unsupported_application_layout')
        require(not cfg.get('Volumes') and not cfg.get('OnBuild'), 'unsupported_image_configuration')
        require(pg['Config'].get('Entrypoint') == ['docker-entrypoint.sh'] and pg['Config'].get('Cmd') == ['postgres'], 'unsupported_postgres_image')
        inspection = self._create(j, 'inspection', image['Id'], [*self._base('none', doc['user'], 134217728, .25, 32, {}), '--entrypoint', 'php', image['Id'], '--version'])
        self._security(j, inspection, network='none', user=doc['user'], entrypoint=['php'], command=['--version'], tmpfs={}, memory=134217728, cpus=250000000, pids=32, running=False)
        base = wd + '/vendor/shiftcodes/deploy-monitor-workloads/'
        archives = {name: self._run(['cp', inspection['id'] + ':' + base + name, '-'], limit=package.MAX_ARCHIVE_BYTES) for name in package.COMPONENTS}
        package.verify_archives(archives)
        metadata = single_json_archive(self._run(['cp', inspection['id'] + ':' + wd + '/vendor/composer/installed.json', '-'], limit=2097152))
        packages = metadata.get('packages') if type(metadata) is dict else metadata
        require(type(packages) is list and len(packages) <= 2000, 'invalid_framework_metadata')
        framework = [p for p in packages if type(p) is dict and p.get('name') == 'laravel/framework']
        require(len(framework) == 1 and framework[0].get('version') in (package.FRAMEWORK_VERSION, 'v' + package.FRAMEWORK_VERSION), 'framework_mismatch')
        database_tmpfs = {'/var/lib/postgresql/data': 'rw,nosuid,nodev,noexec,size=268435456,mode=1777', '/var/run/postgresql': 'rw,nosuid,nodev,noexec,size=16777216,mode=1777', '/tmp': 'rw,nosuid,nodev,noexec,size=16777216,mode=1777'}
        admin, password = secrets.token_hex(32), secrets.token_hex(32)
        database = self._create(j, 'database', pg['Id'], [*self._base('none', 'postgres', 268435456, .5, 64, database_tmpfs), '-e', 'POSTGRES_PASSWORD', '-e', 'POSTGRES_DB=fixture', '-e', 'PGDATA=/var/lib/postgresql/data/pgdata', pg['Id']], {'POSTGRES_PASSWORD': admin})
        security = dict(network='none', user='postgres', entrypoint=['docker-entrypoint.sh'], command=['postgres'], tmpfs=database_tmpfs, memory=268435456, cpus=500000000, pids=64)
        self._security(j, database, **security, running=False)
        self._run(['start', database['id']])
        ready = False
        for _ in range(60):
            self._security(j, database, **security, running=True)
            try:
                require(self._sql(database['id'], 'SELECT 1;').strip() == b'1', 'postgres_not_ready')
                ready = True; break
            except VerificationError:
                time.sleep(.25)
        require(ready, 'postgres_not_ready')
        schema = SQL_SCHEMA + "\nINSERT INTO verifier_sentinel VALUES ('" + j['nonce'] + "');\nCREATE ROLE fixture_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '" + password + "';\nREVOKE ALL ON DATABASE fixture FROM PUBLIC; GRANT CONNECT ON DATABASE fixture TO fixture_app; REVOKE CREATE ON SCHEMA public FROM PUBLIC; GRANT USAGE ON SCHEMA public TO fixture_app; GRANT SELECT ON " + ','.join(TABLES) + ' TO fixture_app;'
        self._sql(database['id'], schema)
        def snapshot():
            raw = self._run(['exec', '--user', 'postgres', database['id'], 'pg_dump', '--schema-only', '--no-owner', '--no-privileges', '--no-comments', '-U', 'postgres', '-d', 'fixture'], limit=65536)
            # Recent supported PG minor releases add randomized psql guard tokens.
            return re.sub(rb'^\\(?:un)?restrict [A-Za-z0-9]+\r?$', b'', raw, flags=re.MULTILINE)
        before_schema = snapshot()
        scope = {'project': doc['project'], 'environment': doc['source_environment'], 'service_id': str(uuid.uuid4()), 'definition_digest': digest(doc)}
        env = {'APP_ENV': 'testing', 'APP_DEBUG': 'false', 'APP_KEY': 'base64:' + base64.b64encode(secrets.token_bytes(32)).decode(), 'DB_CONNECTION': 'pgsql', 'DB_HOST': '127.0.0.1', 'DB_PORT': '5432', 'DB_DATABASE': 'fixture', 'DB_USERNAME': 'fixture_app', 'DB_PASSWORD': password, 'LOG_CHANNEL': 'stderr', 'CACHE_STORE': 'array', 'CACHE_DRIVER': 'array', 'SESSION_DRIVER': 'array', 'QUEUE_CONNECTION': doc['workload']['configuration']['connection'], 'DEPLOY_MONITOR_WORKLOAD': canonical(doc['workload']), **{'DEPLOY_MONITOR_' + k.upper(): v for k, v in scope.items()}}
        for role, operation in [('capabilities', 'capabilities'), ('observation', 'queue.observe' if doc['workload']['role'] == 'queue' else 'scheduler.observe')]:
            self._security(j, database, **security, running=True)
            request = workload_protocol.validate_request({'protocol': 1, 'operation': operation, 'scope': scope})
            tmpfs = {'/tmp': 'rw,nosuid,nodev,noexec,size=33554432,mode=1777'}
            argv = ['artisan', 'deploy-monitor:protocol', '--no-ansi', '--no-interaction']
            candidate = self._create(j, role, image['Id'], [*self._base('container:' + database['id'], doc['user'], 268435456, .5, 64, tmpfs), '--interactive', '--entrypoint', 'php', *[a for k in sorted(env) for a in ('-e', k)], image['Id'], *argv], env)
            kwargs = dict(network='container:' + database['id'], user=doc['user'], entrypoint=['php'], command=argv, tmpfs=tmpfs, memory=268435456, cpus=500000000, pids=64)
            row = self._security(j, candidate, **kwargs, running=False)
            actual_env = dict(v.split('=', 1) for v in row['Config'].get('Env', []) if '=' in v)
            require(all(actual_env.get(k) == v for k, v in env.items()), 'fixture_environment_changed')
            response = self._run(['start', '--attach', '--interactive', candidate['id']], canonical(request).encode() + b'\n', timeout=20)
            row = self._security(j, candidate, **kwargs, running=False)
            require(row['State'].get('Status') == 'exited' and row['State'].get('ExitCode') == 0 and not row['State'].get('OOMKilled'), 'candidate_not_completed')
            result = workload_protocol.validate_response(decode(response), request)
            if role == 'capabilities':
                require(result['status'] == 'ok', 'unsupported_bootstrap')
                observed = result['result']
                require(all(observed.get(k) == v for k, v in {'package_version': package.PACKAGE_VERSION, 'package_digest': package.PACKAGE_DIGEST, 'framework_version': package.FRAMEWORK_VERSION, 'adapter_revision': 1, 'role': doc['workload']['role']}.items()), 'capability_mismatch')
            elif doc['workload']['role'] == 'queue':
                require(result['status'] == 'ok', 'unsupported_bootstrap')
                observation = result['result']
                require(observation['connection_id'] == doc['workload']['configuration']['connection'] and observation['workers'] == [] and observation['queues'] == [{'name': q, 'ready': 0, 'delayed': 0, 'reserved': 0} for q in doc['workload']['configuration']['queues']], 'observation_mismatch')
            else:
                require(result.get('error', {}).get('code') == 'registry_not_observed' and result['status'] != 'ok', 'observation_mismatch')
        self._security(j, database, **security, running=True)
        require(snapshot() == before_schema, 'fixture_schema_changed')
        check = "SELECT value FROM verifier_sentinel;" + ''.join('SELECT count(*) FROM ' + name + ';' for name in TABLES)
        require(self._sql(database['id'], check).decode().split() == [j['nonce']] + ['0'] * len(TABLES), 'fixture_data_changed')
