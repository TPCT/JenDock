"""Immutable enqueue-time targets. Recipes are private worker data, never jobs API data.

The caller owns enqueue/registry transactions. Store a target in the same write
transaction as its job; this module neither commits nor looks up current policy.
Deleting expired jobs and then their orphan targets is the retention owner's job.
"""
import hashlib
import json
import re
import sqlite3
import time

from store import valid_branch

VERSION = 1
ENVIRONMENTS = ('development', 'production', 'legacy-unclassified')
CAPTURE_FIELDS = ('project', 'environment', 'repository', 'repository_id', 'branch', 'sha',
                  'mode', 'recipe_revision', 'recipe', 'project_revision', 'environment_revision')
PUBLIC_FIELDS = ('schema_version', 'project', 'environment', 'repository', 'repository_id',
                 'branch', 'sha', 'mode', 'recipe_revision', 'recipe_digest',
                 'project_revision', 'environment_revision', 'fingerprint')


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS job_targets(
        job_id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL,
        document TEXT NOT NULL, created REAL NOT NULL)''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS job_target_immutable_update
        BEFORE UPDATE ON job_targets BEGIN SELECT RAISE(ABORT,'Job targets are immutable'); END''')
    # Also protect against INSERT OR REPLACE, which need not fire DELETE triggers.
    db.execute('''CREATE TRIGGER IF NOT EXISTS job_target_immutable_replace
        BEFORE INSERT ON job_targets WHEN EXISTS(SELECT 1 FROM job_targets WHERE job_id=NEW.job_id)
        BEGIN SELECT RAISE(ABORT,'Job targets are immutable'); END''')


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ValueError('Target must contain valid JSON values.') from None


def positive(value, label, optional=False):
    if optional and value is None:
        return
    if type(value) is not int or not 0 < value < 2 ** 63:
        raise ValueError(label + ' must be a positive integer.')


def token(value, pattern, label):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError('Invalid ' + label + '.')


def _path(value):
    return isinstance(value, str) and value.startswith('/') and not any(ord(c) < 32 for c in value)


def capture(project, environment, repository, repository_id, branch, sha, mode,
            recipe_revision, recipe, *, project_revision=None, environment_revision=None):
    token(project, r'[a-z0-9][a-z0-9-]{0,63}', 'project identifier')
    if not isinstance(environment, str) or environment not in ENVIRONMENTS:
        raise ValueError('Choose an explicit environment or legacy-unclassified.')
    token(repository, r'[A-Za-z0-9][A-Za-z0-9-]{0,99}/[A-Za-z0-9_.-]{1,100}', 'repository')
    positive(repository_id, 'Repository ID')
    if not valid_branch(branch):
        raise ValueError('Invalid branch.')
    token(sha, r'[a-f0-9]{40}', 'commit SHA')
    if not isinstance(mode, str) or mode not in ('validate', 'deploy'):
        raise ValueError('Invalid execution mode.')
    positive(recipe_revision, 'Recipe revision', optional=environment == 'legacy-unclassified')
    positive(project_revision, 'Project revision', optional=True)
    positive(environment_revision, 'Environment revision', optional=True)
    if environment == 'legacy-unclassified' and environment_revision is not None:
        raise ValueError('Unclassified legacy targets have no environment revision.')
    if not isinstance(recipe, dict) or not recipe:
        raise ValueError('A configured private recipe is required.')
    if recipe.get('schema_kind') == 'artifact-build':
        if mode != 'validate' or environment == 'legacy-unclassified':
            raise ValueError('Source build profiles only support explicit environment build validation.')
        import build_profiles
        build_profiles.validate_recipe(recipe)
    else:
        # Capture stable target identity; deeper build/test recipe validation remains
        # with the deployment engine. No path is resolved against mutable disk here.
        token(recipe.get('compose_project'), r'[a-z0-9][a-z0-9_-]*', 'Compose project')
        files, services = recipe.get('compose_files'), recipe.get('services')
        if not isinstance(files, list) or not files or not all(_path(path) for path in files):
            raise ValueError('Absolute Compose file paths are required.')
        if not isinstance(services, list) or not services or len(set(canonical(item) for item in services)) != len(services):
            raise ValueError('Unique deployment services are required.')
        for service in services:
            token(service, r'[A-Za-z0-9][A-Za-z0-9_.-]*', 'service')
        if recipe.get('env_file') is not None and not _path(recipe['env_file']):
            raise ValueError('Invalid environment file path.')
    recipe_json = canonical(recipe)
    target = {'schema_version': VERSION, 'project': project, 'environment': environment,
              'repository': repository, 'repository_id': repository_id, 'branch': branch,
              'sha': sha, 'mode': mode, 'recipe_revision': recipe_revision,
              'project_revision': project_revision, 'environment_revision': environment_revision,
              'recipe': json.loads(recipe_json),
              'recipe_digest': hashlib.sha256(recipe_json.encode()).hexdigest()}
    target['fingerprint'] = hashlib.sha256(canonical(target).encode()).hexdigest()
    return target


def _validated(target):
    if not isinstance(target, dict) or set(target) != set(CAPTURE_FIELDS) | {'schema_version', 'recipe_digest', 'fingerprint'}:
        raise ValueError('Invalid job target document.')
    if type(target['schema_version']) is not int or target['schema_version'] != VERSION:
        raise ValueError('Unsupported job target schema version.')
    expected = capture(**{key: target[key] for key in CAPTURE_FIELDS})
    if target['recipe_digest'] != expected['recipe_digest'] or target['fingerprint'] != expected['fingerprint']:
        raise ValueError('Job target fingerprint does not match its content.')
    return expected


def matches(target, candidate):
    return _validated(target)['fingerprint'] == _validated(candidate)['fingerprint']


def _public(target):
    result = {key: target[key] for key in PUBLIC_FIELDS}
    if target['recipe'].get('schema_kind') == 'artifact-build':
        result['source_revision'] = target['recipe_revision']
    return result


def store(db, job_id, target):
    positive(job_id, 'Job ID')
    target = _validated(target)
    initialize(db)
    job = db.execute('SELECT project,branch,sha,mode FROM jobs WHERE id=?', (job_id,)).fetchone()
    if job is None or any(job[key] != target[key] for key in ('project', 'branch', 'sha', 'mode')):
        raise ValueError('Job identity does not match the captured target.')
    old = db.execute('SELECT document FROM job_targets WHERE job_id=?', (job_id,)).fetchone()
    if old is not None:
        if not matches(json.loads(old['document']), target):
            raise ValueError('This job already has a different immutable target.')
        return _public(target)
    try:
        db.execute('INSERT INTO job_targets VALUES(?,?,?,?)',
                   (job_id, target['fingerprint'], canonical(target), time.time()))
    except sqlite3.IntegrityError:
        # Independent callers may race; return only an exact prior snapshot.
        old = db.execute('SELECT document FROM job_targets WHERE job_id=?', (job_id,)).fetchone()
        if old is None or not matches(json.loads(old['document']), target):
            raise ValueError('This job already has a different immutable target.') from None
    return _public(target)


def load(db, job_id):
    positive(job_id, 'Job ID')
    initialize(db)
    row = db.execute('SELECT document,fingerprint FROM job_targets WHERE job_id=?', (job_id,)).fetchone()
    if row is None:
        raise ValueError('This run has no pinned target. Explicit reconciliation is required.')
    target = _validated(json.loads(row['document']))
    if target['fingerprint'] != row['fingerprint']:
        raise ValueError('Stored target fingerprint is inconsistent.')
    return target


def public(db, job_id):
    return _public(load(db, job_id))
