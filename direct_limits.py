"""Direct container caps constrained by existing reservations, without adoption."""
import copy
import hashlib
import json
import time

import resource_policy
from registry import transaction


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS container_limit_requests(request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL)')


def limits(value):
    if not isinstance(value, dict) or set(value) != {'memory_mib', 'cpu'}:
        raise ValueError('Provide RAM in MiB and a CPU limit.')
    resource_policy.budget(value)
    return copy.deepcopy(value)


def owner(db, project, container, actual):
    """Resolve only trusted full Docker identity to one registered environment."""
    import re
    import registry
    from provenance import matches
    if not isinstance(actual, dict) or not re.fullmatch('[a-f0-9]{64}', str(actual.get('Id', ''))):
        return None
    if actual.get('Name', '').lstrip('/') != container:
        return None
    labels = actual.get('Config', {}).get('Labels') or {}
    files = labels.get('com.docker.compose.project.config_files', '')
    if not isinstance(files, str): return None
    registry.initialize(db)
    owners = []
    for row in db.execute('SELECT e.project,e.name,e.revision,e.recipe_revision,r.document,r.digest FROM registry_environments e '
                          'JOIN registry_recipes r ON r.id=e.recipe_revision AND r.project=e.project'):
        recipe = json.loads(row['document'])
        if hashlib.sha256(registry.canonical(recipe).encode()).hexdigest() != row['digest']:
            raise ValueError('Registered environment recipe identity is inconsistent.')
        if matches(recipe, labels.get('com.docker.compose.project'), labels.get('com.docker.compose.service'), files.split(',')):
            owners.append(dict(row))
    if len(owners) != 1 or owners[0]['project'] != project:
        return None
    return {key: owners[0][key] for key in ('name', 'revision', 'recipe_revision')}


def context(db, project, container, actual=None):
    policy = resource_policy.read(db, project)
    resource_policy.validate(policy['document'])
    document = policy['document']
    configured = bool(document['budget'] or any(row['budget'] or row['services'] for row in document['environments'].values()))
    reservations = [(name, row['services'][container]) for name, row in document['environments'].items() if container in row['services']]
    reserved = reservations[0][1] if reservations else None
    binding = owner(db, project, container, actual)
    ceiling = copy.deepcopy(reserved)
    reason = ''
    if configured and binding is not None:
        if reservations and reservations[0][0] != binding['name']:
            reason = 'The saved reservation belongs to a different environment. Reconcile that stale record before saving limits.'
        else:
            row = document['environments'][binding['name']]
            if not all(field in row['budget'] for field in ('memory_mib', 'cpu')):
                reason = 'Set this environment’s RAM and CPU budget before reserving container capacity.'
            else:
                ceiling = {field: row['budget'][field] - sum(value[field] for name, value in row['services'].items() if name != container)
                           for field in ('memory_mib', 'cpu')}
    elif configured and reserved is None:
        reason = 'Verified environment ownership is required to reserve capacity for this container. Adopt its workload before changing limits.'
    return {'revision': policy['revision'], 'configured': configured, 'limits_ceiling': ceiling,
            'reservation': copy.deepcopy(reserved), 'environment': binding['name'] if binding else None,
            'editable': not reason, 'reason': reason}


def check(db, project, container, value, actual=None):
    requested = limits(value)
    policy = context(db, project, container, actual)
    if not policy['editable']:
        raise ValueError(policy['reason'])
    ceiling = policy['limits_ceiling']
    if ceiling is not None:
        for field in ('memory_mib', 'cpu'):
            if requested[field] > ceiling[field] + 1e-8:
                raise ValueError('Requested ' + field + ' exceeds available budget capacity (' + str(ceiling[field]) + '). Adjust the environment budget or other reservations first.')
    return policy


