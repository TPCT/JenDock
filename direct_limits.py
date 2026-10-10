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


def context(db, project, container):
    policy = resource_policy.read(db, project)
    resource_policy.validate(policy['document'])
    document = policy['document']
    configured = bool(document['budget'] or any(row['budget'] or row['services'] for row in document['environments'].values()))
    ceiling = next((row['services'][container] for row in document['environments'].values() if container in row['services']), None)
    reason = ''
    if configured and ceiling is None:
        reason = 'This project has explicit aggregate budgets, but this service has no reserved capacity. Reconcile the project budget before changing this limit.'
    return {'revision': policy['revision'], 'configured': configured, 'limits_ceiling': copy.deepcopy(ceiling),
            'editable': not configured or ceiling is not None, 'reason': reason}


def check(db, project, container, value):
    requested = limits(value)
    policy = context(db, project, container)
    if not policy['editable']:
        raise ValueError(policy['reason'])
    ceiling = policy['limits_ceiling']
    if ceiling is not None:
        for field in ('memory_mib', 'cpu'):
            if requested[field] > ceiling[field] + 1e-8:
                raise ValueError('Requested ' + field + ' exceeds this service\'s reserved capacity (' + str(ceiling[field]) + '). Reconcile the existing project budget before increasing it.')
    return policy


def fingerprint(body):
    fields = {key: body.get(key) for key in ('project', 'container', 'container_id', 'revision')}
    if not all(isinstance(value, str) and value for value in fields.values()):
        raise ValueError('Reload the current container details before saving limits.')
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


def admit(db, body):
    initialize(db)
    digest = fingerprint(body)
    with transaction(db):
        if db.execute('SELECT 1 FROM control_runs WHERE request_id=?', (body['request_id'],)).fetchone():
            raise ValueError('This request ID is already in use. Reload the operation before retrying.')
        check(db, body['project'], body['container'], body['limits'])
        now = time.time()
        db.execute('INSERT INTO control_runs(request_id,created,updated,actor,project,container,operation,status,detail) VALUES(?,?,?,?,?,?,?,?,?)',
                   (body['request_id'], now, now, body.get('actor', 'admin'), body['project'], body['container'],
                    'container-limits-save', 'running', 'Applying explicit RAM and CPU limits.'))
        db.execute('INSERT INTO container_limit_requests VALUES(?,?)', (body['request_id'], digest))
