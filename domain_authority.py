"""Authority for observations of an already provisioned domain.

This does not authorize initial provisioning or prove runtime health. Callers must
first establish that the operation reached HTTP/TLS observation, then perform
fresh runtime checks under their existing execution/host lock. No writes occur
here; accepted domain configuration remains immutable.
"""
import json

import application_operations as ops
import ingress
import ingress_bindings as bindings
import managed_services as services


_DYNAMIC = frozenset({'service_definition_id', 'service_definition_digest',
                      'container_id', 'binding_digest', 'artifact_id', 'artifact_digest'})


def _reject():
    raise ops.OperationError('domain_authority_changed',
                             'Domain authority or committed backend evidence changed.', 409)


def validate_current(db, principal, accepted_definition):
    """Return current pins only when stable authority and committed routing agree.

    Source environment/revision remain stable. Both original and current source
    grants are checked. A deployment may replace artifact/container identities,
    but only its committed managed-service and ingress CAS evidence authorize
    using those identities for a later observation. This function starts no
    transaction and is safe inside the caller's authority transaction.
    """
    try:
        accepted = accepted_definition
        if not isinstance(accepted, dict): _reject()
        project, environment = accepted['project'], accepted['environment']
        ingress._authorize(db, principal, 'domains:create', project, environment)
        source = accepted['source_environment']
        if (principal.allowed_environments is not None and source is not None
                and (project, source) not in principal.allowed_environments):
            raise ops.OperationError('forbidden', 'Source environment is not permitted.', 403)
        row = db.execute('''SELECT d.document,d.digest,r.project,r.environment,r.hostname
                            FROM ingress_routes r JOIN ingress_definitions d ON d.id=r.definition_id
                            WHERE r.hostname=?''', (accepted['hostname'],)).fetchone()
        if (not row or json.loads(row['document']) != accepted
                or row['digest'] != ingress._digest(accepted)
                or any(row[k] != accepted[k] for k in ('project','environment','hostname'))): _reject()
        current = ingress._definition(db, principal, project, environment,
                                      {k: accepted[k] for k in ingress.FIELDS}, 'domains:create')
        if (set(current) != set(accepted)
                or any(current[k] != accepted[k] for k in accepted if k not in _DYNAMIC)): _reject()
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingress_runtime_intents'").fetchone(): _reject()
        intent = db.execute('SELECT definition,digest,phase FROM ingress_runtime_intents WHERE project=? AND environment=?',
                            (project, environment)).fetchone()
        if not intent or intent['phase'] != 'verified': _reject()
        gateway = json.loads(intent['definition'])
        if (bindings._digest(gateway) != intent['digest']
                or gateway['project'] != project or gateway['environment'] != environment): _reject()
        snapshot = bindings.capture(db, project, environment, accepted['service_id'])
        if not snapshot or snapshot['gateway_digest'] != intent['digest']: _reject()
        routes = [r for r in bindings.current_routes(db, gateway) if r['hostname'] == accepted['hostname']]
        if len(routes) != 1: _reject()
        backend = snapshot['backend']
        if routes[0] != dict(backend, hostname=accepted['hostname']): _reject()
        if (backend['service_id'] != current['service_id']
                or backend['definition_id'] != current['service_definition_id']
                or backend['definition_digest'] != current['service_definition_digest']
                or backend['container_id'] != current['container_id']
                or backend['http_endpoint'] != current['http_endpoint']): _reject()
        # Recheck the actual stored service rows, not merely a shape-valid route
        # snapshot: incomplete delivery transactions cannot establish authority.
        service = db.execute('SELECT project,environment,definition_id,state FROM managed_services WHERE id=?',
                             (current['service_id'],)).fetchone()
        definition = db.execute('SELECT document,digest FROM service_definitions WHERE id=?',
                                (current['service_definition_id'],)).fetchone()
        bound = db.execute('SELECT document FROM service_bindings WHERE service_id=?', (current['service_id'],)).fetchone()
        if not service or not definition or not bound: _reject()
        document = json.loads(definition['document']); binding = json.loads(bound['document'])
        if (service['project'] != project or service['environment'] != environment
                or service['definition_id'] != current['service_definition_id'] or service['state'] != 'running'
                or definition['digest'] != current['service_definition_digest']
                or services.digest(document) != definition['digest']
                or document['project'] != project or document['environment'] != environment
                or document['service_id'] != current['service_id']
                or document['artifact_id'] != current['artifact_id']
                or services.digest(document['artifact']) != current['artifact_digest']
                or backend['alias'] != environment + '-' + document['name']
                or binding['container_id'] != backend['container_id']
                or binding['definition_digest'] != backend['definition_digest']
                or binding['compose_service'] != backend['alias']
                or ingress._digest(binding) != current['binding_digest']): _reject()
        ingress._authorize(db, principal, 'domains:create', project, environment)
        return current
    except ops.OperationError:
        raise
    except (KeyError, TypeError, ValueError):
        _reject()
