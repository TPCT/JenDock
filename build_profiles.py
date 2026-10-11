"""Revisioned repository build inputs, independent of runtime workload ownership."""
import copy
import hashlib
import json
import re
import time

import registry

FIELDS = {'dockerfile', 'context', 'target', 'test_command', 'command', 'healthcheck', 'user'}


def initialize(db):
    registry.initialize(db)
    db.execute('''CREATE TABLE IF NOT EXISTS environment_build_profiles(
        project TEXT NOT NULL,environment TEXT NOT NULL,revision INTEGER NOT NULL,
        recipe_revision INTEGER NOT NULL,PRIMARY KEY(project,environment))''')


def _path(value, label, directory=False):
    if (not isinstance(value, str) or not 1 <= len(value) <= 240
            or value.startswith('/') or '\\' in value or ':' in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or (value != '.' or not directory) and any(part in ('', '.', '..', '.git') for part in value.split('/'))):
        raise ValueError(label + ' must be a normalized path inside the repository.')


def _argv(value, label):
    if (not isinstance(value, list) or not 1 <= len(value) <= 40
            or not all(isinstance(arg, str) and 1 <= len(arg) <= 2048
                       and not any(ord(char) < 32 or ord(char) == 127 for char in arg) for arg in value)):
        raise ValueError(label + ' must contain 1 to 40 nonempty arguments without control characters.')


def validate_http_endpoint(value):
    """Trusted internal HTTP address metadata; never an arbitrary upstream URL."""
    if not isinstance(value, dict) or set(value) != {'port', 'health_path'}:
        raise ValueError('HTTP endpoint requires exactly a port and health path.')
    if type(value['port']) is not int or not 1 <= value['port'] <= 65535:
        raise ValueError('HTTP endpoint port must be an integer from 1 to 65535.')
    path = value['health_path']
    if (not isinstance(path, str) or not 1 <= len(path) <= 240
            or not re.fullmatch(r'/[A-Za-z0-9._~/-]*', path)
            or path != '/' and any(part in ('', '.', '..') for part in path[1:].split('/'))):
        raise ValueError('HTTP health path must be a normalized absolute path without a query or URL.')
    return copy.deepcopy(value)


