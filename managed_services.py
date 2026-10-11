"""Immutable verified artifacts, service definitions and durable capacity claims.

Only trusted build workers publish artifacts. Acceptance queues provisioning; it
never starts containers or claims that a service is running.
"""
import copy
import hashlib
import json
import re
import time
import uuid

import job_target
import registry
import resource_policy


def digest(value):
    return hashlib.sha256(registry.canonical(value).encode()).hexdigest()


def initialize(db):
    registry.initialize(db); job_target.initialize(db); resource_policy.initialize(db)
    for sql in (
        '''CREATE TABLE IF NOT EXISTS service_artifacts(id TEXT PRIMARY KEY,project TEXT NOT NULL,job_id INTEGER NOT NULL,document TEXT NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL,UNIQUE(project,digest))''',
        '''CREATE TABLE IF NOT EXISTS service_definitions(id TEXT PRIMARY KEY,document TEXT NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS managed_services(id TEXT PRIMARY KEY,project TEXT NOT NULL,environment TEXT NOT NULL,name TEXT NOT NULL,definition_id TEXT NOT NULL,operation_id TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(project,environment,name))''',
        '''CREATE TABLE IF NOT EXISTS service_reservations(service_id TEXT PRIMARY KEY,project TEXT NOT NULL,environment TEXT NOT NULL,memory_mib REAL NOT NULL,cpu REAL NOT NULL,state TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS service_bindings(service_id TEXT PRIMARY KEY,document TEXT NOT NULL,updated REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS service_operation_leases(operation_id TEXT PRIMARY KEY,worker TEXT NOT NULL,expires REAL NOT NULL)''',
    ):
        db.execute(sql)
    for table in ('service_artifacts', 'service_definitions'):
        for action in ('UPDATE', 'DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'Service evidence is immutable'); END")


def _profile(target, template_id):
    if not isinstance(template_id, str): raise ValueError('Service template identity is invalid.')
    profiles=target['recipe'].get('service_profiles', {})
    if not isinstance(profiles,dict): raise ValueError('Service profile catalog is invalid.')
    profile = profiles.get(template_id)
    if (not isinstance(template_id, str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}', template_id)
            or not isinstance(profile, dict) or set(profile) not in ({'revision', 'image_name', 'command', 'healthcheck', 'user'},
                {'revision', 'image_name', 'command', 'healthcheck', 'user', 'http_endpoint'})
            or type(profile['revision']) is not int or profile['revision'] < 1
            or 'http_endpoint' in profile and profile['revision'] != 2):
        raise ValueError('This build has no supported immutable service profile.')
    if 'http_endpoint' in profile:
        from build_profiles import validate_http_endpoint
        validate_http_endpoint(profile['http_endpoint'])
    if profile['image_name'] not in [image.get('name') for image in target['recipe'].get('images', [])]:
        raise ValueError('The service profile image is not part of this build recipe.')
    for field in ('command', 'healthcheck'):
        argv = profile[field]
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 40 or
                not all(isinstance(arg, str) and 0 < len(arg) <= 2048 and not any(ord(char) < 32 for char in arg) for arg in argv)):
            raise ValueError('Service command and health profiles require bounded fixed arguments.')
    if not isinstance(profile['user'], str) or not re.fullmatch('[1-9][0-9]{0,8}:[0-9]{1,9}', profile['user']):
        raise ValueError('Service profiles require an explicit non-root runtime user.')
    return copy.deepcopy(profile)


def _source(db, job_id):
    row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row is None:
        raise ValueError('A successful verified build is required for a service artifact.')
    target = job_target.load(db, job_id)
    if row['status'] not in ('validated', 'succeeded'):
        publication = None
        if target['recipe'].get('schema_kind') == 'managed-build' and db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_publications'").fetchone():
            publication = db.execute('SELECT recipe_digest FROM artifact_publications WHERE job_id=?', (job_id,)).fetchone()
        if publication is None or publication['recipe_digest'] != digest(target['recipe']):
            raise ValueError('A successful verified build is required for a service artifact.')
    if any(target[key] != row[key] for key in ('project', 'branch', 'sha', 'mode')):
        raise ValueError('The build source differs from its immutable target.')
    return target


