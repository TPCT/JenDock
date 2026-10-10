"""Database-owned project registry. Public readers never return delivery recipes.

Legacy workloads stay unclassified until explicitly adopted. Registry mutations
only save metadata: no Docker, DNS, Git or database provisioning is performed.
"""
import copy
import hashlib
import json
import re
import time
from contextlib import contextmanager

ENVIRONMENTS = ('development', 'production')
PROJECT_FIELDS = {'repository', 'repository_id', 'label', 'legacy_binding'}
ENVIRONMENT_FIELDS = {'branch', 'automatic', 'wake_on_push', 'provision_on_push', 'recipe_revision'}


def initialize(db):
    # execute, rather than executescript, preserves the caller's transaction.
    statements = [
        'CREATE TABLE IF NOT EXISTS registry_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
        '''CREATE TABLE IF NOT EXISTS registry_projects(
            id TEXT PRIMARY KEY, revision INTEGER NOT NULL, repository TEXT NOT NULL,
            repository_id INTEGER, label TEXT NOT NULL, legacy_binding TEXT,
            legacy_document TEXT, created REAL NOT NULL, updated REAL NOT NULL,
            CHECK(legacy_binding IS NULL OR legacy_binding IN ('development','production')))''',
        '''CREATE TABLE IF NOT EXISTS registry_environments(
            project TEXT NOT NULL, name TEXT NOT NULL, revision INTEGER NOT NULL,
            branch TEXT NOT NULL, automatic INTEGER NOT NULL, wake_on_push INTEGER NOT NULL,
            provision_on_push INTEGER NOT NULL, state TEXT NOT NULL, recipe_revision INTEGER,
            PRIMARY KEY(project,name), CHECK(name IN ('development','production')))''',
        '''CREATE TABLE IF NOT EXISTS registry_recipes(
            id INTEGER PRIMARY KEY, project TEXT NOT NULL, digest TEXT NOT NULL,
            document TEXT NOT NULL, created REAL NOT NULL, UNIQUE(project,digest))''',
        '''CREATE TRIGGER IF NOT EXISTS registry_recipe_immutable_update
            BEFORE UPDATE ON registry_recipes BEGIN SELECT RAISE(ABORT,'Recipes are immutable'); END''',
        '''CREATE TRIGGER IF NOT EXISTS registry_recipe_immutable_delete
            BEFORE DELETE ON registry_recipes BEGIN SELECT RAISE(ABORT,'Recipes are immutable'); END''',
    ]
    for statement in statements:
        db.execute(statement)


@contextmanager
def transaction(db):
    if db.in_transaction:
        raise ValueError('Registry writes require a dedicated transaction.')
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError):
        raise ValueError('Configuration must contain valid JSON values.') from None


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', value):
        raise ValueError('Invalid project identifier.')


def revision_number(value):
    if type(value) is not int or value < 1:
        raise ValueError('Invalid or stale revision.')


def branch_name(value):
    from store import valid_branch
    if not valid_branch(value):
        raise ValueError('Invalid branch name.')


