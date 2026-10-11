"""One deployment at a time. Recipes are administrator-owned, never webhook commands.

The receiver cannot reach Docker. The worker rechecks the configured branch and
uses isolated test containers without production environment files or volumes.
"""
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import control
import provenance
import compose_review
from compose_identity import ordered_files
from telemetry import HostMetrics, record as record_metrics
import time
from pathlib import Path

from store import connect, projects, effective_projects, valid_branch, set_status, snapshot

DOCKER = os.environ.get('DOCKER_BIN', '/usr/local/bin/docker')
COMPOSE = os.environ.get('COMPOSE_BIN', '/usr/local/bin/docker-compose')
STATE = Path(os.environ.get('STATE_DIR', '/worker-state'))
ARTIFACT_WORK = Path(os.environ.get('ARTIFACT_WORK_DIR', '/work'))
ARTIFACT_BUILD_NETWORK = os.environ.get('ARTIFACT_BUILD_NETWORK', 'deploy-build')
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
    if record.get('provenance_transition'):
        import provenance_store
        provenance_store.rollback(db, record['provenance_transition'])
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
    if 'service_profiles' in recipe:
        import managed_services
        profiles = recipe['service_profiles']
        if not isinstance(profiles, dict):
            raise StepFailure('Service profiles must be a configured template map.')
        try:
            for template_id in profiles:
                managed_services._profile({'recipe': recipe}, template_id)
        except (ValueError, TypeError, KeyError):
            raise StepFailure('Service profiles must define valid fixed commands, health checks and runtime identity.') from None
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


def verify_recipe_provenance(recipe):
    entries = recipe.get('service_provenance', {})
    if not isinstance(entries, dict):
        raise StepFailure('Compose provenance mapping is invalid.')
    def resolve(current, service, observed_files):
        return compose_review.review(current, service, observed_files,
                                     Path('/opt/docker-engine/projects'),
                                     lambda args: run(args, capture=True), COMPOSE)
    try:
        for service in entries:
            if service not in recipe.get('owned_services', recipe.get('services', [])):
                raise ValueError('Compose provenance claims an unowned service.')
            provenance.revalidate(recipe, service, resolve)
    except ValueError:
        raise StepFailure('Compose provenance changed or could not be verified. Refresh reconciliation before deployment.') from None


def verify_target_ownership(project, expected_ids=None):
    """Read actual Docker identity; a queued deployment may not wake stopped targets."""
    recipe = project['recipe']
    verify_recipe_provenance(recipe)
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
        except (ValueError, AttributeError):
            raise StepFailure('The observed container ownership configuration is invalid.') from None
        if (labels.get('com.docker.compose.service') != entry['Service'] or
                not provenance.matches(recipe, labels.get('com.docker.compose.project'),
                                       entry['Service'], list(files))):
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


def _migration_source_evidence(paths):
    detected = sorted(set(path for path in paths if schema_change_paths(path)))
    # Source filenames are untrusted; quote controls and bound public diagnostics.
    summary = ', '.join(json.dumps(path[:160], ensure_ascii=True) for path in detected[:8])
    if len(detected) > 8:
        summary += ' (additional paths omitted)'
    return detected, summary


def check_managed_migration_capability(source, sha, selection, tracked):
    """Heuristic source boundary, not ledger verification or startup-code safety."""
    baselines = set()
    services = selection.get('services') if isinstance(selection, dict) else None
    if not isinstance(services, list) or not services:
        raise StepFailure('Managed migration source evidence is missing. No build or runtime cutover was started.')
    for service in services:
        try:
            baseline = service['definition']['artifact'].get('source_sha')
        except (KeyError, TypeError, AttributeError):
            baseline = None
        if not isinstance(baseline, str) or not re.fullmatch('[0-9a-f]{40}', baseline):
            detected, summary = _migration_source_evidence(tracked)
            if detected:
                raise StepFailure('Managed migration runner is unsupported: current source has schema or migration files without a verified source baseline: ' + summary + '. No migrations or runtime cutover were performed.')
            continue
        baselines.add(baseline)
    for baseline in sorted(baselines):
        try:
            changed = run(['git', 'diff', '--name-only', '--no-renames', '-z', baseline, sha, '--'], cwd=source, capture=True)
        except StepFailure:
            raise StepFailure('Managed migration source comparison failed. No migrations or runtime cutover were performed.') from None
        detected, summary = _migration_source_evidence(changed.split(chr(0)))
        if detected:
            raise StepFailure('Managed migration runner is unsupported. Changed schema or migration source files (not database pending migrations): ' + summary + '. No migrations or runtime cutover were performed. Validation-only builds remain available.')