def publish_artifact(db, job_id, template_id, image_id):
    initialize(db)
    if not isinstance(image_id, str) or not re.fullmatch(r'(?:[a-zA-Z0-9][a-zA-Z0-9._:/-]*@)?sha256:[a-f0-9]{64}', image_id):
        raise ValueError('An immutable built image identity is required.')
    with registry.transaction(db):
        target = _source(db, job_id); profile = _profile(target, template_id)
        document = {'project': target['project'], 'source_job': job_id, 'recipe_revision': target['recipe_revision'],
                    'source_sha': target['sha'], 'source_environment': target['environment'],
                    'target_fingerprint': target['fingerprint'], 'template_id': template_id,
                    'template_revision': profile['revision'], 'image_name': profile['image_name'], 'image': image_id,
                    'command': profile['command'], 'healthcheck': profile['healthcheck'], 'user': profile['user']}
        if 'http_endpoint' in profile: document['http_endpoint'] = copy.deepcopy(profile['http_endpoint'])
        fingerprint = digest(document)
        old = db.execute('SELECT id FROM service_artifacts WHERE project=? AND digest=?', (target['project'], fingerprint)).fetchone()
        if old: return artifact_for_service(db, target['project'], old['id'])
        identifier = str(uuid.uuid4())
        db.execute('INSERT INTO service_artifacts VALUES(?,?,?,?,?,?)', (identifier, target['project'], job_id, registry.canonical(document), fingerprint, time.time()))
        return dict(document, id=identifier, digest=fingerprint)


def artifact_for_service(db, project, artifact_id):
    initialize(db)
    row = db.execute('SELECT * FROM service_artifacts WHERE id=? AND project=?', (artifact_id, project)).fetchone()
    if row is None: raise ValueError('This project has no matching verified service artifact.')
    document = json.loads(row['document'])
    if digest(document) != row['digest']: raise ValueError('Service artifact evidence is inconsistent.')
    target = _source(db, row['job_id']); profile = _profile(target, document['template_id'])
    if (target['project'] != project or target['fingerprint'] != document['target_fingerprint']
            or any(document[key] != profile[key] for key in ('image_name', 'command', 'healthcheck', 'user'))
            or document['template_revision'] != profile['revision']
            or ('http_endpoint' in document) != ('http_endpoint' in profile)
            or document.get('http_endpoint') != profile.get('http_endpoint')):
        raise ValueError('Service artifact no longer matches its verified source.')
    return dict(document, id=row['id'], digest=row['digest'])


def protected_images(db):
    initialize(db)
    images = set()
    for row in db.execute('SELECT DISTINCT project FROM service_artifacts'):
        images.update(item['image'] for item in catalog(db, row['project']))
    for row in db.execute('SELECT d.document FROM service_definitions d JOIN managed_services s ON s.definition_id=d.id'):
        images.add(json.loads(row['document'])['artifact']['image'])
    return images


def catalog(db, project):
    initialize(db)
    result = []; selected = set()
    # Retry completion is not build chronology. Prefer the immutable source
    # job creation order, with its monotonic identity breaking timestamp ties.
    for row in db.execute('''SELECT artifact.id FROM service_artifacts AS artifact
            JOIN jobs AS source ON source.id=artifact.job_id
            WHERE artifact.project=?
            ORDER BY source.created DESC,source.id DESC,artifact.created DESC,artifact.id DESC''', (project,)):
        try: artifact = artifact_for_service(db, project, row['id'])
        except ValueError: continue
        identity = (artifact['template_id'], artifact['source_environment'])
        if identity in selected: continue
        selected.add(identity)
        result.append({'id': artifact['template_id'], 'revision': artifact['template_revision'], 'label': artifact['template_id'],
                       'artifact_id': artifact['id'], 'image': artifact['image'], 'image_name': artifact['image_name'],
                       'source_job': artifact['source_job'], 'source_sha': artifact['source_sha'],
                       'source_environment': artifact['source_environment'], 'command': artifact['command'],
                       'healthcheck': artifact['healthcheck'], 'desired_states': ['stopped', 'running'],
                       **({'http_endpoint': copy.deepcopy(artifact['http_endpoint'])} if 'http_endpoint' in artifact else {})})
    return result


class HistoryEvidenceError(ValueError):
    pass


