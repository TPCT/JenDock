"""Revisioned repository build inputs, independent of runtime workload ownership."""
import copy
import hashlib
import json
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import registry

FIELDS = {'dockerfile', 'context', 'target', 'test_command', 'command', 'healthcheck', 'user'}


def initialize(db):
    registry.initialize(db)
    # A read snapshot must not upgrade to a writer just to repeat schema DDL.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='environment_build_profiles'").fetchone():
        return
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


ROLE_TEMPLATES = {'laravel-database-queue-v1':'queue', 'laravel-scheduler-v1':'scheduler'}


def role_profile(template, values, image_name, user):
    """Resolve closed role settings to immutable argv; declarations are not runtime proof."""
    role = ROLE_TEMPLATES.get(template)
    if role is None or not isinstance(values,dict) or set(values)!={'framework_version','instrumentation','configuration'}:
        raise ValueError('Choose a supported versioned workload role profile.')
    if not isinstance(values['framework_version'],str) or not re.fullmatch(r'12\.[0-9]{1,3}\.[0-9]{1,3}',values['framework_version']):
        raise ValueError('The workload adapter requires an exact Laravel 12 version.')
    instrument=values['instrumentation']
    if (not isinstance(instrument,dict) or set(instrument)!={'version','digest'} or instrument['version']!='1.0.0'
            or not isinstance(instrument['digest'],str) or not re.fullmatch('[a-f0-9]{64}',instrument['digest'])):
        raise ValueError('The workload instrumentation package must have a pinned supported version and digest.')
    config=values['configuration']
    if not isinstance(config,dict):raise ValueError('Workload settings must be a typed object.')
    if role=='queue':
        required={'connection','queues','timeout_seconds','retry_after_seconds','max_attempts','sleep_seconds','shutdown_grace_seconds'}
        if set(config)!=required:raise ValueError('Provide exactly the supported database queue settings.')
        def name(value):return isinstance(value,str) and re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,63}',value)
        if not (isinstance(config['connection'],str) and re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,62}',config['connection'])) or not isinstance(config['queues'],list) or not 1<=len(config['queues'])<=16 or not all(name(v) for v in config['queues']) or len(set(config['queues']))!=len(config['queues']):
            raise ValueError('Choose a connection and unique bounded queue names.')
        for key,low,high in [('timeout_seconds',1,600),('retry_after_seconds',2,3600),('max_attempts',1,100),('sleep_seconds',1,60),('shutdown_grace_seconds',1,900)]:
            if type(config[key]) is not int or not low<=config[key]<=high:raise ValueError('Queue timing and attempts must be bounded integers.')
        if config['timeout_seconds']+5>config['retry_after_seconds'] or config['shutdown_grace_seconds']<config['timeout_seconds']+5:
            raise ValueError('Queue retry and shutdown grace must exceed the worker timeout by at least five seconds.')
        command=['php','artisan','queue:work',config['connection'],'--queue='+','.join(config['queues']),
                 '--timeout='+str(config['timeout_seconds']),'--tries='+str(config['max_attempts']),
                 '--sleep='+str(config['sleep_seconds']),'--no-interaction']
    else:
        if set(config)!={'timezone','scope','connection'} or config['scope']!='default' or not isinstance(config['timezone'],str) or len(config['timezone'])>64:
            raise ValueError('Scheduler requires a timezone and the environment default schedule scope.')
        if not isinstance(config['connection'],str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,62}',config['connection']):raise ValueError('Scheduler requires an explicit instrumentation database connection.')
        try:ZoneInfo(config['timezone'])
        except (ZoneInfoNotFoundError,ValueError):raise ValueError('Scheduler timezone is unavailable.') from None
        command=['php','artisan','deploy-monitor:scheduler']
    workload={'role':role,'adapter':'laravel-database-v1' if role=='queue' else 'laravel-scheduler-v1',
              'adapter_revision':1,**copy.deepcopy(values)}
    return {'revision':1,'image_name':image_name,'command':command,
            'healthcheck':['php','artisan','deploy-monitor:health','--no-interaction'],'user':user,'workload':workload}


def validate_role_profile(template, profile):
    if not isinstance(profile,dict) or set(profile)!={'revision','image_name','command','healthcheck','user','workload'}:
        raise ValueError('Invalid immutable workload role profile.')
    workload=profile['workload']
    if not isinstance(workload,dict) or set(workload)!={'role','adapter','adapter_revision','framework_version','instrumentation','configuration'}:
        raise ValueError('Invalid workload metadata.')
    expected=role_profile(template,{k:workload[k] for k in ('framework_version','instrumentation','configuration')},profile['image_name'],profile['user'])
    if type(profile['revision']) is not int or type(workload['adapter_revision']) is not int or profile!=expected:
        raise ValueError('Workload role metadata or command differs from its trusted template.')
    return copy.deepcopy(profile)


def validate_values(values):
    if not isinstance(values, dict) or (not FIELDS<=set(values) or set(values)-FIELDS-{'http_endpoint','role_profiles'}):
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
    if 'role_profiles' in values:
        roles=values['role_profiles']
        if not isinstance(roles,dict) or not 1<=len(roles)<=2 or set(roles)-set(ROLE_TEMPLATES):
            raise ValueError('Choose one or two supported workload role profiles.')
        for template,role in roles.items():role_profile(template,role,'validation-image',values['user'])
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
    profiles={'application-v1':profile}
    for template,role in values.get('role_profiles',{}).items():profiles[template]=role_profile(template,role,name,values['user'])
    return {'schema_kind': 'artifact-build', 'schema_version': 3 if values.get('role_profiles') else revision, 'images': [image],
            'tests': [{'image': name, 'command': values['test_command']}],
            'service_profiles': profiles}


def validate_recipe(recipe):
    """Fail closed before workers use persisted build instructions."""
    try:
        if isinstance(recipe,dict) and type(recipe.get('schema_version')) is int and recipe['schema_version']==3:
            if set(recipe)!={'schema_kind','schema_version','images','tests','service_profiles'} or recipe['schema_kind']!='artifact-build':
                raise ValueError('Invalid role build recipe.')
            profiles=recipe['service_profiles']
            if not isinstance(profiles,dict) or 'application-v1' not in profiles or not 2<=len(profiles)<=3 or set(profiles)-{'application-v1'}-set(ROLE_TEMPLATES):
                raise ValueError('Invalid role profile catalog.')
            legacy=copy.deepcopy(recipe);legacy['schema_version']=profiles['application-v1']['revision'];legacy['service_profiles']={'application-v1':profiles['application-v1']}
            if legacy['schema_version'] not in (1,2):raise ValueError('Invalid application profile version.')
            validate_recipe(legacy)
            for template,profile in profiles.items():
                if template=='application-v1':continue
                validate_role_profile(template,profile)
                if profile['image_name']!=recipe['images'][0]['name'] or profile['user']!=profiles['application-v1']['user']:
                    raise ValueError('Role image and runtime user must match the verified build.')
            return copy.deepcopy(recipe)
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
        if recipe['schema_version']==3:
            values['role_profiles']={template:{k:copy.deepcopy(value['workload'][k]) for k in ('framework_version','instrumentation','configuration')} for template,value in recipe['service_profiles'].items() if template!='application-v1'}
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
