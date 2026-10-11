"""Fresh, independently observed public routing evidence; no provisioning effects."""
import hashlib
import json
import re
import time

FRESH_SECONDS = 60
RECHECK_SECONDS = 30


def observe(db, definition, operation, journal, *, gateway, host_client, verify, key_path, provider_transport, authorize):
    import cloudflare_dns
    import domain_execution
    route, operation_id = operation['route_id'], operation['id']
    authorize()
    owner = db.execute('SELECT record_id FROM ingress_dns_ownership WHERE operation_id=? AND route_id=?', (operation_id, route)).fetchone()
    intent = db.execute('SELECT state,record_id FROM ingress_dns_intents WHERE operation_id=? AND route_id=?', (operation_id, route)).fetchone()
    if not owner or not intent or intent['state'] != 'verified' or intent['record_id'] != owner['record_id']:
        raise ValueError('Existing DNS ownership is required.')
    adoption = journal.get('adoption') or {}
    dns = cloudflare_dns.apply(db, route, operation_id, key_path,
        preview_digest=journal['dns_preview']['preview_digest'], adopt_record_id=adoption.get('record_id'),
        transport=provider_transport, authorize=authorize)
    if dns.get('dns') != 'verified' or dns.get('record_id') != journal['dns_verified']['record_id']:
        raise ValueError('Current DNS ownership differs.')
    dns_observed = time.time()
    identity = {'route_id': route, 'operation_id': operation_id}
    cert, stage = journal['certificate'], journal['tls_stage']
    domain_execution._certificate_evidence(cert, route, operation_id, definition['hostname'])
    def origin():
        result = host_client.call('verify_tls', identity)
        domain_execution._tls_evidence(result, route, operation_id, cert, ('active',))
        if result['digest'] != stage['digest']: raise ValueError('Served TLS identity changed.')
    origin()
    before = gateway.readiness(db, definition, authorize=authorize)
    for key in ('proof_token', 'backend_id', 'gateway_id'):
        if not re.fullmatch('[a-f0-9]{64}', str(before.get(key, ''))): raise ValueError('Gateway proof identity is unavailable.')
    proof = verify(definition['hostname'], before['proof_token'])
    expected = hashlib.sha256(before['proof_token'].encode()).hexdigest()
    if (not isinstance(proof, dict) or proof.get('hostname') != definition['hostname'] or proof.get('status') != 'verified'
            or proof.get('proof_digest') != expected or type(proof.get('observed_at')) not in (int, float)
            or not 0 <= time.time() - proof['observed_at'] <= FRESH_SECONDS):
        raise ValueError('Public route proof is unavailable.')
    after = gateway.readiness(db, definition, authorize=authorize)
    if any(after.get(k) != before[k] for k in ('proof_token', 'backend_id', 'gateway_id')):
        raise ValueError('Application changed during public verification.')
    origin()
    authorize()
    observed = time.time()
    return {'hostname': definition['hostname'], 'proof_digest': expected,
        'gateway_id': before['gateway_id'], 'backend_id': before['backend_id'],
        'observed_at': proof['observed_at'], 'dns_observed_at': dns_observed,
        'origin_observed_at': observed, 'expires_at': min(dns_observed + FRESH_SECONDS, proof['observed_at'] + FRESH_SECONDS, cert['not_after']),
        'tls_fingerprint': cert['fingerprint'], 'tls_digest': stage['digest']}