def versions(db, project, environment, *, limit=25, cursor=None):
    """Bounded history of verified artifacts; associations are persisted evidence only."""
    initialize(db)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Choose a history page size between 1 and 100.')
    if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch(r'[a-f0-9-]{36}', cursor)):
        raise ValueError('The history cursor is invalid. Reload version history.')
    # CASE guards JSON access even for malformed documents. Scope must be known
    # before filtering; otherwise corrupted records would masquerade as absence.
    if db.execute('''SELECT 1 FROM service_artifacts WHERE project=? AND
            CASE WHEN json_valid(document) THEN COALESCE(
                json_type(document)='object' AND json_type(document,'$.source_environment')='text'
                AND json_extract(document,'$.source_environment') IN ('development','production','legacy-unclassified'), 0)
            ELSE 0 END = 0 LIMIT 1''', (project,)).fetchone():
        raise HistoryEvidenceError('Version history evidence is malformed. It cannot be presented as an empty history.')
    query = """SELECT artifact.id,artifact.created AS published_at,COALESCE(source.created,artifact.created) AS source_created,
        artifact.job_id AS source_job FROM service_artifacts artifact LEFT JOIN jobs source ON source.id=artifact.job_id
        WHERE artifact.project=? AND json_extract(artifact.document,'$.source_environment')=?"""
    parameters = [project, environment]
    if cursor is not None:
        anchor = db.execute(query + ' AND artifact.id=?', (*parameters, cursor)).fetchone()
        if anchor is None: raise ValueError('The history cursor does not belong to this environment.')
        query += ' AND (COALESCE(source.created,artifact.created),artifact.job_id,artifact.created,artifact.id) < (?,?,?,?)'
        parameters += [anchor['source_created'], anchor['source_job'], anchor['published_at'], anchor['id']]
    query += ' ORDER BY COALESCE(source.created,artifact.created) DESC,artifact.job_id DESC,artifact.created DESC,artifact.id DESC LIMIT ?'
    rows = db.execute(query, (*parameters, limit + 1)).fetchall()
    associations = {}
    for row in db.execute("""SELECT s.id,s.name,s.environment,s.state,s.updated,d.document,d.digest,
            b.document AS binding FROM managed_services s LEFT JOIN service_definitions d ON d.id=s.definition_id
            LEFT JOIN service_bindings b ON b.service_id=s.id WHERE s.project=? AND s.environment=?
            ORDER BY s.name,s.id""", (project, environment)):
        try:
            definition = json.loads(row['document'])
            binding = json.loads(row['binding']) if row['binding'] else {}
            if (not isinstance(definition, dict) or digest(definition) != row['digest']
                    or not isinstance(definition.get('artifact_id'), str) or not isinstance(binding, dict)):
                raise ValueError()
            associations.setdefault(definition['artifact_id'], []).append({
                'service_id': row['id'], 'name': row['name'], 'environment': row['environment'],
                'recorded_state': row['state'], 'recorded_at': row['updated'], 'container_id': binding.get('container_id')})
        except (ValueError, KeyError, TypeError):
            raise HistoryEvidenceError('Recorded service association evidence is inconsistent.') from None
    result = []
    for row in rows[:limit]:
        try: artifact = artifact_for_service(db, project, row['id'])
        except (ValueError, KeyError, TypeError):
            raise HistoryEvidenceError('A historical artifact cannot be verified against its recorded source. History is unavailable until the evidence is repaired.') from None
        result.append({key: artifact[key] for key in ('template_id', 'template_revision', 'source_job',
            'source_sha', 'source_environment', 'image_name', 'image')} | {
            'artifact_id': artifact['id'], 'source_created': row['source_created'],
            'published_at': row['published_at'], 'image_availability': 'not_checked',
            'recorded_services': associations.get(artifact['id'], [])})
    return {'project': project, 'environment': environment, 'versions': result,
            'next_cursor': rows[limit - 1]['id'] if len(rows) > limit else None, 'evidence': 'recorded'}


def reservation_totals(db, project, environment):
    initialize(db)
    row = db.execute("SELECT COALESCE(SUM(memory_mib),0),COALESCE(SUM(cpu),0) FROM service_reservations WHERE project=? AND environment=? AND state='held'", (project, environment)).fetchone()
    return {'memory_mib': row[0], 'cpu': row[1]}


def validate_capacity(db, project, document):
    resource_policy.validate(document)
    for environment, env in document['environments'].items():
        held = reservation_totals(db, project, environment)
        for field in ('memory_mib', 'cpu'):
            total = held[field] + sum(value[field] for value in env['services'].values())
            if total > env['budget'].get(field, 0) + 1e-8:
                raise ValueError(environment + ' budget cannot cover saved and accepted service reservations for ' + field + '.')