def validate_values(values):
    if not isinstance(values, dict) or set(values) not in (FIELDS, FIELDS | {'http_endpoint'}):
        raise ValueError('Provide exactly Dockerfile, context, target, test/application/health arguments and runtime user.')
    _path(values['dockerfile'], 'Dockerfile'); _path(values['context'], 'Build context', True)
    target = values['target']
    if target is not None and (not isinstance(target, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', target)):
        raise ValueError('Build target must be a stage identifier or null.')
    for field in ('test_command', 'command', 'healthcheck'): _argv(values[field], field)
    if not isinstance(values['user'], str) or not re.fullmatch('[1-9][0-9]{0,8}:[0-9]{1,9}', values['user']):
        raise ValueError('Runtime user must be a non-root numeric UID:GID.')
    result = copy.deepcopy(values)
    if result.get('http_endpoint') is not None:
        result['http_endpoint'] = validate_http_endpoint(result['http_endpoint'])
    else:
        result.pop('http_endpoint', None)
    return result


def _recipe(project, environment, values):
    name = project + '-' + environment
    image = {'name': name, 'dockerfile': values['dockerfile'], 'context': values['context']}
    if values['target'] is not None: image['target'] = values['target']
    endpoint = values.get('http_endpoint')
    revision = 2 if endpoint is not None else 1
    profile = {'revision': revision, 'image_name': name, 'command': values['command'],
               'healthcheck': values['healthcheck'], 'user': values['user']}
    if endpoint is not None: profile['http_endpoint'] = copy.deepcopy(endpoint)
    return {'schema_kind': 'artifact-build', 'schema_version': revision, 'images': [image],
            'tests': [{'image': name, 'command': values['test_command']}],
            'service_profiles': {'application-v1': profile}}


def validate_recipe(recipe):
    """Fail closed before workers use persisted build instructions."""
    try:
        if (not isinstance(recipe, dict) or set(recipe) != {'schema_kind','schema_version','images','tests','service_profiles'}
                or recipe['schema_kind'] != 'artifact-build' or type(recipe['schema_version']) is not int or recipe['schema_version'] not in (1, 2)
                or not isinstance(recipe['images'], list) or len(recipe['images']) != 1
                or not isinstance(recipe['tests'], list) or len(recipe['tests']) != 1
                or not isinstance(recipe['service_profiles'], dict) or set(recipe['service_profiles']) != {'application-v1'}):
            raise ValueError('Invalid artifact build profile schema.')
        image, test, profile = recipe['images'][0], recipe['tests'][0], recipe['service_profiles']['application-v1']
        if (not isinstance(image, dict) or set(image) not in ({'name','dockerfile','context'}, {'name','dockerfile','context','target'})
                or not isinstance(test, dict) or set(test) != {'image','command'}
                or not isinstance(profile, dict) or set(profile) not in ({'revision','image_name','command','healthcheck','user'},
                    {'revision','image_name','command','healthcheck','user','http_endpoint'})
                or type(profile['revision']) is not int or profile['revision'] != recipe['schema_version']
                or 'http_endpoint' in profile and profile['revision'] != 2
                or not isinstance(image['name'], str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}-(development|production)',image['name'])
                or test['image'] != image['name'] or profile['image_name'] != image['name']):
            raise ValueError('Artifact profile identities are inconsistent.')
        if 'http_endpoint' in profile: validate_http_endpoint(profile['http_endpoint'])
        validate_values({'dockerfile':image['dockerfile'], 'context':image['context'], 'target':image.get('target'),
                         'test_command':test['command'], 'command':profile['command'], 'healthcheck':profile['healthcheck'], 'user':profile['user']})
        return copy.deepcopy(recipe)
    except (KeyError, TypeError, IndexError):
        raise ValueError('Invalid artifact build profile schema.') from None


def get_private(db, project, environment):
    initialize(db); registry.get_project(db, project); registry.get_environment(db, project, environment)
    row = db.execute('''SELECT profile.revision,profile.recipe_revision,recipe.document,recipe.digest
        FROM environment_build_profiles AS profile JOIN registry_recipes AS recipe ON recipe.id=profile.recipe_revision AND recipe.project=profile.project
        WHERE profile.project=? AND profile.environment=?''', (project, environment)).fetchone()
    if row is None:
        if db.execute('SELECT 1 FROM environment_build_profiles WHERE project=? AND environment=?', (project, environment)).fetchone():
            raise ValueError('Build profile source is unavailable.')
        return None
    recipe = validate_recipe(json.loads(row['document']))
    if hashlib.sha256(registry.canonical(recipe).encode()).hexdigest() != row['digest'] or recipe['images'][0]['name'] != project+'-'+environment:
        raise ValueError('Build profile source identity is inconsistent.')
    return {'source_revision': row['recipe_revision'], 'profile_revision': row['revision'], 'recipe': recipe}


def get(db, project, environment):
    source = get_private(db, project, environment)
    project_row = registry.get_project(db, project); env = registry.get_environment(db, project, environment)
    values = None
    if source:
        recipe = source['recipe']; image = recipe['images'][0]; profile = recipe['service_profiles']['application-v1']
        values = {'dockerfile':image['dockerfile'], 'context':image['context'], 'target':image.get('target'),
                  'test_command':recipe['tests'][0]['command'], 'command':profile['command'], 'healthcheck':profile['healthcheck'], 'user':profile['user']}
        if 'http_endpoint' in profile: values['http_endpoint'] = copy.deepcopy(profile['http_endpoint'])
    return {'project':project, 'environment':environment, 'project_revision':project_row['revision'],
            'environment_revision':env['revision'], 'profile_revision':source['profile_revision'] if source else 0,
            'source_revision':source['source_revision'] if source else None, 'values':values,
            'capabilities':{'validate':source is not None, 'deploy':False}}


def save(db, project, environment, project_revision, environment_revision, profile_revision, values):
    initialize(db); values = validate_values(values)
    registry.identifier(project)
    if not isinstance(environment, str) or environment not in registry.ENVIRONMENTS: raise ValueError('Unknown environment.')
    for revision in (project_revision, environment_revision): registry.revision_number(revision)
    if type(profile_revision) is not int or profile_revision < 0: raise ValueError('Invalid build profile revision.')
    recipe = _recipe(project, environment, values)
    document = registry.canonical(recipe); fingerprint = hashlib.sha256(document.encode()).hexdigest()
    with registry.transaction(db):
        current = get(db, project, environment)
        if any(current[key] != value for key,value in (('project_revision',project_revision), ('environment_revision',environment_revision), ('profile_revision',profile_revision))):
            raise ValueError('Project, environment or build profile changed. Reload before saving.')
        db.execute('INSERT OR IGNORE INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,?)', (project, fingerprint, document, time.time()))
        source = db.execute('SELECT id FROM registry_recipes WHERE project=? AND digest=?', (project, fingerprint)).fetchone()[0]
        db.execute('INSERT OR REPLACE INTO environment_build_profiles VALUES(?,?,?,?)', (project, environment, profile_revision+1, source))
        db.execute('UPDATE registry_projects SET revision=revision+1,updated=? WHERE id=?', (time.time(), project))
        db.execute('UPDATE registry_environments SET revision=revision+1 WHERE project=? AND name=?', (project, environment))
        return get(db, project, environment)
