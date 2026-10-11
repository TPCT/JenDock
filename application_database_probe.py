"""Worker-only application engine authentication before attaching database networks."""
import os
import re
import time
import copy
from pathlib import Path

import application_credentials
import application_database_bindings as bindings
import database_target
from database_helper import InspectorHelper, exchange, MAX_RESPONSE


class ProbeError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('Application database identity could not be verified: ' + code + '.')


def verify(db, metadata, *, docker, socket, key_path, resolve_candidate, authorize,
           state_dir=None, runner=exchange, deadline=None):
    definition = metadata['definition']
    if definition['engine'] == 'postgresql':
        return None
    if definition['engine'] not in ('mysql', 'mariadb'):
        raise ProbeError('unsupported_engine')
    deadline = time.monotonic() + 45 if deadline is None else deadline

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise ProbeError('timeout')
        return value

    def current():
        remaining()
        authorize()
        resolved = bindings.resolve_runtime(db, metadata['project'], metadata['environment'],
                    metadata['service_id'], metadata['pin'], authorize=authorize)
        if resolved != metadata:
            raise ProbeError('identity_changed')
        candidate = database_target.resolve_ownership(metadata, resolve_candidate=resolve_candidate)
        database_target.check_registered(db, candidate)
        authorize()
        remaining()
        return candidate

    def bounded(argv, payload=b'', *, timeout, env, limit=MAX_RESPONSE):
        if not argv or argv[0] != docker or env.get('DOCKER_HOST') != socket:
            raise ProbeError('invalid_configuration')
        result = runner(argv, payload, timeout=min(timeout, remaining()), env=env, limit=min(limit, MAX_RESPONSE))
        if not isinstance(result, bytes) or len(result) > min(limit, MAX_RESPONSE):
            raise ProbeError('response_limit')
        return result

    try:
        expected = current()

        def revalidate(container_id):
            if container_id != expected['container_id'] or current() != expected:
                raise ProbeError('identity_changed')

        state = Path(state_dir) if state_dir is not None else Path(os.environ.get('STATE_DIR', '/worker-state')) / 'application-database-probe'
        state.mkdir(mode=0o700, exist_ok=True)
        helper = InspectorHelper(docker=docker, socket=socket,
                    image=os.environ.get('DATABASE_INSPECTOR_IMAGE'), state_dir=state, runner=bounded)
        helper.reconcile_pending()
        revalidate(expected['container_id'])
        secret = application_credentials.credential_for_runtime(db, metadata['project'], metadata['environment'],
                    definition['secret_id'], definition['secret_revision'], key_path, authorize=authorize)
        request = {'action': 'application-identity', 'engine': definition['engine'],
                   'database': definition['database_name'], 'port': definition['database_port'],
                   'user': secret['username'], 'password': secret['password']}
        result = helper.inspect(expected['container_id'], request, revalidate=revalidate)
        revalidate(expected['container_id'])
        proof = result.get('result')
        if (set(result) != {'result'} or not isinstance(proof, dict)
                or set(proof) != {'engine', 'database', 'server_version', 'cluster_identity_verified'}
                or proof['engine'] != definition['engine'] or proof['database'] != definition['database_name']
                or proof['cluster_identity_verified'] is not False or not isinstance(proof['server_version'], str)
                or not re.fullmatch(r'[A-Za-z0-9._+~ -]{1,128}', proof['server_version'])):
            raise ProbeError('identity_unverified')
        return {'identity': proof, 'candidate': copy.deepcopy(expected)}
    except ProbeError:
        raise
    except Exception:
        # Driver, authority and helper errors may contain sensitive context.
        raise ProbeError('identity_unavailable') from None


def bind_candidate(metadata, proof, resolve_candidate):
    """Carry authenticated identity into network transitions, without rebaselining.

    The first downstream observation must match the complete authenticated
    snapshot. Later observations may differ only in networks, whose authorized
    changes are independently checked by the network journal transition logic.
    """
    if proof is None:
        return resolve_candidate
    expected = copy.deepcopy(proof['candidate'])
    first = True

    def pinned(project, environment, reference, **options):
        nonlocal first
        raw = resolve_candidate(project, environment, reference, **options)
        # Resolve using the same normalization as the authenticated snapshot:
        # a Laravel mysql observation may describe the configured MariaDB.
        current = database_target.resolve_ownership(metadata, resolve_candidate=lambda *args, **kwargs: raw)
        omitted = {'fingerprint'} if first else {'fingerprint', 'networks'}
        if ({key:value for key,value in current.items() if key not in omitted} !=
                {key:value for key,value in expected.items() if key not in omitted}):
            raise ProbeError('identity_changed')
        first = False
        return raw
    return pinned