def build_artifacts(db, job, project, validate_only):
    """Build a new project's pinned source without any runtime target dependency."""
    managed = project['recipe'].get('schema_kind') == 'managed-build'
    if (managed and (job['mode'] != 'deploy' or validate_only)) or (not managed and (job['mode'] != 'validate' or not validate_only)):
        raise StepFailure('Artifact-only build profiles support validation builds only. No deployment was started.')
    if project.get('environment') not in ('development', 'production'):
        raise StepFailure('An explicit environment is required for an artifact build.')
    import build_profiles
    try:
        source_recipe = dict(project['recipe'])
        if managed:
            import managed_delivery
            managed_selection = managed_delivery.load_target(db, job['id'])
            source_recipe.pop('managed_target'); source_recipe['schema_kind'] = 'artifact-build'
        recipe = build_profiles.validate_recipe(source_recipe)
    except (ValueError, TypeError, KeyError):
        raise StepFailure('The pinned artifact build profile is invalid. Save a supported build source before retrying.') from None
    if any(image['name'] != job['project'] + '-' + project['environment'] for image in recipe['images']):
        raise StepFailure('The artifact image belongs to a different project or environment.')
    sha, branch = job['sha'], job['branch']
    if (not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha)
            or not valid_branch(branch) or branch != project['branch']):
        raise StepFailure('The artifact build must use its pinned branch and commit.')
    if not isinstance(project.get('repository'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,99}/[A-Za-z0-9_.-]{1,100}', project['repository']):
        raise StepFailure('The pinned source repository is invalid.')
    with tempfile.TemporaryDirectory(prefix=job['project'] + '-artifact-', dir=ARTIFACT_WORK) as temporary:
        source = Path(temporary) / 'source'
        set_status(db, job['id'], 'building', 'Checking out the pinned source for an isolated artifact build.')
        run(['git', 'clone', '--branch', branch, '--', 'git@github.com:' + project['repository'] + '.git', str(source)], timeout=300)
        if run(['git', 'rev-parse', 'HEAD'], cwd=source, capture=True).strip() != sha:
            set_status(db, job['id'], 'superseded', 'The source branch changed. No image was built from a different commit.')
            return
        tracked = run(['git', 'ls-files'], cwd=source, capture=True).splitlines()
        if any(Path(path).name in ('.env', 'id_rsa', 'id_ed25519') for path in tracked):
            raise StepFailure('A sensitive configuration file was found in Git. Build stopped before copying the context.')
        migration_record = None
        if managed:
            import migration_jobs
            migration_record = migration_jobs.optional(db, job['id'])
            if migration_record and migration_record['target']['binding'] is not None:
                migration_jobs.authorize(db, job['id'])
            else:
                check_managed_migration_capability(source, sha, managed_selection, tracked)
        images = {}
        for spec in recipe['images']:
            try:
                dockerfile = (source / spec['dockerfile']).resolve(strict=True)
                context = (source / spec.get('context', '.')).resolve(strict=True)
            except OSError:
                raise StepFailure('The configured Dockerfile or build context is missing from the pinned checkout.') from None
            if not dockerfile.is_relative_to(source) or not dockerfile.is_file() or not context.is_relative_to(source) or not context.is_dir():
                raise StepFailure('Build paths must resolve to files and directories within the pinned checkout.')
            tag = spec['name'] + ':' + sha[:12] + '-' + str(job['id'])
            args = [DOCKER, 'build', '--force-rm', '--memory', '1536m', '--cpu-period', '100000',
                    '--cpu-quota', '200000', '--network', ARTIFACT_BUILD_NETWORK,
                    '--label', 'net.shiftcodes.deployer=deploy-monitor', '--label', 'net.shiftcodes.job=' + str(job['id']),
                    '--label', 'org.opencontainers.image.revision=' + sha, '-t', tag, '-f', str(dockerfile)]
            if spec.get('target'):
                args += ['--target', spec['target']]
            args.append(str(context))
            run(args, timeout=1800)
            images[spec['name']] = tag
        set_status(db, job['id'], 'testing', 'Testing the built images without runtime environment files, networks or databases.')
        for index, test in enumerate(recipe['tests']):
            test_name = 'artifact-test-%s-%d-%s' % (job['id'], index, Path(temporary).name.rsplit('-', 1)[-1])
            cidfile = Path(temporary) / ('test-%d.cid' % index)
            args = [DOCKER, 'run', '--rm', '--name', test_name, '--cidfile', str(cidfile), '--network', 'none', '--read-only',
                    '--cap-drop=ALL', '--security-opt=no-new-privileges', '--memory=512m', '--cpus=1',
                    '--pids-limit=128', '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m',
                    '--entrypoint', test['command'][0], images[test['image']], *test['command'][1:]]
            try:
                run(args, timeout=180)
            finally:
                identifier = cidfile.read_text().strip() if cidfile.exists() else ''
                if re.fullmatch('[a-f0-9]{64}', identifier):
                    subprocess.run([DOCKER, 'rm', '-f', identifier], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if managed:
            if migration_record and migration_record['target']['binding'] is not None:
                migration_jobs.save_sources(db, job['id'], migration_jobs.collect_sources(
                    source, migration_record['target']['binding']['definition']))
            complete_build(db, job['id'], project['recipe'], images, 'deploying',
                           'Source and isolated tests passed. Publishing artifacts before managed runtime verification.')
            # The idle worker dispatches the durable rollout after releasing
            # the non-reentrant deployment/control lock.
        else:
            complete_build(db, job['id'], recipe, images, 'validated',
                           'Pinned source built and isolated tests passed. Verified application artifact publication was recorded; no environment was provisioned.')


def deploy(db, job, project, validate_only=False):
    diagnostics.job_id = job['id']
    if isinstance(project.get('recipe'), dict) and project['recipe'].get('schema_kind') in ('artifact-build', 'managed-build'):
        return build_artifacts(db, job, project, validate_only)
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
        # Persist exact old image IDs before replacement can remove their containers.
        import image_cleanup
        previous_ids = [inspect_artifact_image(reference) for reference in previous_images]
        image_cleanup.enqueue(db, job['id'], 'legacy-images', images=previous_ids)
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
            complete_build(db, job['id'], recipe, images, 'validated', 'Build and internal tests passed. Services and databases were not changed.' + (' Development migrations will run during deployment.' if migration else ' Migration changes require review.' if schema_changes else ''))
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
            import provenance_store
            recreated = {}
            if recipe.get('service_provenance'):
                observed_ids = [entry['ID'] for entry in containers(recipe)]
                for row in json.loads(run([DOCKER, 'inspect', *observed_ids], capture=True)):
                    service = (row.get('Config', {}).get('Labels') or {}).get('com.docker.compose.service')
                    if service in recreated:
                        raise StepFailure('Recreated service identity is ambiguous.')
                    recreated[service] = row
            transition = provenance_store.prepare(db, job['project'], recipe,
                lambda current, service, files: compose_review.review(current, service, files,
                    Path('/opt/docker-engine/projects'), lambda args: run(args, capture=True), COMPOSE), recreated=recreated)
            if transition:
                record['provenance_transition'] = transition
                atomic_json(journal, record)
                provenance_store.commit(db, transition)
            journal.unlink()
        except Exception:
            recover(db, journal)
            return
        complete_build(db, job['id'], recipe, images, 'succeeded', 'Deployment and runtime checks passed. ' + ('Pending development migrations were applied successfully.' if migration else 'No database migrations were run automatically.'))
        process_cleanup(db, job_id=job['id'], kind='legacy-images', locked=True)


def inspect_artifact_image(reference):
    rows = json.loads(run([DOCKER, 'image', 'inspect', reference], capture=True))
    if not isinstance(rows, list) or len(rows) != 1:
        raise StepFailure('Built image identity is unavailable for service creation.')
    return rows[0].get('Id')


def complete_build(db, job_id, recipe, images, status, detail):
    import artifact_publication
    artifact_publication.record(db, job_id, recipe, images, status=status, detail=detail)
    artifact_publication.process(db, inspect_artifact_image, job_id)


def publish_service_artifacts(db, job_id, recipe, images):
    import artifact_publication
    artifact_publication.record(db, job_id, recipe, images)
    return artifact_publication.process(db, inspect_artifact_image, job_id)


def prune_job_images(db, job_id):
    """Protect catalogued/accepted service sources while clearing unused build outputs."""
    import managed_services
    import artifact_publication
    if artifact_publication.pending(db, job_id):
        return False
    import managed_delivery
    protected = set(managed_services.protected_images(db)) | managed_delivery.protected_images(db)
    candidates = run([DOCKER, 'image', 'ls', '--all', '--no-trunc', '--quiet',
                      '--filter', 'label=net.shiftcodes.job=' + str(job_id)], capture=True).splitlines()
    return remove_cleanup_images(set(candidates) - protected)


def remove_cleanup_images(candidates):
    # Bound one lease's image work; subsequent scans continue from live inventory.
    images = sorted(image for image in candidates if re.fullmatch(r'sha256:[a-f0-9]{64}', image))
    deadline = time.monotonic() + 120
    for image in images[:20]:
        remaining = deadline - time.monotonic()
        if remaining <= 0: return False
        result = subprocess.run([DOCKER, 'image', 'rm', image], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=min(10, remaining))
        if result.returncode: raise StepFailure('Unused image cleanup is pending.')
    return len(images) <= 20


def prune_legacy_images(db, job_id):
    import managed_services, managed_delivery, artifact_publication
    row = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row and row['status'] in ('queued','building','testing','deploying'): return False
    if not row or row['status'] != 'succeeded': return True
    artifact_publication.initialize(db)
    if db.execute("SELECT 1 FROM artifact_publications WHERE status!='succeeded' LIMIT 1").fetchone(): return False
    record = db.execute("SELECT images FROM image_cleanup WHERE job_id=? AND kind='legacy-images'", (job_id,)).fetchone()
    candidates = set(json.loads(record['images']))
    protected = managed_services.protected_images(db) | managed_delivery.protected_images(db)
    existing = set(run([DOCKER, 'image', 'ls', '--all', '--no-trunc', '--quiet'], capture=True).splitlines())
    return remove_cleanup_images(candidates & existing - protected)


def prune_build_cache(db, job_id):
    result = subprocess.run([DOCKER, 'builder', 'prune', '-af'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    if result.returncode: raise StepFailure('Build cache cleanup is pending.')


def process_cleanup(db, *, job_id=None, kind=None, locked=False):
    import image_cleanup
    if db.in_transaction: return False
    with contextlib.nullcontext() if locked else control.LOCK:
        if control.JOURNAL.exists() or (STATE / 'transaction.json').exists(): return False
        image_cleanup.reconcile_terminal_jobs(db)
        return image_cleanup.process_once(db, {
            'job-images': prune_job_images,
            'previous-images': prune_managed_previous_images,
            'build-cache': prune_build_cache,
            'legacy-images': prune_legacy_images,
        }, job_id=job_id, kind=kind)


def schedule_cleanup(db, job_id, kind, *, locked=False):
    import image_cleanup
    image_cleanup.enqueue(db, job_id, kind)
    process_cleanup(db, job_id=job_id, kind=kind, locked=locked)


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


def prune_managed_previous_images(db, job_id):
    """Retire only superseded images from this verified managed target."""
    import job_target, managed_services, managed_delivery
    job_target.initialize(db)
    if db.execute('SELECT 1 FROM job_targets WHERE job_id=?', (job_id,)).fetchone() is None:
        # Pre-target historical jobs cannot have an attributable managed prior image.
        # Existing managed-delivery evidence makes a missing target corruption,
        # which must remain visible/pending rather than be called inapplicable.
        has_deliveries = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_deliveries'").fetchone()
        if has_deliveries and db.execute('SELECT 1 FROM managed_deliveries WHERE job_id=?', (job_id,)).fetchone():
            raise ValueError('Managed cleanup target is unavailable.')
        return True
    target = job_target.load(db, job_id)
    if target['recipe'].get('schema_kind') != 'managed-build': return True
    protected = managed_services.protected_images(db) | managed_delivery.protected_images(db)
    candidates = {item['definition']['artifact']['image'] for item in target['recipe']['managed_target']['services']}
    existing = set(run([DOCKER, 'image', 'ls', '--all', '--no-trunc', '--quiet'], capture=True).splitlines())
    return remove_cleanup_images(candidates & existing - protected)


def process_managed_migration(db, job_id, authorize, *, recovery=False):
    """Called under control.LOCK, never a SQLite transaction."""
    import migration_jobs, migration_bindings, migration_coordinator
    migration_jobs.initialize(db)
    if not db.execute('SELECT 1 FROM job_migration_targets WHERE job_id=?', (job_id,)).fetchone():
        # Historical jobs predate this contract; never attach a newly configured
        # database authority to them retroactively.
        import job_target
        target = job_target.load(db, job_id)
        if migration_bindings.capture(db, target['project'], target['environment']) is not None:
            raise ValueError('Historical job has no pinned migration target.')
        return True
    record = migration_jobs.load(db, job_id)
    if record['target']['binding'] is None: return True
    if recovery and record['state'] in ('captured', 'source-ready', 'planned', 'failed'):
        # No apply intent, or reconciliation proved no commit. Recovery may
        # observe unchanged containers and release this failed delivery. It
        # must not initiate migrations while recovering an interrupted run.
        return True
    from database_candidates import resolve
    from database_inventory import DockerInventory
    from migration_runner import MigrationHelper
    socket = os.environ.get('DOCKER_HOST', 'unix:///run/docker-isolation.sock')
    inventory = DockerInventory(docker=DOCKER, socket=socket, timeout=8)
    def candidate(project, environment, reference, **options):
        return resolve(db, project, environment, reference, inventory=inventory,
                       provenance_resolver=control.review_compose, **options)
    directory = STATE / 'migration-helper'
    directory.mkdir(mode=0o700, exist_ok=True)
    helper = MigrationHelper(docker=DOCKER, socket=socket,
        image=os.environ.get('MIGRATION_HELPER_IMAGE'), state_dir=directory)
    # Exact owned leftover helpers are terminated before inspecting the ledger;
    # ambiguous creation remains fenced rather than starting another executor.
    helper.reconcile_pending()
    verified = migration_coordinator.process(db, job_id, helper=helper,
        resolve_candidate=candidate, key_path=os.environ.get('DATABASE_INSPECTION_KEY_FILE'), authorize=authorize)
    return verified or (recovery and migration_jobs.load(db, job_id)['state'] == 'failed')


def recover_runtime_settings(db):
    import service_runtime_operations
    with control.LOCK:
        if control.JOURNAL.exists() or (STATE / 'transaction.json').exists():return
        def authorize(project):
            if project not in effective_projects(db,CONFIG):raise ValueError('Runtime project authority changed.')
        import managed_lifecycle
        managed_lifecycle.recover_pending(db,docker=DOCKER,authorize_project=authorize)
        service_runtime_operations.recover_pending(db,docker=DOCKER,compose=COMPOSE,authorize_project=authorize)


def process_managed_delivery(db, job_id):
    import artifact_publication, managed_delivery, managed_rollout
    if artifact_publication.pending(db, job_id):
        return False
    worker_id = 'worker-' + str(os.getpid())
    with control.LOCK:
        if control.JOURNAL.exists() or (STATE / 'transaction.json').exists():
            return False
        try:
            managed_delivery.prepare(db, job_id)
            claim = managed_delivery.claim(db, job_id, worker_id, lease_seconds=300)
            if claim is None:
                outcome = managed_delivery.public(db, job_id)
                if outcome['status'] == 'succeeded':
                    set_status(db, job_id, 'succeeded', 'Managed containers were updated and runtime health verified.')
                elif outcome['status'] in ('failed', 'recovery-required'):
                    set_status(db, job_id, 'failed', 'Managed delivery requires runtime reconciliation.')
                return False
            def migration_authorize():
                managed_delivery.renew_claim(db, job_id, worker_id, lease_seconds=300)
            if not process_managed_migration(db, job_id, migration_authorize):
                raise ValueError('Migration execution has not been verified.')
            for item in claim['services']:
                if item['state'] != 'pending': continue
                def authorize():
                    managed_delivery.renew_claim(db, job_id, worker_id, lease_seconds=300)
                try:
                    binding = managed_rollout.rollout(db, str(job_id)+'-'+item['service_id'], item,
                        docker=DOCKER, compose=COMPOSE, authorize=authorize)
                    managed_delivery.complete_service(db, job_id, worker_id, item['service_id'], binding)
                except managed_rollout.RolloutError as error:
                    restored = getattr(error, 'restored_binding', None)
                    if restored is not None:
                        managed_delivery.rollback_service(db, job_id, worker_id, item['service_id'], restored)
                    managed_delivery.fail(db, job_id, worker_id, recovery_required=restored is None)
                    set_status(db, job_id, 'failed', 'Runtime verification failed. '+
                        ('The previous image was restored and verified.' if restored is not None else 'Runtime reconciliation is required.'))
                    return False
            outcome = managed_delivery.public(db, job_id)
            if outcome['status'] != 'succeeded':
                raise ValueError('Managed delivery did not verify all selected containers.')
            set_status(db, job_id, 'succeeded', 'Managed containers were updated and runtime health verified.')
            schedule_cleanup(db, job_id, 'previous-images', locked=True)
            return True
        except (ValueError, OSError, KeyError):
            try: managed_delivery.fail(db, job_id, worker_id, recovery_required=True)
            except ValueError:
                managed_delivery.abandon_unstarted(db, job_id)
            set_status(db, job_id, 'failed', 'Managed target changed or runtime reconciliation is required. Deployment was not marked successful.')
            return False


def recover_managed_deliveries(db):
    """Reconcile interrupted managed mutations through observations only."""
    import managed_delivery, managed_rollout
    managed_delivery.initialize(db)
    changed = False
    with control.LOCK:
        if control.JOURNAL.exists() or (STATE / 'transaction.json').exists(): return False
        candidates = db.execute("SELECT job_id FROM managed_deliveries WHERE state='recovery-required' OR (state IN ('running','reconciling') AND expires<=?)", (time.time(),)).fetchall()
        import uuid
        for row in candidates:
            worker_id = 'recovery-' + str(uuid.uuid4())
            job_id = row['job_id']
            try:
                plan = managed_delivery.claim_recovery(db, job_id, worker_id, lease_seconds=300)
            except (ValueError, KeyError, TypeError):
                continue
            if plan is None: continue
            outcomes = {}
            def authorize():
                managed_delivery.renew_recovery(db, job_id, worker_id, lease_seconds=300)
                return managed_delivery.authorize_recovery(db, job_id, worker_id)
            try:
                if not process_managed_migration(db, job_id, authorize, recovery=True): continue
            except (ValueError, OSError, KeyError, TypeError):
                continue
            for item in plan['services']:
                try:
                    observed = managed_rollout.reconcile(db, str(job_id)+'-'+item['service_id'], item,
                                                        docker=DOCKER, authorize=authorize)
                    outcome = {'verified-new':'new', 'verified-old':'old'}.get(observed['outcome'], 'unknown')
                    outcomes[item['service_id']] = {'outcome':outcome, 'binding':observed.get('binding')}
                except (ValueError, OSError, KeyError, TypeError):
                    outcomes[item['service_id']] = {'outcome':'unknown'}
            try:
                result = managed_delivery.finish_recovery(db, job_id, worker_id, outcomes)
            except (ValueError, KeyError, TypeError):
                # No further mutation or ordinary deployment may steal this
                # lease. Expiration returns it to bounded read-only recovery.
                continue
            changed = True
            status = 'succeeded' if result['status'] == 'succeeded' else 'failed'
            detail = ('Interrupted delivery was reconciled: all replacement containers are healthy.' if status == 'succeeded'
                      else 'Runtime observations were reconciled; unchanged or restored services remain on their previous image.'
                      if result['status'] == 'failed' else 'Runtime identity remains uncertain. Automatic reconciliation will retry without repeating deployment commands.')
            old = db.execute('SELECT status,detail FROM jobs WHERE id=?',(job_id,)).fetchone()
            if old and (old['status'] != status or old['detail'] != detail):
                set_status(db, job_id, status, detail)
            if status == 'succeeded':
                schedule_cleanup(db, job_id, 'previous-images', locked=True)
    return changed


def process_delivery_proof_once(db):
    import ingress_proof_refresh
    with control.LOCK:
        if control.JOURNAL.exists() or (STATE / 'transaction.json').exists():return False
        import runtime_proof_refresh
        configured=runtime_proof_refresh.process_once(db,root=control.ROOT / 'managed-ingress',docker=DOCKER)
        delivered=ingress_proof_refresh.process_once(db,root=control.ROOT / 'managed-ingress',docker=DOCKER)
        return configured or delivered


def resume_managed_deliveries(db):
    import job_target, artifact_publication
    artifact_publication.initialize(db)
    for row in db.execute("SELECT jobs.id FROM jobs JOIN artifact_publications p ON p.job_id=jobs.id WHERE jobs.status IN ('deploying','interrupted') AND p.status='succeeded'").fetchall():
        try:
            target = job_target.load(db, row['id'])
            if target['recipe'].get('schema_kind') == 'managed-build':
                process_managed_delivery(db, row['id'])
        except (ValueError, KeyError, TypeError):
            set_status(db, row['id'], 'failed', 'Managed delivery target could not be verified. Runtime reconciliation is required.')


def process_service_once(db):
    """Serialize provisioning with deployments and direct container operations."""
    import managed_services
    import service_provisioning
    with control.LOCK:
        if control.JOURNAL.exists() or (STATE / 'transaction.json').exists():
            return False
        worker_id = 'worker-' + str(os.getpid())
        claim = managed_services.claim_next(db, worker_id, lease_seconds=300)
        if claim is None:
            return False
        operation_id = claim['operation_id']
        def authorize():
            definition = managed_services.authorize_claim(db, operation_id, worker_id)
            managed_services.renew_claim(db, operation_id, worker_id, lease_seconds=300)
            return definition
        try:
            definition = authorize()
            managed_root = control.ROOT / 'managed'
            if managed_root.is_symlink():
                raise StepFailure('Managed service directory cannot be a symbolic link.')
            managed_root.mkdir(mode=0o700, parents=False, exist_ok=True)
            binding = service_provisioning.provision(db, operation_id, definition,
                root=managed_root, docker=DOCKER, compose=COMPOSE, authorize=authorize)
            managed_services.finish(db, operation_id, worker_id, binding, definition['desired_state'])
        except Exception:
            managed_services.fail(db, operation_id, worker_id, recovery_required=True)
        return True


def observe_domain_executor(db, settings, client):
    """At most one bounded, read-only host capability probe per 30 seconds."""
    import hashlib
    fingerprint=hashlib.sha256(json.dumps(settings,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    previous=db.execute("SELECT updated,value FROM snapshots WHERE name='ingress-worker'").fetchone()
    if previous:
        try:
            value=json.loads(previous['value'])
            if (type(previous['updated']) in (int,float) and 0<=time.time()-previous['updated']<30
                    and isinstance(value,dict) and value.get('configuration_digest')==fingerprint
                    and value.get('configured') is True):
                if value.get('state')=='available':return
                if value.get('state')=='unavailable':
                    unavailable=True
                else:unavailable=False
            else:unavailable=False
        except (ValueError,TypeError):unavailable=False
        if unavailable:raise ValueError('Domain host capability is unavailable.')
    try:
        evidence=client.call('available_ports',{})
        ports=evidence.get('ports') if isinstance(evidence,dict) else None
        observed=evidence.get('observed') if isinstance(evidence,dict) else None
        if (not isinstance(ports,list) or len(ports)>4096 or any(type(port) is not int or not 1024<=port<=65535 for port in ports)
                or len(set(ports))!=len(ports) or type(observed) not in (int,float) or not 0<=time.time()-observed<=30):
            raise ValueError('Host capability observation is invalid.')
    except Exception:
        snapshot(db,'ingress-worker',{'state':'unavailable','configured':True,'configuration_digest':fingerprint})
        raise ValueError('Domain host capability is unavailable.') from None
    snapshot(db,'ingress-worker',{'state':'available','configured':True,'configuration_digest':fingerprint})


def process_domain_once(db):
    """Run one domain phase under the same lock as delivery and direct controls."""
    import domain_worker
    import domain_execution
    from ingress_host_transport import HostClient
    try:
        settings = domain_worker.configuration()
    except (ValueError,TypeError,OSError):
        snapshot(db,'ingress-worker',{'state':'unavailable','configured':False})
        raise ValueError('Domain worker configuration is unavailable.') from None
    if settings is None:
        snapshot(db,'ingress-worker',{'state':'disabled','configured':False})
        return False
    with control.LOCK:
        if control.JOURNAL.exists() or (STATE / 'transaction.json').exists():
            snapshot(db,'ingress-worker',{'state':'blocked','configured':True})
            return False
        client = HostClient(settings['socket'], expected_server_uid=0, timeout=60)
        observe_domain_executor(db,settings,client)
        gateway = domain_worker.Gateway(root=control.ROOT / 'managed-ingress',
            docker=DOCKER, compose=COMPOSE, host_client=client,
            image=settings['image'], resources=settings['resources'])
        import ingress_external
        import domain_renewal
        if domain_renewal.process_once(db,worker_id='worker-' + str(os.getpid()),host_client=client,
                key_path=os.environ.get('INTEGRATION_KEY_FILE', '/config/integrations.key')):
            return True
        return domain_execution.process_once(db, external_verifier=ingress_external.verify, worker_id='worker-' + str(os.getpid()),
            gateway=gateway, host_client=client,
            key_path=os.environ.get('INTEGRATION_KEY_FILE', '/config/integrations.key'))


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
                import artifact_publication
                artifact_publication.process(db, inspect_artifact_image)
                process_cleanup(db)
                recover_runtime_settings(db)
                recover_managed_deliveries(db)
                resume_managed_deliveries(db)
                process_delivery_proof_once(db)
                process_service_once(db)
                try:
                    process_domain_once(db)
                except (ValueError, OSError):
                    pass  # process_domain_once already recorded sanitized configuration/helper evidence.
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
                            source = db.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()
                            if source['status'] not in ('validated', 'succeeded'):
                                set_status(db, job['id'], 'failed', phase + ' — ' + detail)
                        finally:
                            # Only this job's unreferenced images; never force
                            # delete a running image and never touch volumes.
                            schedule_cleanup(db, job['id'], 'job-images')
                            schedule_cleanup(db, job['id'], 'build-cache')
            except (StepFailure, OSError):
                snapshot(db, 'worker', {'state': 'unavailable', 'detail': 'Docker could not be observed. Previous observations are stale.'})
            time.sleep(5)


if __name__ == '__main__':
    main()
