"""Shared console/MCP workload operations. Private peer auth belongs to caller.

Reads never start services. Durable retries retain original runtime pins and only
query the original receipt after dispatch intent exists, even following a timeout.
"""
import re
import time

from application_operations import OperationError
import managed_services
import workload_auth
import workload_operations as operations
import workload_protocol

TARGET = {'project', 'environment', 'service_id', 'container_id', 'definition_digest', 'adapter_revision'}
ACTIONS = frozenset({'workload-targets', 'workload-role-details', 'queue-failures', 'scheduler-history', 'queue-retry', 'workload-operation'})


def invalid():
    raise OperationError('invalid_input', 'The workload request is incomplete or invalid.', 400)


def _target(db, request):
    if (not isinstance(request.get('service_id'), str)
            or type(request.get('adapter_revision')) is not int or request['adapter_revision'] != 1
            or any(not isinstance(request.get(k), str) or not re.fullmatch('[a-f0-9]{64}', request[k]) for k in ('container_id', 'definition_digest'))):
        invalid()
    try:
        service = managed_services.get(db, request['service_id'])
        definition, binding = service['definition'], service['binding']
        workload = definition['artifact']['workload']
        if (service['project'] != request['project'] or service['environment'] != request['environment']
                or managed_services.digest(definition) != request['definition_digest']
                or not binding or binding['container_id'] != request['container_id']
                or binding['definition_digest'] != request['definition_digest']
                or workload['adapter_revision'] != request['adapter_revision']):
            raise ValueError()
        managed_services.require_role_capability(db, definition['artifact'])
        if 'connection_id' in request and request['connection_id'] != workload['configuration']['connection']:
            raise ValueError()
        if 'queue' in request and request['queue'] not in workload['configuration'].get('queues', []):
            raise ValueError()
        return workload
    except (KeyError, TypeError, ValueError):
        raise OperationError('workload_identity_changed', 'The accepted workload identity changed. Reload its details.', 409) from None


def recorded_workload(db, service):
    """Public immutable role metadata, never a claim of current runtime health."""
    definition = service['definition']
    workload = definition['artifact'].get('workload')
    if not workload:
        return None
    artifact_verified = True
    try:
        managed_services.require_role_capability(db, definition['artifact'])
    except ValueError:
        artifact_verified = False
    binding = service.get('binding') or {}
    digest = managed_services.digest(definition)
    target = None
    if (binding.get('definition_digest') == digest and isinstance(binding.get('container_id'), str)
            and re.fullmatch('[a-f0-9]{64}', binding['container_id'])):
        target = dict(project=service['project'], environment=service['environment'], service_id=service['id'],
                      container_id=binding['container_id'], definition_digest=digest, adapter_revision=workload['adapter_revision'])
    config_keys = ('connection', 'queues', 'timeout_seconds', 'retry_after_seconds', 'max_attempts',
                   'sleep_seconds', 'shutdown_grace_seconds') if workload['role'] == 'queue' else ('connection', 'timezone', 'scope')
    import copy
    return {'role': workload['role'], 'adapter': workload['adapter'], 'artifact_verified': artifact_verified,
            'runtime_observed': False, 'target': target,
            'configuration': {k: copy.deepcopy(workload['configuration'][k]) for k in config_keys if k in workload['configuration']}}


def targets(db, request, authority):
    if not {'project', 'environment'} <= set(request) <= {'project', 'environment', 'limit', 'cursor'}:
        invalid()
    limit = request.get('limit', 25)
    cursor = request.get('cursor')
    if type(limit) is not int or not 1 <= limit <= 100:
        invalid()
    if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch('[a-f0-9-]{36}', cursor)):
        invalid()
    authority.authorize(authority.actor, request, 'read')
    managed_services.initialize(db)
    rows = db.execute('SELECT id FROM managed_services WHERE project=? AND environment=? AND id>? ORDER BY id LIMIT ?',
                      (request['project'], request['environment'], cursor or '', limit + 1)).fetchall()
    result = []
    for row in rows[:limit]:
        service = managed_services.get(db, row['id'])
        definition = service['definition']
        workload = recorded_workload(db, service)
        if workload is None:
            continue
        result.append({'service_id': service['id'], 'name': definition['name'], 'role': workload['role'],
                       'recorded_state': service['state'], 'artifact_verified': workload['artifact_verified'],
                       'runtime_observed': False, 'target': workload['target']})
    authority.authorize(authority.actor, request, 'read')
    return {'items': result, 'next_cursor': rows[limit - 1]['id'] if len(rows) > limit else None}