def _transition(db, body, actual):
    policy = check(db, body['project'], body['container'], body['limits'], actual)
    if policy['configured'] and (type(body.get('policy_revision')) is not int or body['policy_revision'] != policy['revision']):
        raise ValueError('Resource settings changed. Reload current container limits before saving.')
    binding = owner(db, body['project'], body['container'], actual)
    if not policy['configured'] or binding is None:
        return None
    before = resource_policy.read(db, body['project'])['document']
    after = copy.deepcopy(before)
    after['environments'][binding['name']]['services'][body['container']] = limits(body['limits'])
    resource_policy.validate(after)
    if after == before: return None
    return {'project': body['project'], 'revision': policy['revision'], 'before': before, 'after': after, 'owner': binding}


def commit(db, transition):
    if transition is None: return
    with transaction(db):
        current = resource_policy.read(db, transition['project'])
        if current['revision'] == transition['revision'] + 1 and current['document'] == transition['after']: return
        if current['revision'] != transition['revision'] or current['document'] != transition['before']:
            raise ValueError('Resource settings changed before the reservation could be saved.')
        binding = transition['owner']
        row = db.execute('SELECT revision,recipe_revision FROM registry_environments WHERE project=? AND name=?',
                         (transition['project'], binding['name'])).fetchone()
        if row is None or row['revision'] != binding['revision'] or row['recipe_revision'] != binding['recipe_revision']:
            raise ValueError('Environment ownership changed before the reservation could be saved.')
        resource_policy.validate(transition['after'])
        db.execute('INSERT OR REPLACE INTO resource_policies VALUES(?,?,?,?)',
                   (transition['project'], transition['revision'] + 1, json.dumps(transition['after']), time.time()))


def rollback(db, transition):
    if transition is None: return
    with transaction(db):
        current = resource_policy.read(db, transition['project'])
        if current['document'] == transition['before']: return
        if current['revision'] != transition['revision'] + 1 or current['document'] != transition['after']:
            raise ValueError('Resource settings changed; reservation recovery requires reconciliation.')
        db.execute('UPDATE resource_policies SET revision=?,document=?,updated=? WHERE project=?',
                   (current['revision'] + 1, json.dumps(transition['before']), time.time(), transition['project']))


def fingerprint(body):
    fields = {key: body.get(key) for key in ('project', 'container', 'container_id', 'revision')}
    if not all(isinstance(value, str) and value for value in fields.values()):
        raise ValueError('Reload the current container details before saving limits.')
    if 'policy_revision' in body:fields['policy_revision'] = body['policy_revision']
    fields['actor'] = body.get('actor', 'admin')
    fields['limits'] = limits(body.get('limits'))
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def replay(db, body):
    initialize(db)
    digest = fingerprint(body)
    row = db.execute('''SELECT r.fingerprint,c.status,c.detail FROM container_limit_requests r
                       JOIN control_runs c ON c.request_id=r.request_id WHERE r.request_id=?''', (body['request_id'],)).fetchone()
    if row is None:
        return None
    if row['fingerprint'] != digest:
        raise ValueError('This request ID belongs to different container limits. Reload or submit a new request.')
    return {'status': row['status'], 'message': row['detail']}


def admit(db, body, actual=None):
    initialize(db)
    digest = fingerprint(body)
    with transaction(db):
        if db.execute('SELECT 1 FROM control_runs WHERE request_id=?', (body['request_id'],)).fetchone():
            raise ValueError('This request ID is already in use. Reload the operation before retrying.')
        transition = _transition(db, body, actual)
        now = time.time()
        db.execute('INSERT INTO control_runs(request_id,created,updated,actor,project,container,operation,status,detail) VALUES(?,?,?,?,?,?,?,?,?)',
                   (body['request_id'], now, now, body.get('actor', 'admin'), body['project'], body['container'],
                    'container-limits-save', 'running', 'Applying explicit RAM and CPU limits.'))
        db.execute('INSERT INTO container_limit_requests VALUES(?,?)', (body['request_id'], digest))

        return transition