def validate_definition(db, project, environment, values):
    required = {'name', 'artifact_id', 'template_id', 'template_revision', 'environment_revision', 'policy_revision', 'resources'}
    if not isinstance(values, dict) or not required <= set(values) or set(values) - required - {'desired_state'}:
        raise ValueError('Provide only the supported service definition fields.')
    if not isinstance(values['artifact_id'], str) or not isinstance(values['template_id'], str):
        raise ValueError('Choose a published service artifact and template.')
    name = values['name']
    if not isinstance(name, str) or not re.fullmatch('[a-z][a-z0-9-]{0,47}', name):
        raise ValueError('Use a lowercase service name with letters, numbers and hyphens.')
    desired = values.get('desired_state', 'stopped')
    if desired not in ('stopped', 'running'): raise ValueError('Choose stopped or running.')
    for field in ('environment_revision', 'policy_revision', 'template_revision'):
        if type(values[field]) is not int or values[field] < (0 if field == 'policy_revision' else 1):
            raise ValueError('Review current environment, budget and template revisions.')
    resources = values['resources']
    if not isinstance(resources, dict) or set(resources) != {'memory_mib', 'cpu'}:
        raise ValueError('Service creation requires explicit RAM and CPU limits.')
    resource_policy.budget(resources)
    artifact = artifact_for_service(db, project, values['artifact_id'])
    if artifact['id'] not in {entry['artifact_id'] for entry in catalog(db, project)}:
        raise ValueError('A newer verified artifact is available. Reload the service catalog.')
    if (artifact['template_id'], artifact['template_revision']) != (values['template_id'], values['template_revision']):
        raise ValueError('The reviewed service template changed.')
    return {**copy.deepcopy(values), 'project': project, 'environment': environment, 'desired_state': desired, 'artifact': artifact}


def ensure_environment_eligible(db, project, environment):
    env=registry.get_environment(db,project,environment)
    if env['recipe_revision'] is not None:
        row=db.execute('SELECT document,digest FROM registry_recipes WHERE id=? AND project=?',(env['recipe_revision'],project)).fetchone()
        if row is None: raise ValueError('Environment ownership evidence is unavailable.')
        recipe=json.loads(row['document'])
        if digest(recipe)!=row['digest'] or recipe.get('managed_environment')!={'project':project,'environment':environment}:
            raise ValueError('This environment belongs to an existing non-managed workload.')
    return env


def accept(db, project, environment, definition, operation_id):
    """Caller owns BEGIN IMMEDIATE and authorization; returns queued record."""
    if not db.in_transaction: raise ValueError('Service acceptance requires an atomic application transaction.')
    env = ensure_environment_eligible(db, project, environment)
    if db.execute('SELECT count(*) FROM managed_services WHERE project=? AND environment=?', (project, environment)).fetchone()[0] >= 100:
        raise ValueError('This environment already has the maximum 100 managed services, including pending operations.')
    if env['revision'] != definition['environment_revision']:
        raise ValueError('Environment settings changed. Reload before creating a service.')
    policy = resource_policy.read(db, project)
    if policy['revision'] != definition['policy_revision']: raise ValueError('Resource budgets changed. Reload before creating a service.')
    validate_capacity(db, project, policy['document'])
    totals = reservation_totals(db, project, environment)
    env_policy = policy['document']['environments'][environment]
    for field in ('memory_mib', 'cpu'):
        total = totals[field] + sum(value[field] for value in env_policy['services'].values()) + definition['resources'][field]
        if total > env_policy['budget'].get(field, 0) + 1e-8: raise ValueError('Insufficient environment ' + field + ' budget for this service.')
    metric = db.execute('SELECT observed,value FROM telemetry ORDER BY observed DESC LIMIT 1').fetchone()
    if metric is None or not 0 <= time.time() - metric['observed'] <= 90:
        raise ValueError('Fresh host capacity observations are required before creating a service.')
    host = json.loads(metric['value'])
    for field in ('memory_total', 'memory_used', 'cpu_count'):
        resource_policy.number(host.get(field), 'Observed host capacity', 0, 2**63)
    allocated_cpu=0
    for saved in db.execute('SELECT document FROM resource_policies'):
        document=json.loads(saved['document']); resource_policy.validate(document)
        allocated_cpu+=sum(cap['cpu'] for env in document['environments'].values() for cap in env['services'].values())
    held = db.execute("SELECT COALESCE(SUM(memory_mib),0),COALESCE(SUM(cpu),0) FROM service_reservations WHERE state='held'").fetchone()
    if ((host.get('memory_total', 0) - host.get('memory_used', 0)) / 1048576 < held[0] + definition['resources']['memory_mib']
            or host.get('cpu_count', 0) < allocated_cpu + held[1] + definition['resources']['cpu']):
        raise ValueError('Observed host capacity cannot cover accepted service reservations.')
    if db.execute('SELECT 1 FROM managed_services WHERE project=? AND environment=? AND name=?', (project, environment, definition['name'])).fetchone():
        raise ValueError('This service name is already reserved in the selected environment.')
    identifier = str(uuid.uuid4()); definition = dict(definition, service_id=identifier)
    now = time.time(); document = registry.canonical(definition)
    db.execute('INSERT INTO service_definitions VALUES(?,?,?,?)', (identifier, document, digest(definition), now))
    db.execute('INSERT INTO managed_services VALUES(?,?,?,?,?,?,?,?,?)', (identifier, project, environment, definition['name'], identifier, operation_id, 'queued', now, now))
    db.execute('INSERT INTO service_reservations VALUES(?,?,?,?,?,?)', (identifier, project, environment, definition['resources']['memory_mib'], definition['resources']['cpu'], 'held'))
    return get(db, identifier)