def projection(db, operation, definition, journal, state, phase='external'):
    import math
    import sqlite3
    value = journal.get('external_verified')
    empty = {'status': 'unverified', 'evidence': 'none', 'live_checked': False, 'observed_at': None}
    if not value: return False, empty
    try:
        now = time.time()
        for key in ('observed_at', 'dns_observed_at', 'origin_observed_at', 'expires_at'):
            if type(value[key]) not in (int, float) or not math.isfinite(value[key]): raise ValueError()
        if value['hostname'] != definition['hostname']: raise ValueError()
        for key in ('proof_digest', 'gateway_id', 'backend_id', 'tls_fingerprint', 'tls_digest'):
            if not re.fullmatch('[a-f0-9]{64}', value[key]): raise ValueError()
        if value['tls_fingerprint'] != journal['certificate']['fingerprint'] or value['tls_digest'] != journal['tls_stage']['digest']: raise ValueError()
        fresh = (phase == 'external' and state in ('ready','queued','running')
            and journal.get('pending_effect') in (None,'external') and now < value['expires_at']
            and all(0 <= now - value[k] <= FRESH_SECONDS for k in ('observed_at', 'dns_observed_at', 'origin_observed_at'))
            and type(journal['certificate']['not_after']) in (int, float)
            and math.isfinite(journal['certificate']['not_after'])
            and now < value['expires_at'] <= min(journal['certificate']['not_after'],
                *(value[k] + FRESH_SECONDS for k in ('observed_at', 'dns_observed_at', 'origin_observed_at'))))
        if fresh:
            # A read never pings a host; stored evidence is bounded and authority
            # and known binding changes invalidate it immediately.
            import domain_execution
            principal = domain_execution.resolve_authority(db, operation)
            domain_execution._resume_authority(db, principal, definition['project'], definition['environment'], operation['id'], 'domains:create')
            import ingress_bindings
            import ingress_route_generations as generations
            key = (definition['project'], definition['environment'])
            intent = db.execute('SELECT * FROM ingress_runtime_intents WHERE project=? AND environment=?', key).fetchone()
            if not intent or intent['phase'] != 'verified' or generations.pending(db, *key): raise ValueError()
            gateway = json.loads(intent['definition'])
            persisted = json.loads(intent['journal'])
            if (generations.digest(gateway) != intent['digest'] or gateway.get('schema_version') != 3
                    or any(gateway.get(k) != definition[k] for k in ('project', 'environment'))
                    or persisted.get('binding', {}).get('container_id') != value['gateway_id']): raise ValueError()
            routes = [r for r in ingress_bindings.current_routes(db, gateway) if r['hostname'] == definition['hostname']]
            if len(routes) != 1: raise ValueError()
            route = routes[0]
            if (route['service_id'] != definition['service_id'] or route['http_endpoint'] != definition['http_endpoint']
                    or route['container_id'] != value['backend_id']
                    or hashlib.sha256(generations.route_proof_token(*key, route).encode()).hexdigest() != value['proof_digest']): raise ValueError()
            service = db.execute('SELECT project,environment,definition_id,state FROM managed_services WHERE id=?', (definition['service_id'],)).fetchone()
            if not service or service['state'] != 'running' or service['definition_id'] != route['definition_id'] or (service['project'], service['environment']) != key: raise ValueError()
            row = db.execute('SELECT document FROM service_bindings WHERE service_id=?', (definition['service_id'],)).fetchone()
            binding = json.loads(row['document']) if row else {}
            if (binding.get('container_id') != value['backend_id'] or binding.get('state') != 'running'
                    or binding.get('compose_service') != route['alias'] or binding.get('definition_digest') != route['definition_digest']): fresh = False
        return fresh, {'status': 'verified' if fresh else 'stale', 'evidence': 'recorded', 'live_checked': False,
            'observed_at': value['observed_at'], 'expires_at': value['expires_at']}
    except (ValueError, KeyError, TypeError, RuntimeError, sqlite3.DatabaseError):
        return False, dict(empty, status='unverified')


def schedule(db):
    """Queue one due read-only check; caller still uses ordinary execution leases."""
    import registry
    with registry.transaction(db):
        now = time.time()
        rows = db.execute("SELECT operation_id,journal FROM domain_executions WHERE state IN ('ready','awaiting-external') ORDER BY updated LIMIT 100").fetchall()
        for row in rows:
            journal = json.loads(row['journal'])
            due = journal.get('external_recheck_at', 0)
            if type(due) not in (int, float) or due > now: continue
            db.execute("UPDATE domain_executions SET phase='external',state='queued',updated=? WHERE operation_id=?", (now, row['operation_id']))
            db.execute("UPDATE ingress_operations SET status='queued' WHERE id=?", (row['operation_id'],))
            return True
    return False
