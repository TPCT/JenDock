"""Resolve registry identity once, inside the enqueue write transaction."""
import hashlib
import json

import job_target
import registry


def request_identity(body):
    environment = body.get('environment')
    fields = ('project_revision', 'environment_revision', 'recipe_revision', 'source_revision')
    if environment is None:
        if any(body.get(key) is not None for key in fields):
            raise ValueError('Choose an environment before reviewing its revisions.')
        return {'environment': 'legacy-unclassified'}
    if not isinstance(environment, str) or environment not in registry.ENVIRONMENTS:
        raise ValueError('Choose Development or Production.')
    if body.get('source_revision') is not None:
        if body.get('recipe_revision') is not None:
            raise ValueError('Choose either a source build profile or runtime recipe.')
        fields = ('project_revision', 'environment_revision', 'source_revision')
    else:
        fields = ('project_revision', 'environment_revision', 'recipe_revision')
    if any(type(body.get(key)) is not int or body[key] < 1 for key in fields):
        raise ValueError('Reload this environment and review its current project, environment and recipe revisions.')
    return {'environment': environment, **{key: body[key] for key in fields}}


def build_configured(recipe):
    return (isinstance(recipe, dict) and all(isinstance(recipe.get(key), list) and recipe[key]
                  for key in ('images', 'tests', 'http_checks', 'services', 'compose_files'))
                  and isinstance(recipe.get('compose_project'), str) and bool(recipe['compose_project']))


def readiness(recipe, services, observation, binding, environment):
    configured = build_configured(recipe)
    build_reason = '' if configured else 'This environment needs an immutable build recipe, image definitions, isolated tests and runtime checks.'
    deploy_reason = build_reason
    if configured:
        if environment.get('state') == 'not-provisioned':
            deploy_reason = 'This environment is not provisioned. Automatic provisioning is not implemented; create and verify its workload before deployment.'
        elif observation['stale'] or not observation['ownership_complete']:
            deploy_reason = 'Fresh full container ownership observations are required before deploying this environment.'
        elif binding['state'] != 'verified' or not services:
            deploy_reason = 'All registered services must have unambiguous verified ownership before deployment.'
        elif not set(recipe['services']).issubset({service.get('service') for service in services}):
            deploy_reason = 'Every deployment service must have verified ownership before deployment.'
        elif any(service.get('State') != 'running' for service in services if service.get('service') in recipe['services']):
            deploy_reason = 'This environment has stopped services. Start them explicitly before deployment; automatic wake is not implemented.'
    return {'build': {'enabled': bool(configured), 'reason': build_reason},
            'deploy': {'enabled': bool(configured and not deploy_reason), 'reason': deploy_reason},
            'provision': {'enabled': False, 'reason': 'Environment provisioning is not implemented.'}}


def public_job(db, row):
    result = dict(row)
    try:
        result['target'] = job_target.public(db, row['id'])
        result['target_state'] = 'pinned'
    except (ValueError, TypeError, KeyError):
        result.update(target=None, target_state='unassigned')
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_deliveries'").fetchone():
        if db.execute('SELECT 1 FROM managed_deliveries WHERE job_id=?',(row['id'],)).fetchone():
            import managed_delivery
            result['managed_delivery'] = managed_delivery.public(db, row['id'])
    return result


def assert_retry_environment(db, job_id, requested):
    try:
        previous = job_target.public(db, job_id)['environment']
    except (ValueError, TypeError, KeyError):
        previous = 'legacy-unclassified'
    if previous != requested:
        raise ValueError('A retry must use the original run\'s pinned environment. Start a new build to choose another target.')