def get(db, service_id):
    row = db.execute('SELECT * FROM managed_services WHERE id=?', (service_id,)).fetchone()
    if row is None: raise ValueError('Service was not found.')
    definition = db.execute('SELECT document,digest FROM service_definitions WHERE id=?', (row['definition_id'],)).fetchone()
    document = json.loads(definition['document'])
    if digest(document) != definition['digest']: raise ValueError('Service definition identity is inconsistent.')
    bound = db.execute('SELECT document FROM service_bindings WHERE service_id=?', (service_id,)).fetchone()
    binding = json.loads(bound['document']) if bound else None
    return {**dict(row), 'definition': document, 'desired_state': document['desired_state'], 'binding': binding,
            'container': {'id': binding['container_id'], 'name': binding['container_name'], 'evidence':'recorded', 'observed_at':binding.get('observed')} if binding else None}


def list_services(db, project, environment):
    initialize(db)
    return [get(db, row['id']) for row in db.execute('SELECT id FROM managed_services WHERE project=? AND environment=? ORDER BY created DESC LIMIT 100', (project, environment))]


def environment_definitions(db, project, environment):
    initialize(db)
    rows = [get(db, row['id']) for row in db.execute(
        'SELECT id FROM managed_services WHERE project=? AND environment=? ORDER BY created,id', (project, environment))]
    policy = resource_policy.read(db, project)['document']['environments'][environment]
    for row in rows:
        binding = row['binding']
        if binding:
            effective = policy['services'].get(binding['container_name'])
            if effective is None: raise ValueError('A bound service has no current budget allocation.')
            row['effective_resources'] = copy.deepcopy(effective)
        else:
            row['effective_resources'] = copy.deepcopy(row['definition']['resources'])
    return rows


def _operation(db, operation_id):
    row = db.execute("SELECT * FROM application_operations WHERE id=? AND operation LIKE 'service-create:%'", (operation_id,)).fetchone()
    if row is None: raise ValueError('Service operation was not found.')
    return row


def _update(db, operation_id, state, detail):
    row = _operation(db, operation_id); result = json.loads(row['result'])
    result['operation'].update(status=state, detail=detail)
    service_id = result['operation']['resource_id']
    db.execute('UPDATE managed_services SET state=?,updated=? WHERE id=?', (state, time.time(), service_id))
    result['service'] = get(db, service_id)
    db.execute('UPDATE application_operations SET result=? WHERE id=?', (registry.canonical(result), operation_id))
    db.execute('INSERT INTO application_operation_events(operation_id,actor,event,created) VALUES(?,?,?,?)',
               (operation_id, row['subject'], state, time.time()))


