"""One deployment at a time. Recipes are administrator-owned, never webhook commands.

The receiver cannot reach Docker. The worker rechecks the configured branch and
uses isolated test containers without production environment files or volumes.
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import control
from compose_identity import ordered_files
from telemetry import HostMetrics, record as record_metrics
import time
from pathlib import Path

from store import connect, projects, effective_projects, valid_branch, set_status, snapshot

DOCKER = os.environ.get('DOCKER_BIN', '/usr/local/bin/docker')
COMPOSE = os.environ.get('COMPOSE_BIN', '/usr/local/bin/docker-compose')
STATE = Path(os.environ.get('STATE_DIR', '/worker-state'))
DATABASE = os.environ.get('DATABASE', '/state/state.sqlite')
CONFIG = os.environ.get('PROJECTS_FILE', '/config/projects.json')
diagnostics = threading.local()
host_metrics=HostMetrics()


class StepFailure(Exception):
    pass


def run(args, timeout=180, cwd=None, capture=False):
    # Command output can contain application secrets; it never reaches the UI.
    env = {k: v for k, v in os.environ.items() if k in (
        'PATH', 'HOME', 'DOCKER_HOST', 'GIT_SSH_COMMAND', 'SSL_CERT_FILE')}
    env.update(GIT_TERMINAL_PROMPT='0', DOCKER_BUILDKIT='0')
    try:
        result = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise StepFailure('The step timed out and was not marked successful.') from exc
    if result.returncode:
        job_id = getattr(diagnostics, 'job_id', None)
        if job_id is not None:
            folder = STATE / 'diagnostics'
            folder.mkdir(mode=0o700, exist_ok=True)
            path = folder / (str(int(job_id)) + '.log')
            path.write_bytes(result.stdout[-65536:] + b'\n' + result.stderr[-65536:])
            path.chmod(0o600)
            for old in folder.glob('*.log'):
                if old.stat().st_mtime < time.time() - 7 * 86400:
                    old.unlink()
        reason = 'Execution step failed'
        output = (result.stdout[-65536:] + result.stderr[-65536:]).lower()
        for needle, label in [(b'no space left', 'Not enough disk space for the build'),
                              (b'could not resolve', 'Host name resolution failed during the build'),
                              (b'pull access denied', 'Base image unavailable'),
                              (b'failures!', 'Project tests failed'),
                              (b'assertionerror', 'An assertion failed in the project tests'),
                              (b'permission denied', 'Insufficient permissions for this step')]:
            if needle in output:
                reason = label
                break
        raise StepFailure(reason + ' (exit %d). Detailed diagnostics are stored privately by the worker.' % result.returncode)
    return result.stdout.decode() if capture else None


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as out:
        os.chmod(temp, 0o600)
        json.dump(value, out)
        out.flush()
        os.fsync(out.fileno())
    temp.replace(path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def compose(recipe, *args):
    command = [COMPOSE, '--profile', '*', '-p', recipe['compose_project']]
    if recipe.get('env_file'):
        command += ['--env-file', recipe['env_file']]
    for filename in recipe['compose_files']:
        command += ['-f', filename]
    return command + list(args)


def replace_env(path, changes):
    # Preserve existing configuration symlinks such as .env -> ports.env.
    path = Path(path).resolve(strict=True)
    lines = path.read_text().splitlines()
    seen = set()
    for i, line in enumerate(lines):
        key = line.split('=', 1)[0]
        if key in changes:
            lines[i] = key + '=' + changes[key]
            seen.add(key)
    lines.extend(k + '=' + v for k, v in changes.items() if k not in seen)
    temp = path.with_name(path.name + '.deploy-tmp')
    with temp.open('w') as out:
        os.chmod(temp, path.stat().st_mode & 0o777)
        out.write('\n'.join(lines) + '\n')
        out.flush()
        os.fsync(out.fileno())
    temp.replace(path)


def env_values(path, keys):
    values = {}
    for line in Path(path).read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep and key in keys:
            values[key] = value.strip().strip('"').strip("'")
    if set(keys) != set(values):
        raise StepFailure('Runtime configuration is incomplete. Production was not changed.')
    return values


def containers(recipe):
    raw = run(compose(recipe, 'ps', '--all', '--format', 'json', *recipe['services']), capture=True)
    if raw.strip().startswith('['):
        return json.loads(raw)
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def verify_runtime(recipe, expected_images=None):
    entries = containers(recipe)
    if {e['Service'] for e in entries} != set(recipe['services']):
        raise StepFailure('A required service is missing after deployment.')
    for entry in entries:
        if entry.get('State') != 'running' or entry.get('Health') == 'unhealthy':
            raise StepFailure('A service is unhealthy after deployment.')
    if expected_images is not None and {e['Image'] for e in entries} != set(expected_images):
        raise StepFailure('Running images do not match the built images.')
    # An authenticated application flow belongs in project tests; HTTP is a
    # deployment availability gate, not an assertion of business acceptance.
    import urllib.request
    import urllib.error
    for check in recipe['http_checks']:
        try:
            with urllib.request.urlopen(check['url'], timeout=15) as response:
                code = response.status
        except urllib.error.HTTPError as error:
            code = error.code
        except OSError as error:
            raise StepFailure('The site could not be reached after deployment.') from error
        if code not in check['statuses']:
            raise StepFailure('HTTP check failed after deployment: %d' % code)


def recover(db, journal):
    record = json.loads(journal.read_text())
    if record.get('migration_started'):
        set_status(db, record['job_id'], 'needs-review',
                   'Deployment stopped after development migrations started. Database changes were not rolled back. Inspect the migration result before retrying.')
        raise StepFailure('Migration recovery requires inspection; image rollback cannot restore a database.')
    recipe = record['recipe']
    set_status(db, record['job_id'], 'recovering', 'Restoring and verifying the previous images.')
    replace_env(recipe['env_file'], record['previous_env'])
    run(compose(recipe, 'up', '-d', '--no-deps', '--wait', '--wait-timeout', '180', *recipe['services']), timeout=240)
    verify_runtime(recipe)
    set_status(db, record['job_id'], 'rolled-back', 'Deployment failed. Previous images were restored and passed runtime checks. The database was unchanged.')
    journal.unlink()


def validate_execution_recipe(project):
    """Reject incomplete execution plans before cloning, building, or mutation."""
    recipe = project.get('recipe')
    if project.get('environment') not in ('development', 'production', 'legacy-unclassified') or not isinstance(recipe, dict):
        raise StepFailure('A valid explicit pinned environment and recipe are required.')
    images, tests, checks = (recipe.get(key) for key in ('images', 'tests', 'http_checks'))
    if not all(isinstance(value, list) and value for value in (images, tests, checks)):
        raise StepFailure('Build setup is incomplete: images, isolated tests and runtime checks must be configured.')
    names = set()
    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get('name'), str) or image['name'] in names:
            raise StepFailure('Build image configuration is invalid.')
        names.add(image['name'])
        for key in ('dockerfile', 'context'):
            value = image.get(key, '.' if key == 'context' else None)
            if not isinstance(value, str) or not value or Path(value).is_absolute() or '..' in Path(value).parts:
                raise StepFailure('Build paths must remain inside the checked-out source.')
        if image.get('deploy', True) and (not isinstance(image.get('env_key'), str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', image['env_key'])):
            raise StepFailure('A deployment image variable is required.')
    for test in tests:
        if (not isinstance(test, dict) or test.get('image') not in names or
                not isinstance(test.get('command'), list) or not test['command'] or
                not all(isinstance(arg, str) and arg and '\x00' not in arg for arg in test['command'])):
            raise StepFailure('Isolated test configuration is invalid.')
    for check in checks:
        if (not isinstance(check, dict) or not isinstance(check.get('url'), str) or
                not check['url'].startswith(('http://', 'https://')) or
                not isinstance(check.get('statuses'), list) or not check['statuses'] or
                not all(type(code) is int and 100 <= code <= 599 for code in check['statuses'])):
            raise StepFailure('Runtime HTTP checks are invalid.')
    if not isinstance(recipe.get('env_file'), str) or not Path(recipe['env_file']).is_absolute():
        raise StepFailure('An absolute deployment environment file is required.')


def verify_target_ownership(project, expected_ids=None):
    """Read actual Docker identity; a queued deployment may not wake stopped targets."""
    recipe = project['recipe']
    entries = containers(recipe)
    if not entries or {entry.get('Service') for entry in entries} != set(recipe['services']):
        raise StepFailure('Required target services are missing. Provisioning is not available in this deployment flow.')
    identities = set()
    for entry in entries:
        identifier = entry.get('ID', '')
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{12,64}', identifier):
            raise StepFailure('The target container identity is unavailable.')
        inspected = json.loads(run([DOCKER, 'inspect', identifier], capture=True))
        if not isinstance(inspected, list) or len(inspected) != 1:
            raise StepFailure('The target container could not be verified.')
        actual = inspected[0]
        labels = actual.get('Config', {}).get('Labels') or {}
        try:
            files = ordered_files(labels.get('com.docker.compose.project.config_files', '').split(','))
            expected_files = ordered_files(recipe['compose_files'])
        except (ValueError, AttributeError):
            raise StepFailure('The observed container ownership configuration is invalid.') from None
        if (labels.get('com.docker.compose.project') != recipe['compose_project'] or
                labels.get('com.docker.compose.service') != entry['Service'] or
                files != expected_files):
            raise StepFailure('The observed container ownership differs from the pinned target. No services were changed.')
        if actual.get('State', {}).get('Running') is not True:
            raise StepFailure('A target container is stopped. This deployment flow cannot wake inactive environments.')
        identity = actual.get('Id', '')
        if not re.fullmatch('[a-f0-9]{64}', identity):
            raise StepFailure('The full target container identity is unavailable.')
        identities.add(identity)
    if expected_ids is not None and identities != expected_ids:
        raise StepFailure('The target containers were replaced during the build. Retry against their current state.')
    return identities


def schema_change_paths(changed):
    # Conservative file detection, not a claim to understand arbitrary migration code.
    result = []
    for filename in changed.splitlines():
        path = '/' + filename.lower()
        if (any(part in path for part in ('/migrations/', '/alembic/versions/', '/prisma/')) or
                path.endswith(('.sql', '/schema.rb', '/structure.sql', '/schema.prisma'))):
            result.append(filename)
    return result


def migration_intent(source, filenames):
    """Flag destructive intent for diagnostics; absence is never proof of safety."""
    destructive, unverified = [], []
    pattern = re.compile(
        r"\b(?:drop\s+(?:table|database|schema|column|index)|truncate\s+|delete\s+from|"
        r"dropIfExists|dropColumn|dropTable|dropDatabase|Schema\s*::\s*drop|RemoveField|DeleteModel|"
        r"migrate:(?:fresh|reset|refresh)|db:wipe)\b", re.IGNORECASE)
    root = Path(source).resolve()
    for filename in filenames:
        path = (root / filename).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            unverified.append(filename)
            continue
        # Bound source inspection; unreadable/binary/large changes need adapter review.
        if path.stat().st_size > 1048576:
            unverified.append(filename)
            continue
        try:
            content = path.read_text()
        except (OSError, UnicodeError):
            unverified.append(filename)
            continue
        if filename.lower().endswith('.php'):
            # Laravel rollback methods commonly drop newly added tables; they are
            # not the forward operation. This remains a diagnostic heuristic.
            forward = re.search(r'function\s+up\s*\([^)]*\)[^{]*\{', content)
            if forward:
                content = content[forward.end():]
                content = re.split(r'function\s+down\s*\(', content, maxsplit=1)[0]
        if pattern.search(content):
            destructive.append(filename)
        else:
            unverified.append(filename)
    return {'destructive': destructive, 'unverified': unverified}


def migration_policy(project):
    recipe = project['recipe']
    policy = recipe.get('migration')
    if not policy:
        return None
    if (not isinstance(policy, dict) or project.get('environment') != 'development' or policy.get('framework') != 'laravel'
            or policy.get('service') not in recipe['services']
            or policy.get('image') not in {image['name'] for image in recipe['images']}):
        raise StepFailure('Invalid development migration configuration. No database was changed.')
    return policy


def run_migrations(recipe, policy, images):
    # Compose inherits the selected app's network and environment; only the
    # image is overridden. Do not change live tags or start dependencies.
    with tempfile.TemporaryDirectory(prefix='migration-') as directory:
        override = Path(directory) / 'compose.json'
        override.write_text(json.dumps({'services': {
            policy['service']: {'image': images[policy['image']]}}}))
        run(compose(recipe, '-f', str(override), 'run', '--rm', '--no-deps', '-T',
                    '--entrypoint', 'php', policy['service'], 'artisan', 'migrate',
                    '--force', '--no-interaction'), timeout=600)


def deploy(db, job, project, validate_only=False):
    diagnostics.job_id = job['id']
    validate_execution_recipe(project)
    if job['mode'] not in ('validate', 'deploy') or validate_only != (job['mode'] == 'validate'):
        raise StepFailure('The execution mode differs from the pinned run mode. Retry as a new run.')
    recipe = project['recipe']
    migration = migration_policy(project)
    target_ids = verify_target_ownership(project) if not validate_only and project['environment'] != 'legacy-unclassified' else None
    name, sha = job['project'], job['sha']
    branch = job['branch'] or project['branch']
    if not valid_branch(branch):
        raise StepFailure('Invalid branch name.')
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise StepFailure('Invalid Git commit.')
    if not recipe.get('tests') or not recipe.get('http_checks'):
        raise StepFailure('Build setup is incomplete: isolated tests and runtime checks must be configured.')
    journal = STATE / 'transaction.json'
    with tempfile.TemporaryDirectory(prefix=name + '-', dir='/work') as temp:
        source = Path(temp) / 'source'
        set_status(db, job['id'], 'building', 'Verifying Git and preparing an isolated build.')
        remote = 'git@github.com:' + project['repository'] + '.git'
        run(['git', 'clone', '--branch', branch, '--', remote, str(source)], timeout=300)
        head = run(['git', 'rev-parse', 'HEAD'], cwd=source, capture=True).strip()
        if head != sha:
            set_status(db, job['id'], 'superseded', 'The branch has a newer commit. The older commit will not be deployed.')
            return
        # Detect schema differences separately from image build/test results.
        previous_images = sorted({e['Image'] for e in containers(recipe)})
        revisions = set()
        for previous in previous_images:
            value = run([DOCKER, 'image', 'inspect', '--format', '{{index .Config.Labels "org.opencontainers.image.revision"}}', previous], capture=True).strip()
            if not re.fullmatch('[0-9a-f]{7,40}', value):
                value = previous.rsplit(':', 1)[-1]
            if not re.fullmatch('[0-9a-f]{7,40}', value):
                raise StepFailure('The current image commit could not be identified. Review is required before deployment.')
            revisions.add(value)
        changed = '\n'.join(run(['git', 'diff', '--name-only', old, sha], cwd=source, capture=True) for old in revisions)
        schema_files = schema_change_paths(changed)
        schema_changes = bool(schema_files)
        intent = migration_intent(source, schema_files)
        if not validate_only and project['environment'] != 'development' and intent['destructive']:
            raise StepFailure('Destructive database migration intent was detected. Production or unclassified database changes are prohibited; no services or databases were changed.')
        tracked = run(['git', 'ls-files'], cwd=source, capture=True).splitlines()
        if any(Path(f).name in ('.env', 'id_rsa', 'id_ed25519') for f in tracked):
            raise StepFailure('A sensitive configuration file was found in Git. Build stopped before copying the context.')
        images = {}
        updates = {}
        version = sha[:12] + '-' + str(job['id'])
        for index, spec in enumerate(recipe['images']):
            # Never overwrite a running tag during same-commit validation/retry.
            tag = spec['name'] + ':' + version
            set_status(db, job['id'], 'building', 'Building image ' + spec['name'] + ' from Git.')
            args = [DOCKER, 'build', '--force-rm', '--memory', '1536m', '--cpu-period', '100000',
                    '--cpu-quota', '200000', '--network', 'deploy-build',
                    '--label', 'net.shiftcodes.deployer=deploy-monitor',
                    '--label', 'net.shiftcodes.job=' + str(job['id']),
                    '--label', 'org.opencontainers.image.revision=' + sha,
                    '-t', tag, '-f', str(source / spec['dockerfile'])]
            if index == 0:
                args += ['--no-cache']
            for key, value in spec.get('build_args', {}).items():
                args += ['--build-arg', key + '=' + value]
            if spec.get('target'):
                args += ['--target', spec['target']]
            args += [str(source / spec.get('context', '.'))]
            run(args, timeout=1800)
            images[spec['name']] = tag
            if spec.get('deploy', True):
                updates[spec['env_key']] = tag if spec.get('env_mode', 'image') == 'image' else version
        set_status(db, job['id'], 'testing', 'Testing images without production secrets or databases.')
        for i, test in enumerate(recipe['tests']):
            test_name = 'deploy-test-%s-%d' % (job['id'], i)
            args = [DOCKER, 'run', '--rm', '--name', test_name, '--network', 'none', '--read-only',
                    '--cap-drop=ALL', '--security-opt=no-new-privileges', '--memory=512m', '--cpus=1',
                    '--pids-limit=128', '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m']
            for mount in test.get('tmpfs', []):
                args += ['--tmpfs', mount]
            for key, value in test.get('environment', {}).items():
                args += ['-e', key + '=' + value]
            args += ['--entrypoint', test['command'][0], images[test['image']], *test['command'][1:]]
            try:
                run(args, timeout=180)
            finally:
                subprocess.run([DOCKER, 'rm', '-f', test_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if validate_only:
            set_status(db, job['id'], 'validated', 'Build and internal tests passed. Services and databases were not changed.' + (' Development migrations will run during deployment.' if migration else ' Migration changes require review.' if schema_changes else ''))
            return
        if schema_changes and not migration:
            set_status(db, job['id'], 'failed', 'Build and internal tests passed. Database changes cannot run without a verified non-destructive migration runner for this environment. Services and databases were not changed.')
            return
        previous_env = env_values(recipe['env_file'], updates)
        current_head = run(['git', 'ls-remote', '--refs', 'origin', 'refs/heads/' + branch], cwd=source, capture=True).split()
        if not current_head or current_head[0] != sha:
            set_status(db, job['id'], 'superseded', 'A newer commit arrived during the build. Production services were not changed.')
            return
        if target_ids is not None:
            verify_target_ownership(project, target_ids)
        # Persist recovery instructions before the first production mutation.
        record = {'job_id': job['id'], 'recipe': recipe, 'previous_env': previous_env}
        atomic_json(journal, record)
        try:
            if migration:
                record['migration_started'] = True
                atomic_json(journal, record)
                set_status(db, job['id'], 'deploying', 'Build and tests passed. Applying pending Laravel migrations to the configured development database.')
                run_migrations(recipe, migration, images)
                set_status(db, job['id'], 'deploying', 'Development migrations completed. Updating the selected services.')
            else:
                set_status(db, job['id'], 'deploying', 'Build and tests passed. Updating the selected services.')
            replace_env(recipe['env_file'], updates)
            run(compose(recipe, 'up', '-d', '--no-deps', '--wait', '--wait-timeout', '180', *recipe['services']), timeout=240)
            verify_runtime(recipe, [images[s['name']] for s in recipe['images'] if s.get('deploy', True)])
        except Exception:
            recover(db, journal)
            return
        set_status(db, job['id'], 'succeeded', 'Deployment and runtime checks passed. ' + ('Pending development migrations were applied successfully.' if migration else 'No database migrations were run automatically.'))
        journal.unlink()
        # No force, no volume pruning. Docker refuses images still referenced.
        for old in previous_images:
            if old not in images.values():
                subprocess.run([DOCKER, 'image', 'rm', old], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


def observe(db):
    # Read only ownership labels, never the full label map (which may contain secrets).
    fields = ('ID', 'Names', 'Image', 'State', 'Status')
    labels = {
        'ComposeProject': 'com.docker.compose.project',
        'ComposeService': 'com.docker.compose.service',
        'ComposeConfigFiles': 'com.docker.compose.project.config_files',
    }
    template = '{' + ','.join(
        json.dumps(key) + ':{{json .' + key + '}}' for key in fields
    ) + ',' + ','.join(
        json.dumps(key) + ':{{json (.Label "' + label + '")}}' for key, label in labels.items()
    ) + '}'
    raw = run([DOCKER, 'ps', '-a', '--no-trunc', '--filter', 'label=com.docker.compose.project',
               '--format', template], capture=True)
    data = []
    for line in raw.splitlines():
        obj = json.loads(line)
        if obj.get('Names', '').startswith(('tanialab-', 'deploy-monitor-recovery-check-', 'deploy-test-')):
            continue
        data.append({key: obj.get(key) if isinstance(obj.get(key), str) else None
                     for key in (*fields, *labels)})
    stats_raw = run([DOCKER, 'stats', '--no-stream', '--format', '{{json .}}'], timeout=30, capture=True)
    stats = {row['Name']: row for row in (json.loads(line) for line in stats_raw.splitlines() if line.strip())}
    for entry in data:
        reading = stats.get(entry['Names'], {})
        entry.update({key: reading.get(key) for key in ('CPUPerc','MemUsage','MemPerc','PIDs')})
    snapshot(db, 'containers', data)
    disk = shutil.disk_usage('/work')
    snapshot(db, 'disk', {'total': disk.total, 'free': disk.free})
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        if key in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
            memory[key] = int(value.strip().split()[0]) * 1024
    snapshot(db, 'resources', {'memory': memory, 'load_1m': os.getloadavg()[0], 'cpu_count': os.cpu_count()})
    snapshot(db, 'worker', {'state': 'running', 'external_monitor': False, 'outbound_alerts': False})


def observe_branches(db, name, project):
    try:
        raw=run(['git','ls-remote','--heads','git@github.com:'+project['repository']+'.git'],timeout=45,capture=True)
        branches={}
        for line in raw.splitlines():
            sha,ref=line.split('\t',1)
            branch=ref.removeprefix('refs/heads/')
            if re.fullmatch('[0-9a-f]{40}',sha) and valid_branch(branch):
                branches[branch]=sha
        snapshot(db,'branches:'+name,{'branches':branches,'error':False})
    except (StepFailure,ValueError):
        snapshot(db,'branches:'+name,{'branches':{},'error':True,'detail':'Could not fetch GitHub branches. Try again.'})
    db.execute('DELETE FROM requests WHERE project=?',(name,))


def collect_branches_once(db, config=None):
    """Discard obsolete requests and perform at most one actual Git observation."""
    policy = effective_projects(db, CONFIG if config is None else config)
    name = None
    for request in db.execute('SELECT project,created FROM requests ORDER BY created,project').fetchall():
        if request['project'] in policy:
            name = request['project']
            break
        db.execute('DELETE FROM requests WHERE project=? AND created=?',
                   (request['project'], request['created']))
    if name is None:
        for project in policy:
            row = db.execute('SELECT updated FROM snapshots WHERE name=?', ('branches:' + project,)).fetchone()
            if row is None or time.time() - row['updated'] > 300:
                name = project
                break
    if name is not None:
        observe_branches(db, name, policy[name])
    return name


def project_for_job(db, job):
    """Return only the enqueue-time target; never substitute live registry state."""
    import job_target
    try:
        target = job_target.load(db, job['id'])
    except ValueError as error:
        raise StepFailure('This run has no valid pinned deployment target. Configure the project if needed, then retry as a new run. No running services were changed.') from error
    if any(job[key] != target[key] for key in ('project', 'branch', 'sha', 'mode')):
        raise StepFailure('This run differs from its pinned deployment target. Retry as a new run. No running services were changed.')
    return {'repository': target['repository'], 'repository_id': target['repository_id'],
            'branch': target['branch'], 'environment': target['environment'],
            'recipe': target['recipe']}


def main():
    STATE.mkdir(exist_ok=True)
    Path('/work').mkdir(exist_ok=True)
    with (STATE / 'worker.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = connect(DATABASE)
        journal = STATE / 'transaction.json'
        if journal.exists():
            try:
                recover(db, journal)
            except Exception:
                snapshot(db, 'worker', {'state': 'recovery-failed', 'detail': 'Recovery requires review. Deployments have stopped.'})
                raise SystemExit(1)
        db.execute("UPDATE jobs SET status='interrupted',detail='The worker stopped before the update completed. The run was not marked successful.' WHERE status IN ('building','testing','deploying')")
        def branch_collector():
            branch_db=connect(DATABASE)
            while True:
                try:
                    collect_branches_once(branch_db)
                except (OSError,ValueError,StepFailure):
                    pass
                time.sleep(3)
        def host_collector():
            measurements=connect(DATABASE)
            while True:
                try:
                    reading=host_metrics.read();record_metrics(measurements,reading);snapshot(measurements,'telemetry',reading)
                    snapshot(measurements,'telemetry-error',{'error':False})
                except (OSError,ValueError,KeyError,IndexError):
                    snapshot(measurements,'telemetry-error',{'error':True,'detail':'Host metrics are unavailable. Check the host measurement mounts.'})
                time.sleep(5)
        threading.Thread(target=host_collector,daemon=True).start()
        control.start()
        threading.Thread(target=branch_collector,daemon=True).start()
        def collector():
            observations = connect(DATABASE)
            while True:
                try:
                    observe(observations)
                except (StepFailure, OSError):
                    snapshot(observations, 'worker', {'state': 'unavailable', 'detail': 'Docker could not be observed. Previous observations are stale.'})
                time.sleep(5)
        threading.Thread(target=collector, daemon=True).start()
        while True:
            try:
                job = db.execute("SELECT * FROM jobs WHERE status IN ('queued','validation-queued') ORDER BY id LIMIT 1").fetchone()
                if job:
                    validation = job['status'] == 'validation-queued'
                    claimed=db.execute("UPDATE jobs SET status='building',updated=? WHERE id=? AND status=?",(time.time(),job['id'],job['status'])).rowcount
                    if not claimed:
                        continue
                    try:
                        project = project_for_job(db, job)
                    except StepFailure as error:
                        set_status(db, job['id'], 'failed', str(error))
                    else:
                        try:
                            with control.LOCK:
                                if control.JOURNAL.exists():raise StepFailure('Environment recovery is pending. Deployment was not started.')
                                deploy(db, job, project, validate_only=validation)
                        except Exception as error:
                            if journal.exists():
                                snapshot(db, 'worker', {'state': 'recovery-failed'})
                                raise SystemExit(1)
                            phase = db.execute('SELECT detail FROM jobs WHERE id=?', (job['id'],)).fetchone()['detail']
                            detail = str(error) if isinstance(error, StepFailure) else 'Internal failure. Deployment was not marked successful.'
                            set_status(db, job['id'], 'failed', phase + ' — ' + detail)
                        finally:
                            # Only this job's unreferenced images; never force
                            # delete a running image and never touch volumes.
                            subprocess.run([DOCKER, 'image', 'prune', '-af', '--filter', 'label=net.shiftcodes.job=' + str(job['id'])],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                            subprocess.run([DOCKER, 'builder', 'prune', '-af'],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            except (StepFailure, OSError):
                snapshot(db, 'worker', {'state': 'unavailable', 'detail': 'Docker could not be observed. Previous observations are stale.'})
            time.sleep(5)


if __name__ == '__main__':
    main()