def project_values(values, creating=False):
    if not isinstance(values, dict) or set(values) - PROJECT_FIELDS:
        raise ValueError('Unsupported project fields.')
    if creating and 'repository' not in values:
        raise ValueError('Repository is required.')
    if 'repository' in values and (not isinstance(values['repository'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,99}/[A-Za-z0-9_.-]{1,100}', values['repository'])):
        raise ValueError('Repository must be an owner/repository identifier.')
    if 'repository_id' in values and (type(values['repository_id']) is not int or not 0 < values['repository_id'] < 2**63):
        raise ValueError('Repository ID must be a positive integer.')
    if 'label' in values and (not isinstance(values['label'], str) or not 1 <= len(values['label'].strip()) <= 120 or any(ord(c) < 32 for c in values['label'])):
        raise ValueError('Project name must contain 1 to 120 printable characters.')
    if 'legacy_binding' in values and values['legacy_binding'] not in (None, *ENVIRONMENTS):
        raise ValueError('Legacy binding must identify Development or Production.')


def _insert(db, project, values, legacy=None):
    now = time.time()
    db.execute('INSERT INTO registry_projects VALUES(?,?,?,?,?,?,?,?,?)',
               (project, 1, values['repository'], values.get('repository_id'), values.get('label', project), None,
                canonical(legacy) if legacy is not None else None, now, now))
    for name in ENVIRONMENTS:
        db.execute('INSERT INTO registry_environments VALUES(?,?,?,?,?,?,?,?,?)',
                   (project, name, 1, name, 1, 0, 0, 'not-provisioned', None))


def imported(db):
    initialize(db)
    return db.execute("SELECT 1 FROM registry_meta WHERE key='legacy-import' ").fetchone() is not None


def import_legacy(db, data, saved_settings):
    initialize(db)
    with transaction(db):
        if imported(db):
            return {'imported': [], 'skipped': [], 'completed': True}
        if not isinstance(data, dict):
            raise ValueError('Invalid legacy project collection.')
        settings = saved_settings if isinstance(saved_settings, dict) else {row['project']: dict(row) for row in saved_settings}
        added, skipped = [], []
        for project, original in data.items():
            identifier(project)
            if not isinstance(original, dict):
                raise ValueError('Invalid legacy project configuration.')
            values = {key: original[key] for key in ('repository', 'repository_id', 'label') if key in original}
            project_values(values, creating=True)
            if db.execute('SELECT 1 FROM registry_projects WHERE id=?', (project,)).fetchone():
                skipped.append(project)
                continue
            legacy = copy.deepcopy(original)
            saved = settings.get(project)
            if saved:
                legacy.update(branch=saved['branch'], automatic=bool(saved['automatic']))
            legacy.setdefault('automatic', True)
            branch_name(legacy.get('branch'))
            _insert(db, project, values, legacy)
            if legacy.get('recipe'):
                document = canonical(legacy['recipe'])
                db.execute('INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,?)',
                           (project, hashlib.sha256(document.encode()).hexdigest(), document, time.time()))
            added.append(project)
        evidence = {'imported': added, 'skipped': skipped, 'completed': True, 'created': time.time(),
                    'source': 'legacy-json-and-saved-settings', 'binding': 'unresolved'}
        db.execute('INSERT INTO registry_meta VALUES(?,?)', ('legacy-import', canonical(evidence)))
        return {key: evidence[key] for key in ('imported', 'skipped', 'completed')}


def get_environment(db, project, name):
    if not isinstance(name, str) or name not in ENVIRONMENTS:
        raise ValueError('Unknown environment.')
    row = db.execute('SELECT * FROM registry_environments WHERE project=? AND name=?', (project, name)).fetchone()
    if row is None:
        raise ValueError('Project not found.')
    result = dict(row)
    for key in ('automatic', 'wake_on_push', 'provision_on_push'):
        result[key] = bool(result[key])
    return result


def get_project(db, project):
    initialize(db)
    identifier(project)
    row = db.execute('SELECT * FROM registry_projects WHERE id=?', (project,)).fetchone()
    if row is None:
        raise ValueError('Project not found.')
    result = dict(row)
    legacy = json.loads(result.pop('legacy_document') or 'null')
    result['legacy_settings'] = ({'branch': legacy['branch'], 'automatic': bool(legacy['automatic']),
                                  'build_ready': bool(legacy.get('recipe'))} if legacy is not None else None)
    result['environments'] = {name: get_environment(db, project, name) for name in ENVIRONMENTS}
    return result


def list_projects(db):
    initialize(db)
    return {row['id']: get_project(db, row['id']) for row in db.execute('SELECT id FROM registry_projects ORDER BY id')}


def legacy_projects(db):
    """Trusted worker compatibility projection. Never return this through an API."""
    initialize(db)
    result = {}
    for row in db.execute('SELECT * FROM registry_projects ORDER BY id'):
        document = json.loads(row['legacy_document'] or '{}')
        document.update(repository=row['repository'], repository_id=row['repository_id'], label=row['label'])
        if not row['legacy_document']:
            document.update(branch='development', automatic=False, enabled=False, container_prefixes=[], image_prefixes=[],
                            reason='Not provisioned. Configure an environment before deployment.')
        if row['legacy_binding']:
            environment = get_environment(db, row['id'], row['legacy_binding'])
            recipe = db.execute('SELECT document,digest FROM registry_recipes WHERE id=? AND project=?',
                                (environment['recipe_revision'], row['id'])).fetchone()
            if recipe is None:
                raise ValueError('The bound environment recipe is missing or belongs to another project.')
            try:
                pinned = json.loads(recipe['document'])
                digest = hashlib.sha256(canonical(pinned).encode()).hexdigest()
            except (ValueError, TypeError):
                raise ValueError('The bound environment recipe is invalid.') from None
            if not isinstance(pinned, dict) or not pinned or digest != recipe['digest']:
                raise ValueError('The bound environment recipe digest or document is inconsistent.')
            document.update(branch=environment['branch'], automatic=environment['automatic'],
                            environment=environment['name'], recipe=pinned)
        result[row['id']] = document
    return result


def create_project(db, project, values):
    identifier(project)
    project_values(values, creating=True)
    if values.get('legacy_binding') is not None:
        raise ValueError('A new project has no legacy workload to adopt.')
    initialize(db)
    with transaction(db):
        if db.execute('SELECT 1 FROM registry_projects WHERE id=?', (project,)).fetchone():
            raise ValueError('Project already exists.')
        _insert(db, project, values)
        result = get_project(db, project)
    return result


def update_project(db, project, revision, changes):
    identifier(project); revision_number(revision); project_values(changes)
    initialize(db)
    with transaction(db):
        current = get_project(db, project)
        if current['revision'] != revision:
            raise ValueError('Project settings changed. Reload before saving.')
        if current['legacy_settings'] and any(key in changes and changes[key] != current[key] for key in ('repository', 'repository_id')):
            raise ValueError('A legacy workload repository requires an explicit target reconciliation.')
        binding = changes.get('legacy_binding', current['legacy_binding'])
        if current['legacy_binding'] is not None and binding != current['legacy_binding']:
            raise ValueError('An adopted legacy workload cannot be reassigned through settings.')
        if binding is not None and current['legacy_binding'] is None:
            if not current['legacy_settings']:
                raise ValueError('This project has no legacy workload.')
            env = current['environments'][binding]
            if env['revision'] != 1 or env['state'] != 'not-provisioned':
                raise ValueError('Environment settings changed; reconcile them before legacy adoption.')
            recipe = db.execute('SELECT id FROM registry_recipes WHERE project=? ORDER BY id LIMIT 1', (project,)).fetchone()
            legacy = current['legacy_settings']
            db.execute('UPDATE registry_environments SET revision=revision+1,branch=?,automatic=?,state=?,recipe_revision=? WHERE project=? AND name=?',
                       (legacy['branch'], int(legacy['automatic']), 'legacy-bound', recipe['id'] if recipe else None, project, binding))
        new = dict(current, **changes)
        db.execute('UPDATE registry_projects SET revision=revision+1,repository=?,repository_id=?,label=?,legacy_binding=?,updated=? WHERE id=?',
                   (new['repository'], new['repository_id'], new['label'], binding, time.time(), project))
        result = get_project(db, project)
    return result


def update_environment(db, project, name, revision, changes):
    identifier(project); revision_number(revision)
    if not isinstance(changes, dict) or set(changes) - ENVIRONMENT_FIELDS:
        raise ValueError('Unsupported environment settings.')
    if 'branch' in changes:
        branch_name(changes['branch'])
    for key in ('automatic', 'wake_on_push', 'provision_on_push'):
        if key in changes and type(changes[key]) is not bool:
            raise ValueError('Environment switches must be boolean.')
    initialize(db)
    with transaction(db):
        current = get_environment(db, project, name)
        if current['revision'] != revision:
            raise ValueError('Environment settings changed. Reload before saving.')
        if 'recipe_revision' in changes and changes['recipe_revision'] != current['recipe_revision']:
            # No verified provisioner or recipe editor yet: references cannot
            # silently bind a second environment to the legacy live database.
            raise ValueError('Recipe bindings require a verified provisioning operation.')
        updated = dict(current, **changes)
        db.execute('UPDATE registry_environments SET revision=revision+1,branch=?,automatic=?,wake_on_push=?,provision_on_push=? WHERE project=? AND name=?',
                   (updated['branch'], int(updated['automatic']), int(updated['wake_on_push']), int(updated['provision_on_push']), project, name))
        result = get_environment(db, project, name)
    return result


def save_legacy_settings(db, project, branch, automatic):
    identifier(project); branch_name(branch)
    if type(automatic) is not bool:
        raise ValueError('Automatic deployment must be boolean.')
    initialize(db)
    with transaction(db):
        current = get_project(db, project)
        if current['legacy_settings'] is None:
            raise ValueError('Configure this project through its environment settings.')
        row = db.execute('SELECT legacy_document FROM registry_projects WHERE id=?', (project,)).fetchone()
        document = json.loads(row['legacy_document'])
        document.update(branch=branch, automatic=automatic)
        db.execute('UPDATE registry_projects SET legacy_document=?,revision=revision+1,updated=? WHERE id=?',
                   (canonical(document), time.time(), project))
        if current['legacy_binding']:
            db.execute('UPDATE registry_environments SET branch=?,automatic=?,revision=revision+1 WHERE project=? AND name=?',
                       (branch, int(automatic), project, current['legacy_binding']))
        result = get_project(db, project)
    return result