def claim_next(db, worker, lease_seconds=120):
    initialize(db)
    with registry.transaction(db):
        for row in db.execute('SELECT operation_id FROM service_operation_leases WHERE expires<?', (time.time(),)).fetchall():
            _update(db, row['operation_id'], 'recovery-required', 'Worker lease expired; reconcile recorded resources before retrying.')
            db.execute('DELETE FROM service_operation_leases WHERE operation_id=?', (row['operation_id'],))
        if db.execute('SELECT 1 FROM service_operation_leases LIMIT 1').fetchone(): return None
        row = db.execute("SELECT * FROM managed_services WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
        if row is None: return None
        operation_id = row['operation_id']
        db.execute('INSERT INTO service_operation_leases VALUES(?,?,?)', (operation_id, worker, time.time() + lease_seconds))
        _update(db, operation_id, 'provisioning', 'Creating the selected service; runtime verification is pending.')
        return {'operation_id': operation_id, 'service_id': row['id'], 'definition': get(db, row['id'])['definition']}


def authorize_claim(db, operation_id, worker):
    row = _operation(db, operation_id)
    lease = db.execute('SELECT * FROM service_operation_leases WHERE operation_id=?', (operation_id,)).fetchone()
    if lease is None or lease['worker'] != worker or lease['expires'] < time.time():
        raise ValueError('Service provisioning lease is unavailable or expired.')
    service_id = json.loads(row['result'])['operation']['resource_id']
    service = get(db, service_id)
    if service['state'] != 'provisioning': raise ValueError('Service operation is not provisioning.')
    definition = service['definition']
    if row['credential_id'] is not None:
        import mcp_credentials
        import application_operations as operations
        principal = mcp_credentials.principal_for_credential(db, row['credential_id'])
        if principal.subject_id != row['subject']:
            raise ValueError('Service operation owner no longer matches the credential.')
        operations._service_authorize(db, principal, 'services:create', definition['project'], definition['environment'])
        operations._service_authorize(db, principal, 'services:create', definition['project'], definition['artifact']['source_environment'])
        if definition['desired_state'] == 'running':
            operations._service_authorize(db, principal, 'services:start', definition['project'], definition['environment'])
    ensure_environment_eligible(db, definition['project'], definition['environment'])
    artifact = artifact_for_service(db, row['project'], definition['artifact_id'])
    if artifact != definition['artifact']: raise ValueError('Pinned service artifact changed.')
    return definition


def renew_claim(db, operation_id, worker, lease_seconds=120):
    authorize_claim(db, operation_id, worker)
    db.execute('UPDATE service_operation_leases SET expires=? WHERE operation_id=? AND worker=?', (time.time() + lease_seconds, operation_id, worker))


def finish(db, operation_id, worker, binding, state):
    with registry.transaction(db):
        existing_operation = _operation(db, operation_id)
        existing_id = json.loads(existing_operation['result'])['operation']['resource_id']
        existing_service = get(db, existing_id)
        if existing_service['state'] in ('stopped', 'running'):
            if existing_service['state'] == state and existing_service['binding'] == binding: return existing_service
            raise ValueError('This service operation already completed with different evidence.')
        definition = authorize_claim(db, operation_id, worker)
        if state not in ('stopped', 'running') or state != definition['desired_state']:
            raise ValueError('Verified runtime state differs from requested service state.')
        if (not isinstance(binding, dict) or not re.fullmatch('[a-f0-9]{64}', str(binding.get('container_id', '')))
                or binding.get('compose_project') != definition['project']
                or binding.get('compose_service') != definition['environment'] + '-' + definition['name']):
            raise ValueError('Verified service binding identity is invalid.')
        env=registry.get_environment(db,definition['project'],definition['environment'])
        if env['recipe_revision'] is not None:
            current=db.execute('SELECT document FROM registry_recipes WHERE id=? AND project=?',(env['recipe_revision'],definition['project'])).fetchone()
            if current is None or json.loads(current['document']).get('managed_environment') != {'project':definition['project'],'environment':definition['environment']}:
                raise ValueError('Environment ownership changed during provisioning.')
        from compose_identity import ordered_files
        files = list(ordered_files(binding.get('compose_files')))
        db.execute('INSERT INTO service_bindings VALUES(?,?,?)', (definition['service_id'], registry.canonical(binding), time.time()))
        bound = db.execute('SELECT b.document FROM service_bindings b JOIN managed_services s ON s.id=b.service_id WHERE s.project=? AND s.environment=?',
                           (definition['project'], definition['environment'])).fetchall()
        bindings = [json.loads(row['document']) for row in bound]
        if any(row['compose_files'] != files or row['compose_project'] != definition['project'] for row in bindings):
            raise ValueError('Managed environment invocation identity is inconsistent.')
        recipe = {'managed_environment': {'project': definition['project'], 'environment': definition['environment']},
                  'compose_project': definition['project'], 'compose_files': files,
                  'services': sorted(row['compose_service'] for row in bindings), 'owned_services': sorted(row['compose_service'] for row in bindings)}
        fingerprint = digest(recipe)
        db.execute('INSERT OR IGNORE INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,?)',
                   (definition['project'], fingerprint, registry.canonical(recipe), time.time()))
        recipe_id = db.execute('SELECT id FROM registry_recipes WHERE project=? AND digest=?', (definition['project'], fingerprint)).fetchone()['id']
        states = {item.get('state') for item in bindings}
        environment_state = 'running' if states == {'running'} else 'stopped' if states == {'stopped'} else 'degraded'
        db.execute('UPDATE registry_environments SET recipe_revision=?,revision=revision+1,state=? WHERE project=? AND name=?',
                   (recipe_id, environment_state, definition['project'], definition['environment']))
        policy = resource_policy.read(db, definition['project'])
        document = policy['document']
        name = binding.get('container_name')
        if not isinstance(name, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name):
            raise ValueError('Verified container name is unavailable.')
        if any(name in item['services'] for item in document['environments'].values()):
            raise ValueError('A reservation already uses this runtime container identity.')
        document['environments'][definition['environment']]['services'][name] = definition['resources']
        db.execute("UPDATE service_reservations SET state='allocated' WHERE service_id=?", (definition['service_id'],))
        validate_capacity(db, definition['project'], document)
        db.execute('INSERT OR REPLACE INTO resource_policies VALUES(?,?,?,?)',
                   (definition['project'], policy['revision'] + 1, registry.canonical(document), time.time()))
        db.execute('UPDATE registry_projects SET revision=revision+1,updated=? WHERE id=?', (time.time(), definition['project']))
        _update(db, operation_id, state, 'Service runtime ownership and requested state were verified.')
        db.execute('DELETE FROM service_operation_leases WHERE operation_id=?', (operation_id,))
        return get(db, definition['service_id'])


def fail(db, operation_id, worker, detail=None, recovery_required=True):
    with registry.transaction(db):
        lease = db.execute('SELECT worker FROM service_operation_leases WHERE operation_id=?', (operation_id,)).fetchone()
        if lease is None or lease['worker'] != worker: raise ValueError('Service operation lease is not owned by this worker.')
        # Fixed public text: never echo provider/Docker errors or runtime secrets.
        _update(db, operation_id, 'recovery-required' if recovery_required else 'failed',
                'Provisioning did not finish. Review recorded resource ownership before recovery.')
        db.execute('DELETE FROM service_operation_leases WHERE operation_id=?', (operation_id,))


def refresh_binding(db, service_id, expected_container_id, verified_binding):
    """CAS metadata after the executor verifies the actual replacement in full.

    This function never locates a container by name or authorizes a replacement;
    the worker must verify labels/image/network/manifest/effective caps first.
    """
    initialize(db)
    with registry.transaction(db):
        service = get(db, service_id); old = service['binding']
        if (old is None or not isinstance(verified_binding, dict)
                or not re.fullmatch('[a-f0-9]{64}', str(verified_binding.get('container_id', '')))
                or verified_binding.get('state') not in ('running', 'stopped')):
            raise ValueError('Verified replacement binding is invalid.')
        mutable = {'container_id', 'observed', 'state'}
        if ({key:value for key,value in old.items() if key not in mutable}
                != {key:value for key,value in verified_binding.items() if key not in mutable}):
            raise ValueError('Replacement changes the established service ownership.')
        if old == verified_binding: return copy.deepcopy(old)
        if old['container_id'] != expected_container_id:
            raise ValueError('Service binding changed during runtime reconciliation.')
        observed=verified_binding.get('observed')
        if type(observed) not in (int,float) or not 0 <= time.time()-observed <= 90:
            raise ValueError('Fresh runtime replacement observations are required.')
        db.execute('UPDATE service_bindings SET document=?,updated=? WHERE service_id=?',
                   (registry.canonical(verified_binding),time.time(),service_id))
        db.execute('UPDATE managed_services SET state=?,updated=? WHERE id=?',
                   (verified_binding['state'],time.time(),service_id))
        operation=_operation(db,service['operation_id']); result=json.loads(operation['result'])
        result['service']=get(db,service_id)
        db.execute('UPDATE application_operations SET result=? WHERE id=?',(registry.canonical(result),service['operation_id']))
        return copy.deepcopy(verified_binding)