def capture_registered_target(db, project, branch, sha, mode, *, environment=None,
                              environment_revision=None, project_revision=None, recipe_revision=None, source_revision=None,
                              execution_enabled=True):
    if not registry.imported(db):
        raise ValueError('Project registry must be imported before enqueueing a target.')
    registered = registry.get_project(db, project)
    binding = environment
    captured_environment_revision = None
    captured_recipe_revision = None
    if source_revision is not None:
        expected = request_identity({'environment': binding, 'project_revision': project_revision,
            'environment_revision': environment_revision, 'source_revision': source_revision, 'recipe_revision': recipe_revision})
        import build_profiles
        source = build_profiles.get(db, project, binding)
        if any(source[key] != expected[key] for key in ('project_revision', 'environment_revision', 'source_revision')):
            raise ValueError('Source or environment settings changed. Reload before building.')
        row = db.execute('SELECT document,digest FROM registry_recipes WHERE id=? AND project=?', (source_revision, project)).fetchone()
        if row is None:
            raise ValueError('Source build profile is unavailable.')
        recipe = json.loads(row['document'])
        if hashlib.sha256(registry.canonical(recipe).encode()).hexdigest() != row['digest']:
            raise ValueError('Source build profile integrity is invalid.')
        if mode == 'deploy':
            from workspace import read
            capability = read(db, project, binding)['capabilities']['deploy']
            if not capability['enabled'] or capability.get('kind') != 'managed-build':
                raise ValueError(capability['reason'] or 'No verified managed deployment target exists.')
            import managed_delivery
            recipe = dict(recipe, schema_kind='managed-build', managed_target=managed_delivery.capture(db, project, binding))
        return job_target.capture(project=project, environment=binding, repository=registered['repository'],
            repository_id=registered['repository_id'], branch=branch, sha=sha, mode=mode,
            recipe_revision=source_revision, recipe=recipe, project_revision=project_revision,
            environment_revision=environment_revision)
    if binding is not None:
        expected = request_identity({'environment': binding, 'environment_revision': environment_revision,
                                     'project_revision': project_revision, 'recipe_revision': recipe_revision})
        environment_record = registered['environments'][binding]
        if (registered['revision'] != expected['project_revision'] or environment_record['revision'] != expected['environment_revision']
                or environment_record['recipe_revision'] != expected['recipe_revision']):
            raise ValueError('Environment settings or recipe changed after review. Reload the environment before launching.')
        captured_environment_revision = environment_record['revision']
        captured_recipe_revision = environment_record['recipe_revision']
        row = db.execute('SELECT document,digest FROM registry_recipes WHERE id=? AND project=?',
                         (captured_recipe_revision, project)).fetchone()
        if row is None:
            raise ValueError('The environment recipe binding is unavailable. Reconcile its configuration before retrying.')
        recipe = json.loads(row['document'])
        if hashlib.sha256(registry.canonical(recipe).encode()).hexdigest() != row['digest']:
            raise ValueError('The stored environment recipe digest is inconsistent.')
        from workspace import read
        workspace = read(db, project, binding)
        capability = workspace['capabilities']['deploy' if mode == 'deploy' and execution_enabled else 'build']
        if not capability['enabled']:
            raise ValueError(capability['reason'])
    else:
        if registered['legacy_binding'] is not None:
            raise ValueError('This workload has an explicit environment. Open that environment and review its target before launching.')
        legacy = registry.legacy_projects(db).get(project)
        recipe = legacy.get('recipe') if legacy else None
        if not recipe:
            return None
        digest = hashlib.sha256(registry.canonical(recipe).encode()).hexdigest()
        row = db.execute('SELECT id FROM registry_recipes WHERE project=? AND digest=?',
                         (project, digest)).fetchone()
        captured_recipe_revision = row['id'] if row else None
    return job_target.capture(
        project=project, environment=binding or 'legacy-unclassified',
        repository=registered['repository'], repository_id=registered['repository_id'],
        branch=branch, sha=sha, mode=mode, recipe_revision=captured_recipe_revision, recipe=recipe,
        project_revision=registered['revision'], environment_revision=captured_environment_revision,
    )


def webhook_bindings(db, repository, repository_id, branch):
    matches = []
    legacy = registry.legacy_projects(db)
    for project, registered in registry.list_projects(db).items():
        if registered['repository'] != repository or registered['repository_id'] != repository_id:
            continue
        configured = []
        import build_profiles
        for name, env in registered['environments'].items():
            if env['branch'] != branch:
                continue
            source = build_profiles.get_private(db, project, name)
            if env['recipe_revision'] is None and source is None:
                continue
            configured.append(name)
            match = {'project': project, 'branch': branch, 'automatic': env['automatic'],
                     'environment': name, 'project_revision': registered['revision'],
                     'environment_revision': env['revision']}
            runtime_build = False
            if env['recipe_revision'] is not None:
                row = db.execute('SELECT document,digest FROM registry_recipes WHERE id=? AND project=?',
                                 (env['recipe_revision'], project)).fetchone()
                if row is None or hashlib.sha256(registry.canonical(json.loads(row['document'])).encode()).hexdigest() != row['digest']:
                    raise ValueError('Runtime recipe integrity is invalid.')
                runtime_build = build_configured(json.loads(row['document']))
            if runtime_build or source is None:
                match['recipe_revision'] = env['recipe_revision']
            else:
                from workspace import read
                capability = read(db, project, name)['capabilities']['deploy']
                mode = 'deploy' if capability['enabled'] and capability.get('kind') == 'managed-build' else 'validate'
                match.update(source_revision=source['source_revision'], mode=mode)
            matches.append(match)
        if not configured and registered['legacy_binding'] is None and registered['legacy_settings'] and legacy[project]['branch'] == branch:
            matches.append({'project': project, 'branch': branch, 'automatic': legacy[project]['automatic'], 'environment': None})
    return matches
