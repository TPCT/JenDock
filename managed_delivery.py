"""Durable managed rollout targets and CAS metadata, without runtime side effects."""
import copy
import json
import re
import time
import uuid

import job_target
import managed_services as services
import registry
import resource_policy


def initialize(db):
    services.initialize(db)
    db.execute('''CREATE TABLE IF NOT EXISTS managed_delivery_targets(
        job_id INTEGER PRIMARY KEY,document TEXT NOT NULL,digest TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS managed_deliveries(
        job_id INTEGER PRIMARY KEY,document TEXT NOT NULL,state TEXT NOT NULL,
        worker TEXT,expires REAL,updated REAL NOT NULL)''')
    for action in ('UPDATE', 'DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS managed_delivery_target_{action.lower()} BEFORE {action} ON managed_delivery_targets BEGIN SELECT RAISE(ABORT,'Managed targets are immutable'); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS managed_delivery_target_replace BEFORE INSERT ON managed_delivery_targets WHEN EXISTS(SELECT 1 FROM managed_delivery_targets WHERE job_id=NEW.job_id) BEGIN SELECT RAISE(ABORT,'Managed targets are immutable'); END")


def capture(db, project, environment, service_ids=None):
    initialize(db)
    env = services.ensure_environment_eligible(db, project, environment)
    for pending in db.execute("SELECT document FROM managed_deliveries WHERE state IN ('queued','running','recovery-required')"):
        previous = json.loads(pending['document'])
        if previous['project'] == project and previous['environment'] == environment:
            raise ValueError('This environment has a pending or unresolved managed delivery. Reconcile it before launching another.')
    rows = services.environment_definitions(db, project, environment)
    if service_ids is not None:
        if (not isinstance(service_ids, list) or not service_ids or
                any(not isinstance(value, str) for value in service_ids) or len(set(service_ids)) != len(service_ids)):
            raise ValueError('Select unique managed service identifiers.')
        if set(service_ids) - {row['id'] for row in rows}:
            raise ValueError('Selected services do not belong to this environment.')
        rows = [row for row in rows if row['id'] in service_ids]
    if not rows: raise ValueError('Create and verify managed services before deploying.')
    selected = []
    for row in rows:
        if not row['binding'] or row['state'] not in ('running', 'stopped') or row['binding'].get('state') != row['state']:
            raise ValueError('Managed services need reconciled runtime bindings before delivery.')
        selected.append({'service_id': row['id'], 'name': row['name'], 'definition_id': row['definition_id'],
                         'definition_digest': services.digest(row['definition']), 'definition': row['definition'],
                         'binding': row['binding'], 'resources': row['effective_resources'],
                         'operation_id': row['operation_id'],
                         'state': 'pending' if row['state'] == 'running' else 'skipped-stopped'})
    return {'schema_version': 1, 'project': project, 'environment': environment,
            'environment_revision': env['revision'], 'policy_revision': resource_policy.read(db, project)['revision'],
            'environment_service_ids': sorted(row['id'] for row in services.environment_definitions(db, project, environment)),
            'services': selected}


def _validate_target(selection):
    fields = {'schema_version','project','environment','environment_revision','policy_revision','environment_service_ids','services'}
    if (not isinstance(selection, dict) or set(selection) != fields or selection['schema_version'] != 1
            or selection['environment'] not in ('development','production')
            or not isinstance(selection['services'], list) or not 1 <= len(selection['services']) <= 100
            or type(selection['environment_revision']) is not int or selection['environment_revision'] < 1
            or type(selection['policy_revision']) is not int or selection['policy_revision'] < 1):
        raise ValueError('Invalid managed delivery target.')
    identifiers = []
    for item in selection['services']:
        if (not isinstance(item, dict) or set(item) != {'service_id','name','definition_id','definition_digest','definition','binding','resources','operation_id','state'}
                or item['state'] not in ('pending','skipped-stopped')
                or services.digest(item['definition']) != item['definition_digest']
                or item['definition']['project'] != selection['project']
                or item['definition']['environment'] != selection['environment']
                or item['definition']['service_id'] != item['service_id']
                or item['binding'].get('state') != ('running' if item['state'] == 'pending' else 'stopped')):
            raise ValueError('Invalid managed service target.')
        resource_policy.budget(item['resources'])
        identifiers.append(item['service_id'])
    if (len(set(identifiers)) != len(identifiers) or not set(identifiers) <= set(selection['environment_service_ids'])):
        raise ValueError('Invalid managed service selection.')
    return copy.deepcopy(selection)


def validate_target(selection):
    try:
        result = _validate_target(selection)
        if (not isinstance(result['project'], str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}',result['project'])
                or not isinstance(result['environment_service_ids'], list)
                or len(set(result['environment_service_ids'])) != len(result['environment_service_ids'])
                or any(not isinstance(value,str) or not value for value in result['environment_service_ids'])):
            raise ValueError('Invalid managed environment identity.')
        for entry in result['services']:
            if (any(not isinstance(entry[key],str) or not entry[key] for key in ('service_id','name','definition_id','operation_id'))
                    or not isinstance(entry['binding'],dict) or entry['binding'].get('compose_project') != result['project']
                    or entry['binding'].get('compose_service') != result['environment']+'-'+entry['name']
                    or not re.fullmatch('[a-f0-9]{64}',str(entry['binding'].get('container_id','')))):
                raise ValueError('Invalid managed service identity.')
        return result
    except (KeyError,TypeError,AttributeError):
        raise ValueError('Invalid managed delivery target.') from None


def load_target(db, job_id):
    initialize(db)
    target = job_target.load(db, job_id)
    if target['mode'] != 'deploy' or target['recipe'].get('schema_kind') != 'managed-build':
        raise ValueError('This source build has no managed deployment selection.')
    selection = validate_target(target['recipe'].get('managed_target'))
    if any(target[key] != selection[key] for key in ('project','environment','environment_revision')):
        raise ValueError('Managed target differs from source identity.')
    return dict(selection, source_fingerprint=target['fingerprint'])


def persist_target(db, job_id, selection):
    if load_target(db, job_id) != dict(selection, source_fingerprint=job_target.load(db,job_id)['fingerprint']):
        raise ValueError('Managed target differs from source identity.')


def _load(db, job_id):
    row = db.execute('SELECT * FROM managed_deliveries WHERE job_id=?', (job_id,)).fetchone()
    if row is None: raise ValueError('Managed rollout has not been prepared.')
    return row, json.loads(row['document'])


def _save(db, job_id, document, state):
    db.execute('UPDATE managed_deliveries SET document=?,state=?,updated=? WHERE job_id=?',
               (registry.canonical(document), state, time.time(), job_id))


def _unchanged(db, entry):
    current = services.get(db, entry['service_id'])
    expected_id = entry['next_definition_id'] if entry['state'] == 'succeeded' else entry['definition_id']
    binding = entry.get('verified_binding') if entry['state'] in ('succeeded','rolled-back') else entry['binding']
    if current['definition_id'] != expected_id or current['binding'] != binding:
        raise ValueError('Managed service identity changed after selection.')
    if entry['state'] != 'succeeded' and services.digest(current['definition']) != entry['definition_digest']:
        raise ValueError('Managed service definition changed after selection.')
    policy = resource_policy.read(db, current['project'])['document']['environments'][current['environment']]
    if policy['services'].get(binding['container_name']) != entry['resources']:
        raise ValueError('Managed service resources changed after selection.')
    expected_state = 'stopped' if entry['state'] == 'skipped-stopped' else 'running'
    if current['state'] != expected_state: raise ValueError('Managed service state changed after selection.')


def prepare(db, job_id):
    initialize(db)
    with registry.transaction(db):
        old = db.execute('SELECT 1 FROM managed_deliveries WHERE job_id=?', (job_id,)).fetchone()
        if old: return _load(db, job_id)[1]
        selection = load_target(db, job_id)
        _environment_unchanged(db, selection)
        plan = dict(copy.deepcopy(selection), job_id=job_id)
        for entry in plan['services']:
            _unchanged(db, entry)
            if entry['state'] == 'skipped-stopped': continue
            found = []
            for row in db.execute('SELECT id FROM service_artifacts WHERE job_id=? AND project=?', (job_id, plan['project'])):
                artifact = services.artifact_for_service(db, plan['project'], row['id'])
                if artifact['template_id'] == entry['definition']['template_id']: found.append(artifact)
            if len(found) != 1 or found[0]['source_environment'] != plan['environment']:
                raise ValueError('This source job needs exactly one verified artifact for each selected service template.')
            artifact = found[0]
            definition = dict(copy.deepcopy(entry['definition']), artifact=artifact, artifact_id=artifact['id'],
                              template_revision=artifact['template_revision'], resources=copy.deepcopy(entry['resources']), desired_state='running')
            entry.update(artifact=artifact, next_definition=definition, next_definition_id=str(uuid.uuid4()))
        state = 'queued' if any(item['state'] == 'pending' for item in plan['services']) else 'succeeded'
        db.execute('INSERT INTO managed_deliveries VALUES(?,?,?,?,?,?)', (job_id, registry.canonical(plan), state, None, None, time.time()))
        return plan


def claim(db, job_id, worker, lease_seconds=120):
    initialize(db)
    if not isinstance(worker, str) or not worker or not 1 <= lease_seconds <= 3600: raise ValueError('Invalid rollout lease.')
    with registry.transaction(db):
        db.execute("UPDATE managed_deliveries SET state='recovery-required',worker=NULL,expires=NULL WHERE state='running' AND expires<=?", (time.time(),))
        row, plan = _load(db, job_id)
        if row['state'] != 'queued': return None
        if db.execute("SELECT 1 FROM managed_deliveries WHERE state='running'").fetchone(): return None
        _environment_unchanged(db, plan)
        for entry in plan['services']: _unchanged(db, entry)
        db.execute("UPDATE managed_deliveries SET state='running',worker=?,expires=?,updated=? WHERE job_id=? AND state='queued'", (worker, time.time()+lease_seconds, time.time(), job_id))
        return plan


def _environment_unchanged(db, plan):
    env = services.ensure_environment_eligible(db, plan['project'], plan['environment'])
    ids = sorted(row['id'] for row in db.execute('SELECT id FROM managed_services WHERE project=? AND environment=?', (plan['project'],plan['environment'])))
    if (env['revision'] != plan['environment_revision'] or ids != plan['environment_service_ids']
            or resource_policy.read(db, plan['project'])['revision'] != plan['policy_revision']):
        raise ValueError('Managed environment or resource allocation changed after selection.')


def authorize_claim(db, job_id, worker):
    row, plan = _load(db, job_id)
    if row['state'] != 'running' or row['worker'] != worker or row['expires'] <= time.time():
        raise ValueError('Managed rollout lease is unavailable or expired.')
    load_target(db, job_id)
    _environment_unchanged(db, plan)
    for entry in plan['services']: _unchanged(db, entry)
    return plan


def renew_claim(db, job_id, worker, lease_seconds=120):
    if not 1 <= lease_seconds <= 3600: raise ValueError('Invalid rollout lease.')
    with registry.transaction(db):
        authorize_claim(db, job_id, worker)
        db.execute('UPDATE managed_deliveries SET expires=? WHERE job_id=? AND worker=?', (time.time()+lease_seconds, job_id, worker))


def complete_service(db, job_id, worker, service_id, verified_binding):
    with registry.transaction(db):
        plan = authorize_claim(db, job_id, worker)
        entry = next((item for item in plan['services'] if item['service_id'] == service_id), None)
        if entry is None or entry['state'] != 'pending': raise ValueError('Service is not pending in this rollout.')
        old = entry['binding']; mutable = {'container_id', 'observed', 'state', 'image_id', 'definition_digest'}
        if (not isinstance(verified_binding, dict) or verified_binding.get('state') != 'running'
                or verified_binding.get('image_id') != entry['artifact']['image']
                or verified_binding.get('definition_digest') != services.digest(entry['next_definition'])
                or not re.fullmatch('[a-f0-9]{64}', str(verified_binding.get('container_id', '')))
                or {k:v for k,v in old.items() if k not in mutable} != {k:v for k,v in verified_binding.items() if k not in mutable}
                or type(verified_binding.get('observed')) not in (int,float)
                or not 0 <= time.time()-verified_binding['observed'] <= 90):
            raise ValueError('Verified managed replacement binding is invalid.')
        definition = entry['next_definition']; now = time.time()
        db.execute('INSERT INTO service_definitions VALUES(?,?,?,?)', (entry['next_definition_id'], registry.canonical(definition), services.digest(definition), now))
        changed = db.execute('UPDATE managed_services SET definition_id=?,state=?,updated=? WHERE id=? AND definition_id=?',
                             (entry['next_definition_id'], 'running', now, service_id, entry['definition_id'])).rowcount
        if changed != 1: raise ValueError('Managed service changed during completion.')
        db.execute('UPDATE service_bindings SET document=?,updated=? WHERE service_id=?', (registry.canonical(verified_binding), now, service_id))
        entry.update(state='succeeded', verified_binding=copy.deepcopy(verified_binding))
        done = all(item['state'] in ('succeeded','skipped-stopped') for item in plan['services'])
        _save(db, job_id, plan, 'succeeded' if done else 'running')
        if done: db.execute('UPDATE managed_deliveries SET worker=NULL,expires=NULL WHERE job_id=?', (job_id,))
        return public(db, job_id)


def fail(db, job_id, worker, detail=None, recovery_required=True):
    with registry.transaction(db):
        row, plan = _load(db, job_id)
        if row['worker'] != worker or row['state'] != 'running' or row['expires'] <= time.time():
            raise ValueError('Managed rollout lease is unavailable or expired.')
        _save(db, job_id, plan, 'recovery-required' if recovery_required else 'failed')
        db.execute('UPDATE managed_deliveries SET worker=NULL,expires=NULL WHERE job_id=?', (job_id,))
    return public(db, job_id)


def public(db, job_id):
    initialize(db)
    row, plan = _load(db, job_id)
    state = 'recovery-required' if row['state'] == 'running' and row['expires'] <= time.time() else row['state']
    return {'job_id': job_id, 'project': plan['project'], 'environment': plan['environment'], 'status': state,
            'services': [{'service_id': item['service_id'], 'name': item['name'], 'status': item['state']} for item in plan['services']],
            'detail': 'Managed delivery requires runtime reconciliation.' if state == 'recovery-required' else 'Managed delivery status recorded.'}


def protected_images(db):
    initialize(db)
    images = set()
    for row in db.execute("SELECT document FROM managed_deliveries WHERE state IN ('queued','running','recovery-required')"):
        for entry in json.loads(row['document'])['services']:
            images.add(entry['definition']['artifact']['image'])
            if entry.get('artifact'): images.add(entry['artifact']['image'])
    return images


def rollback_service(db, job_id, worker, service_id, verified_binding):
    """Record an executor-verified old-image restoration, never infer rollback."""
    with registry.transaction(db):
        plan = authorize_claim(db, job_id, worker)
        entry = next((item for item in plan['services'] if item['service_id'] == service_id), None)
        if entry is None or entry['state'] != 'pending': raise ValueError('Service is not pending in this rollout.')
        mutable = {'container_id','observed','state'}
        if (not isinstance(verified_binding,dict) or verified_binding.get('state') != 'running'
                or not re.fullmatch('[a-f0-9]{64}',str(verified_binding.get('container_id','')))
                or {k:v for k,v in entry['binding'].items() if k not in mutable} != {k:v for k,v in verified_binding.items() if k not in mutable}
                or type(verified_binding.get('observed')) not in (int,float)
                or not 0 <= time.time()-verified_binding['observed'] <= 90):
            raise ValueError('Verified restored binding is invalid.')
        db.execute('UPDATE service_bindings SET document=?,updated=? WHERE service_id=?', (registry.canonical(verified_binding),time.time(),service_id))
        entry.update(state='rolled-back',verified_binding=copy.deepcopy(verified_binding))
        _save(db,job_id,plan,'running')
    return public(db,job_id)


def abandon_unstarted(db, job_id):
    """Release only a queued rollout with no worker and no runtime journal.

    A rejected pre-claim CAS has made no runtime changes. Running, expired,
    recovery and partially completed execution are never abandoned here.
    """
    initialize(db)
    with registry.transaction(db):
        row = db.execute('SELECT * FROM managed_deliveries WHERE job_id=?', (job_id,)).fetchone()
        if row is None or row['state'] != 'queued' or row['worker'] is not None or row['expires'] is not None:
            return False
        plan = json.loads(row['document'])
        if any(entry['state'] not in ('pending','skipped-stopped') for entry in plan['services']):
            return False
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_rollout_journals'").fetchone():
            for entry in plan['services']:
                operation_id = str(job_id) + '-' + entry['service_id']
                if db.execute('SELECT 1 FROM managed_rollout_journals WHERE operation_id=?', (operation_id,)).fetchone():
                    return False
        changed = db.execute("UPDATE managed_deliveries SET state='failed',updated=? WHERE job_id=? AND state='queued' AND worker IS NULL AND expires IS NULL",
                             (time.time(),job_id)).rowcount
        return changed == 1
