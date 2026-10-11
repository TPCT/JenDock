"""Provision approved application artifacts into newly managed environments.

Call only from the exclusive worker executor. User requests cannot supply Docker
configuration, commands, images, mounts, networks or credentials. The immutable
artifact catalog and accepted service definitions are the execution authority.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

import yaml

import registry

PREFIX = 'net.shiftcodes.managed.'


class ProvisioningError(ValueError):
    pass


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS service_provisioning_journals(
        operation_id TEXT PRIMARY KEY, definition_digest TEXT NOT NULL,
        document TEXT NOT NULL, updated REAL NOT NULL)''')


def command(args, timeout=60, *, environment=None):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout,
                                env={**os.environ, **(environment or {}), 'COMPOSE_IGNORE_ORPHANS': 'true'})
    except (OSError, subprocess.TimeoutExpired):
        raise ProvisioningError('Container operation outcome is uncertain. Reconcile its journal before retrying.') from None
    if result.returncode:
        raise ProvisioningError('Container operation failed. Its journal and reserved capacity are retained for recovery.')
    if len(result.stdout) > 4 * 1024 * 1024:
        raise ProvisioningError('Container inspection exceeded the supported size.')
    return result.stdout.decode()


def _digest(value):
    return hashlib.sha256(registry.canonical(value).encode()).hexdigest()


def _labels(definition, operation_id):
    return {PREFIX + 'project': definition['project'], PREFIX + 'environment': definition['environment'],
            PREFIX + 'service': definition['service_id'], PREFIX + 'operation': operation_id,
            PREFIX + 'definition': _digest(definition)}


def _escaped(value):
    return value.replace('$', '$$')


def service_config(definition, operation_id, resources=None, *, runtime_context=None, database_network=None):
    artifact = definition['artifact']
    argv = artifact['command']
    resources = resources or definition['resources']
    result = {'image': artifact['image'], 'pull_policy': 'never',
            'entrypoint': [_escaped(argv[0])], 'command': [_escaped(value) for value in argv[1:]],
            'user': artifact['user'], 'read_only': True, 'cap_drop': ['ALL'],
            'security_opt': ['no-new-privileges:true'], 'pids_limit': 128,
            'mem_limit': int(resources['memory_mib'] * 1024 * 1024),
            'memswap_limit': int(resources['memory_mib'] * 1024 * 1024),
            'deploy': {'resources': {'limits': {'memory': str(int(resources['memory_mib'] * 1024 * 1024)), 'cpus': str(resources['cpu']), 'pids': 128}}},
            'cpus': resources['cpu'], 'restart': 'no', 'stop_grace_period': '5s',
            'tmpfs': ['/tmp:rw,noexec,nosuid,size=16m'], 'networks': ['environment'],
            'labels': _labels(definition, operation_id),
            'healthcheck': {'test': ['CMD', *[_escaped(value) for value in artifact['healthcheck']]],
                            'interval': '2s', 'timeout': '3s', 'retries': 10, 'start_period': '2s'}}
    if definition.get('runtime'):
        if runtime_context is None:raise ProvisioningError('Accepted runtime values require a private worker context.')
        result['environment']=runtime_context.environment(definition)[0]
    if (definition.get('runtime') or {}).get('database_attachment'):
        if not database_network:raise ProvisioningError('A verified database bridge is required.')
        result['networks']={'environment':{'priority':1000},database_network['name']:{'priority':0}}
    elif database_network is not None:raise ProvisioningError('Unexpected database bridge.')
    return result


def _names(definition):
    project, env = definition['project'], definition['environment']
    return env + '-' + definition['name'], project + '-' + env + '-managed'


def _path(root, definition):
    root = Path(root).resolve(strict=True)
    current = root
    for part in (definition['project'], definition['environment']):
        current = current / part
        if current.is_symlink():
            raise ProvisioningError('Managed configuration path is not a private directory.')
        current.mkdir(mode=0o700, exist_ok=True)
        if not current.is_dir():
            raise ProvisioningError('Managed configuration path is not a directory.')
    path = current / 'compose.json'
    if path.is_symlink():
        raise ProvisioningError('Managed configuration path cannot be a symbolic link.')
    return path


def _atomic(path, document):
    data = registry.canonical(document)
    temporary = path.with_suffix('.pending-' + str(os.getpid()))
    try:
        with temporary.open('x', encoding='utf-8') as output:
            os.chmod(temporary, 0o600)
            output.write(data); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _persist(db, operation_id, digest, journal, phase, **values):
    journal.update(values, phase=phase)
    db.execute('INSERT INTO service_provisioning_journals VALUES(?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET document=excluded.document,updated=excluded.updated',
               (operation_id, digest, registry.canonical(journal), time.time()))


def _json(argv):
    try: return json.loads(command(argv))
    except (TypeError, ValueError):
        raise ProvisioningError('Container inspection returned invalid data.') from None


def _lookup(docker, kind, name):
    args = [docker, 'network', 'ls', '-q', '--no-trunc', '--filter', 'name=^' + name + '$'] if kind == 'network' else [docker, 'ps', '-aq', '--no-trunc', '--filter', 'name=^/' + name + '$']
    ids = command(args).split()
    if len(ids) > 1:
        raise ProvisioningError('Resource name is ambiguous. No resources were replaced.')
    if not ids: return None
    return _json([docker, *(['network'] if kind == 'network' else []), 'inspect', ids[0]])[0]


def _network(row, definition, expected_id=None):
    _, name = _names(definition)
    expected = {PREFIX + 'project': definition['project'], PREFIX + 'environment': definition['environment']}
    if (row.get('Name') != name or expected_id and row.get('Id') != expected_id
            or row.get('Driver') != 'bridge' or row.get('Internal') is not True
            or row.get('EnableIPv6') or row.get('Options')
            or any((row.get('Labels') or {}).get(key) != value for key, value in expected.items())):
        raise ProvisioningError('Environment network ownership or isolation differs. No network was reused.')


def _verify(row, definition, operation_id, image_id, network_id, name, path, resources=None, *, runtime_context=None, docker=None, database_network=None):
    if definition.get('runtime'):
        if runtime_context is None or docker is None:raise ProvisioningError('Runtime verification requires current private authority.')
        image = _json([docker, 'image', 'inspect', image_id])[0]
        if image.get('Id') != image_id:raise ProvisioningError('Runtime image identity changed.')
        runtime_context.verify(definition,row,image.get('Config',{}).get('Env'))
    config, host = row.get('Config', {}), row.get('HostConfig', {})
    labels = config.get('Labels') or {}
    expected = _labels(definition, operation_id)
    expected.update({'com.docker.compose.project': definition['project'],
                     'com.docker.compose.service': _names(definition)[0],
                     'com.docker.compose.project.config_files': str(path)})
    artifact = definition['artifact']; argv = artifact['command']; limits = resources or definition['resources']
    networks = row.get('NetworkSettings', {}).get('Networks', {})
    expected_networks={_names(definition)[1]:network_id}
    attachment=(definition.get('runtime') or {}).get('database_attachment')
    if attachment:
        if not database_network or database_network['binding_id']!=attachment['binding_id']:raise ProvisioningError('Accepted database network is unavailable.')
        expected_networks[database_network['name']]=database_network['id']
    elif database_network is not None:raise ProvisioningError('Unexpected database network identity.')
    created=row.get('State',{}).get('Status')=='created' and not row.get('State',{}).get('Running')
    if (row.get('Name', '').lstrip('/') != name or row.get('Image') != image_id
            or any(labels.get(key) != value for key, value in expected.items())
            or config.get('Entrypoint') != [argv[0]] or (config.get('Cmd') or []) != argv[1:]
            or config.get('User') != artifact['user'] or config.get('Healthcheck', {}).get('Test') != ['CMD', *artifact['healthcheck']]
            or host.get('Privileged') or host.get('ReadonlyRootfs') is not True
            or host.get('CapAdd') or host.get('CapDrop') != ['ALL']
            or host.get('SecurityOpt') != ['no-new-privileges:true']
            or host.get('Binds') or host.get('PortBindings') or row.get('Mounts')
            or host.get('Devices') or host.get('DeviceRequests') or host.get('PidMode') or host.get('IpcMode') not in ('private', '')
            or host.get('Memory') != int(limits['memory_mib'] * 1024 * 1024)
            or host.get('MemorySwap') != int(limits['memory_mib'] * 1024 * 1024)
            or host.get('NanoCpus') != round(limits['cpu'] * 1_000_000_000) or host.get('PidsLimit') != 128
            or set(networks) != set(expected_networks)
            or host.get('NetworkMode') != _names(definition)[1]
            or any(networks[name].get('NetworkID') not in ({identity,''} if created else {identity}) for name,identity in expected_networks.items())):
        raise ProvisioningError('Created container identity, configuration or isolation does not match the accepted definition.')


def provision(db, operation_id, definition, *, root, docker, compose, authorize, health_timeout=35):
    """Return a private verified binding; caller commits managed-service completion.

    authorize() must recheck the lease and principal grants, raising on failure.
    The caller holds the worker-wide control lock throughout execution. A resumed
    uncertain create/start is reconciled from exact identities, never repeated.
    """
    import managed_services
    import service_runtime
    runtime_context=service_runtime.Context(db,authorize)
    descriptors={}
    def config(d,o,r=None):return service_config(d,o,r,runtime_context=runtime_context,database_network=descriptors.get(_digest(d)))
    def verify(*args):return _verify(*args,runtime_context=runtime_context,docker=docker,database_network=descriptors.get(_digest(args[1])))
    if not callable(authorize) or db.in_transaction:
        raise ProvisioningError('Provisioning requires exclusive worker authorization outside a transaction.')
    authorize()
    registry.identifier(definition['project']); registry.identifier(definition['name'])
    if definition['environment'] not in registry.ENVIRONMENTS or definition['desired_state'] not in ('stopped', 'running'):
        raise ProvisioningError('Explicit environment and desired state are required.')
    artifact = managed_services.artifact_for_service(db, definition['project'], definition['artifact_id'])
    if artifact != definition['artifact']:
        raise ProvisioningError('Accepted application artifact changed. No service was provisioned.')
    digest = _digest(definition)
    initialize(db)
    old = db.execute('SELECT * FROM service_provisioning_journals WHERE operation_id=?', (operation_id,)).fetchone()
    if old and old['definition_digest'] != digest:
        raise ProvisioningError('Provisioning operation belongs to another immutable definition.')
    journal = json.loads(old['document']) if old else {}
    # Actual image must already be present from the verified build. No mutable tag
    # resolution, inherited image volumes or unreviewed ENTRYPOINT is permitted.
    image = _json([docker, 'image', 'inspect', artifact['image']])[0]
    if image.get('Config', {}).get('Volumes'):
        raise ProvisioningError('This image declares persistence not supported by the application template.')
    image_id = image['Id']
    if artifact['image'].startswith('sha256:') and artifact['image'] != image_id:
        raise ProvisioningError('Built image digest differs from the approved artifact.')
    if journal.get('image_id', image_id) != image_id:
        raise ProvisioningError('Pinned application image identity changed.')
    path = _path(root, definition)
    service, network_name = _names(definition)
    name = definition['project'] + '-' + service + '-1'
    accepted = managed_services.environment_definitions(db, definition['project'], definition['environment'])
    for entry in accepted:
        d=entry['definition'];descriptors[_digest(d)]=runtime_context.attachment(d,docker,ensure=not bool(entry.get('binding')))
    services = {}
    refreshed_bindings = []
    for item in accepted:
        item_definition, item_operation = item['definition'], item['operation_id']
        services[_names(item_definition)[0]] = config(item_definition, item_operation, item.get('effective_resources'))
        if item.get('binding'):
            binding = item['binding']
            peer = _lookup(docker, 'container', binding['container_name'])
            if peer is None:
                raise ProvisioningError('A previously managed container is missing. Reconcile it before adding another service.')
            verify(peer, item_definition, item_operation, binding['image_id'], binding['network_ids'][0],
                    binding['container_name'], path, item.get('effective_resources'))
            # Runtime state changes (for example a normal stop) do not require
            # container replacement. Refresh every fully verified observation;
            # completion must aggregate current states, not creation outcomes.
            refreshed = dict(binding, container_id=peer['Id'], observed=time.time(),
                             state='running' if peer['State']['Running'] else 'stopped')
            refreshed_bindings.append((item_definition['service_id'], binding['container_id'], refreshed))
    expected = {'name': definition['project'], 'services': services,
                'networks': {'environment': {'external': True, 'name': network_name}}}
    for descriptor in descriptors.values():
        if descriptor:expected['networks'][descriptor['name']]={'external':True,'name':descriptor['name']}
    if service not in services or services[service] != config(definition, operation_id):
        raise ProvisioningError('Accepted service definition is missing from the managed environment.')
    if path.exists():
        previous = yaml.safe_load(path.read_text())
        if (set(previous) != set(expected) or previous['name'] != expected['name'] or previous['networks'] != expected['networks']
                or not isinstance(previous['services'], dict)
                or any(key not in services or value != services[key] for key, value in previous['services'].items())):
            raise ProvisioningError('Managed environment configuration drifted. Existing services were not changed.')
    for service_id, previous_id, refreshed in refreshed_bindings:
        authorize()
        managed_services.refresh_binding(db, service_id, previous_id, refreshed)
    _persist(db, operation_id, digest, journal, journal.get('phase', 'accepted'), image_id=image_id, path=str(path))
    authorize()
    _atomic(path, expected)
    network = _lookup(docker, 'network', network_name)
    if network is not None and not (old and journal.get('phase') in ('network-intent', 'create-intent', 'created', 'start-intent', 'verified')):
        known_ids = []
        for item in accepted:
            evidence = db.execute('SELECT document FROM service_provisioning_journals WHERE operation_id=?', (item['operation_id'],)).fetchone()
            if evidence: known_ids.append(json.loads(evidence['document']).get('network_id'))
        if network.get('Id') not in known_ids:
            raise ProvisioningError('Existing environment network has no durable ownership evidence. No resource was adopted.')
    if network is None:
        if old and journal['phase'] not in ('accepted',):
            raise ProvisioningError('Environment network creation outcome is uncertain. Recovery is required.')
        _persist(db, operation_id, digest, journal, 'network-intent')
        authorize()
        command([docker, 'network', 'create', '--driver', 'bridge', '--internal',
                 '--label', PREFIX + 'project=' + definition['project'],
                 '--label', PREFIX + 'environment=' + definition['environment'], network_name])
        network = _lookup(docker, 'network', network_name)
        if network is None: raise ProvisioningError('Created environment network could not be verified.')
    _network(network, definition, journal.get('network_id'))
    known = {binding['container_id'] for _, _, binding in refreshed_bindings}
    if journal.get('container_id'):
        known.add(journal['container_id'])
    elif old and journal.get('phase') == 'create-intent':
        # A lost create response may precede recording its ID. Admit only the
        # exact candidate after the existing full application verification.
        candidate = _lookup(docker, 'container', name)
        if candidate is not None:
            verify(candidate, definition, operation_id, image_id, network['Id'], name, path)
            known.add(candidate['Id'])

    def verify_members(observed):
        for entry in accepted:
            d=entry['definition']
            if runtime_context.attachment(d,docker)!=descriptors[_digest(d)]:raise ProvisioningError('Database network authority changed.')
        from ingress_runtime import verified_network_members
        gateways = verified_network_members(db, definition['project'], definition['environment'],
            docker=docker, application_network_id=network['Id'], authorize=authorize)
        if set(observed.get('Containers') or {}) - (known | set(gateways)):
            raise ProvisioningError('An unowned container is attached to the environment network.')

    verify_members(network)
    phase = journal['phase']
    _persist(db, operation_id, digest, journal, phase, network_id=network['Id'])
    actual = _lookup(docker, 'container', name)
    if actual is None:
        if old and phase in ('create-intent', 'created', 'start-intent', 'verified'):
            raise ProvisioningError('Container creation outcome is uncertain or the owned container disappeared. Recovery is required.')
        _persist(db, operation_id, digest, journal, 'create-intent')
        authorize()
        command([compose, '-p', definition['project'], '-f', str(path), 'create', '--no-build', '--pull', 'never', '--no-recreate', service], **({'environment':runtime_context.substitutions([entry['definition'] for entry in accepted])} if any(entry['definition'].get('runtime') for entry in accepted) else {}))
        actual = _lookup(docker, 'container', name)
        if actual is None: raise ProvisioningError('Created container could not be verified.')
    if actual is not None and not old and journal['phase'] != 'create-intent':
        raise ProvisioningError('Existing container has no creation journal. No resource was adopted.')
    if journal.get('container_id', actual['Id']) != actual['Id']:
        raise ProvisioningError('Owned container was replaced. Recovery is required.')
    verify(actual, definition, operation_id, image_id, network['Id'], name, path)
    known.add(actual['Id'])
    resumed_start = phase in ('start-intent', 'verified')
    _persist(db, operation_id, digest, journal, phase if resumed_start else 'created', container_id=actual['Id'])
    if definition['desired_state'] == 'running':
        if not actual['State']['Running']:
            if old and resumed_start:
                raise ProvisioningError('Previously started container is stopped. Recovery is required before restarting.')
            _persist(db, operation_id, digest, journal, 'start-intent')
            authorize()
            current_network = _lookup(docker, 'network', network_name)
            if current_network is None: raise ProvisioningError('Environment network disappeared before startup.')
            _network(current_network, definition, network['Id'])
            verify_members(current_network)
            command([docker, 'start', actual['Id']])
        deadline = time.monotonic() + health_timeout
        while True:
            authorize()
            actual = _json([docker, 'inspect', actual['Id']])[0]
            verify(actual, definition, operation_id, image_id, network['Id'], name, path)
            if actual['State'].get('Running') and actual['State'].get('Health', {}).get('Status') == 'healthy': break
            if not actual['State'].get('Running') or time.monotonic() >= deadline:
                raise ProvisioningError('Application health was not verified. The owned container and capacity remain reserved for recovery.')
            time.sleep(0.25)
    elif actual['State']['Running'] or actual['State'].get('StartedAt', '') not in ('', '0001-01-01T00:00:00Z'):
        raise ProvisioningError('Stopped creation unexpectedly ran. Recovery is required; it was not silently stopped.')
    authorize()
    _persist(db, operation_id, digest, journal, 'verified')
    result = {'container_id': actual['Id'], 'container_name': name, 'compose_project': definition['project'],
            'compose_service': service, 'compose_files': [str(path)], 'image_id': image_id,
            'network_ids': [network['Id']], 'network_name': network_name, 'volume_ids': [],
            'state': definition['desired_state'], 'observed': time.time(), 'definition_digest': digest}
    if descriptors[_digest(definition)]:result['database_network']=descriptors[_digest(definition)]
    return result