def execute(db, body, *, transport, authority):
    """Authority is resolved from worker-authenticated proof, never body claims."""
    if not isinstance(body, dict) or body.get('action') not in ACTIONS:
        invalid()
    action = body['action']
    request = {k: v for k, v in body.items() if k != 'action'}
    if action == 'workload-targets':
        return targets(db, request, authority)
    if action == 'workload-operation':
        if set(request) != {'project', 'environment', 'service_id', 'request_id'}:
            invalid()
        # Original request lookup is independent of current container identity.
        operations.initialize(db)
        authority.authorize(authority.actor, request, 'read')
        row = db.execute('SELECT id,request FROM workload_retry_operations WHERE actor=? AND project=? AND environment=? AND request_id=?',
                         (authority.actor, request['project'], request['environment'], request['request_id'])).fetchone()
        if row is None:
            raise OperationError('operation_not_found', 'The original retry is not recorded. Do not repeat it automatically.', 404)
        import json
        accepted = json.loads(row['request'])
        if accepted['service_id'] != request['service_id']:
            raise OperationError('operation_not_found', 'The original retry is not recorded for this workload.', 404)
        result = operations.reconcile(db, authority.actor, row['id'], authorize=authority.authorize,
                                      transport=_retry_transport(transport, authority, accepted))
        return {'operation': result}
    extras = {'workload-role-details': set(), 'queue-failures': {'connection_id', 'queue'},
              'scheduler-history': {'registry_revision'}, 'queue-retry': operations.FIELDS - TARGET}[action]
    optional = {'limit', 'cursor'} if action in ('queue-failures', 'scheduler-history') else set()
    if not TARGET | extras <= set(request) <= TARGET | extras | optional:
        invalid()
    authority.authorize(authority.actor, request, 'accept' if action == 'queue-retry' else 'read')
    workload = _target(db, request)
    if action == 'queue-retry':
        if workload['role'] != 'queue': invalid()
        def authorize(actor, accepted, phase):
            principal = authority.authorize(actor, accepted, phase)
            if phase in ('accept', 'dispatch'):
                _target(db, accepted)
            return principal
        transport.check_target(*(request[k] for k in ('project', 'environment', 'service_id', 'container_id', 'definition_digest')), adapter_revision=request['adapter_revision'], authorize=lambda: authority.authorize(authority.actor, request, 'accept'))
        result = operations.accept(db, authority.actor, request, authorize=authorize)
        # Exact replay with an uncertain prior intent follows receipt-only recovery.
        result = operations.dispatch(db, authority.actor, result['operation_id'], authorize=authorize,
                                     transport=_retry_transport(transport, authority, request))
        return {'operation': result}
    operation = {'queue-failures': 'queue.failures', 'scheduler-history': 'scheduler.history'}.get(action, workload['role'] + '.observe')
    if not operation.startswith(workload['role'] + '.'):
        invalid()
    if action == 'scheduler-history' and request['registry_revision'] != request['definition_digest']:
        invalid()
    arguments = {k: request[k] for k in ('limit', 'cursor', 'queue') if k in request}
    response = transport.call(*(request[k] for k in ('project', 'environment', 'service_id', 'container_id', 'definition_digest')),
                              operation, arguments, adapter_revision=request['adapter_revision'], authorize=lambda: authority.authorize(authority.actor, request, 'read'))
    wire = dict(protocol=1, operation=operation, scope={k: request[k] for k in ('project', 'environment', 'service_id', 'definition_digest')}, **arguments)
    response = workload_protocol.validate_response(response, wire)
    principal = authority.authorize(authority.actor, request, 'read')
    return {'workload': {**{k: request[k] for k in TARGET}, 'role': workload['role'],
                         'received_at': time.time(), 'capabilities': {'read': True, 'retry': 'workloads:retry' in principal.scopes and workload['role'] == 'queue'},
                         'observation': response}}


def _retry_transport(transport, authority, accepted):
    def invoke(wire, guard):
        guard()
        return transport.call(*(accepted[k] for k in ('project', 'environment', 'service_id', 'container_id', 'definition_digest')),
                              wire['operation'], {k: v for k, v in wire.items() if k not in ('protocol', 'operation', 'scope')}, adapter_revision=accepted['adapter_revision'], authorize=guard)
    return invoke
