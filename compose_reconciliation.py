"""Pure conservative comparisons of worker-resolved Compose documents.

Equality proves configuration equivalence only, never runtime ownership. Digests
and invocation proofs are worker-private: configuration may contain secrets.
"""
import hashlib
import json
import re

from compose_identity import ordered_files


class ReconciliationError(ValueError):
    pass


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise ReconciliationError('Compose configuration is not valid JSON data.') from None


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value):
        raise ReconciliationError('Compose reference identity is invalid.')
    return value


def _references(value):
    if isinstance(value, dict):
        return [_name(key) for key in value]
    if isinstance(value, list):
        return [_name(item) for item in value]
    raise ReconciliationError('Compose reference structure is unsupported.')


def _projection(config, service):
    if not isinstance(config, dict) or not isinstance(config.get('services'), dict):
        raise ReconciliationError('Resolved Compose services are required.')
    _canonical(config)
    selected, pending = {}, [service]
    resources = {kind: set() for kind in ('networks', 'volumes', 'configs', 'secrets')}
    while pending:
        name = _name(pending.pop())
        if name in selected:
            continue
        value = config['services'].get(name)
        if not isinstance(value, dict):
            raise ReconciliationError('A selected service or referenced dependency is missing.')
        selected[name] = value
        pending.extend(_references(value.get('depends_on', {})))
        for field in ('network_mode', 'ipc', 'pid'):
            reference = value.get(field)
            if isinstance(reference, str) and reference.startswith('container:'):
                raise ReconciliationError('External container dependencies need separate verification.')
            if isinstance(reference, str) and reference.startswith('service:'):
                pending.append(_name(reference[8:]))
        for field in ('links', 'volumes_from'):
            refs = value.get(field, [])
            if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
                raise ReconciliationError('Compose dependency structure is unsupported.')
            for reference in refs:
                if reference.startswith('container:'):
                    raise ReconciliationError('External container dependencies need separate verification.')
                pending.append(_name(reference.split(':', 1)[0]))
        if 'extends' in value:
            raise ReconciliationError('Compose inheritance must be resolved before comparison.')
        for kind in ('networks', 'configs', 'secrets'):
            refs = value.get(kind, {} if kind == 'networks' else [])
            if kind == 'networks':
                resources[kind].update(_references(refs))
                if not refs and not value.get('network_mode'):
                    resources[kind].add('default')
            else:
                if not isinstance(refs, list):
                    raise ReconciliationError('Compose resource structure is unsupported.')
                resources[kind].update(_name(ref.get('source') if isinstance(ref, dict) else ref) for ref in refs)
        mounts = value.get('volumes', [])
        if not isinstance(mounts, list):
            raise ReconciliationError('Resolved Compose mounts are required.')
        for mount in mounts:
            if not isinstance(mount, dict):
                raise ReconciliationError('Compose mounts must use resolved long syntax.')
            if mount.get('type') == 'volume' and mount.get('source'):
                resources['volumes'].add(_name(mount['source']))
    result = {'services': selected}
    for kind, names in resources.items():
        definitions = config.get(kind, {})
        if not isinstance(definitions, dict):
            raise ReconciliationError('Compose resource definitions are invalid.')
        if names - definitions.keys():
            raise ReconciliationError('A referenced Compose resource definition is missing.')
        result[kind] = {name: definitions[name] for name in names}
    # Fully resolved documents already contain expanded service semantics. Only
    # top-level x-* definition metadata is ignored, per the Compose specification:
    # https://docs.docker.com/reference/compose-file/extension/
    # Unknown non-extension global configuration remains part of the comparison.
    result['global'] = {key: value for key, value in config.items()
                        if key not in ('name', 'version', 'services', *resources)
                        and not (isinstance(key, str) and key.startswith('x-'))}
    return result


def compare_service(observed_config, intended_config, service):
    """Return fixed-category mismatch paths and a private digest on equality."""
    _name(service)
    observed = _projection(observed_config, service)
    intended = _projection(intended_config, service)
    mismatches = []
    # Do not echo arbitrary resource names, field names, environment keys or values.
    for category in ('services', 'networks', 'volumes', 'configs', 'secrets', 'global'):
        if _canonical(observed[category]) != _canonical(intended[category]):
            if category == 'services' and set(observed[category]) == set(intended[category]):
                safe_fields = {'image', 'environment', 'env_file', 'command', 'entrypoint', 'ports', 'volumes',
                               'networks', 'depends_on', 'network_mode', 'ipc', 'pid', 'links', 'volumes_from',
                               'configs', 'secrets', 'healthcheck', 'user', 'working_dir', 'restart', 'deploy',
                               'build', 'privileged', 'cap_add', 'cap_drop', 'devices', 'security_opt', 'labels'}
                for name in observed[category]:
                    left, right = observed[category][name], intended[category][name]
                    for field in left.keys() | right.keys():
                        if field not in left or field not in right or _canonical(left[field]) != _canonical(right[field]):
                            mismatches.append('services[*].' + (field if field in safe_fields else '[other-field]'))
            else:
                mismatches.append(category)
    return {'equivalent': not mismatches, 'mismatches': sorted(set(mismatches)),
            'digest': _digest(observed) if not mismatches else None}


def reconcile(service, observed_config, canonical_config, observed_files, canonical_files, *,
              compose_project, environment_context_digest):
    """Bind successful semantic equality to two exact ordered invocations.

    The caller must hash actual env/config/secret file bytes and resolution
    context into environment_context_digest. A path in resolved JSON alone
    never proves that referenced content or live runtime is unchanged.
    """
    _name(compose_project)
    if not isinstance(environment_context_digest, str) or not re.fullmatch('[a-f0-9]{64}', environment_context_digest):
        raise ReconciliationError('A verified environment context digest is required.')
    old, new = ordered_files(observed_files), ordered_files(canonical_files)
    result = compare_service(observed_config, canonical_config, service)
    proof = None
    if result['equivalent']:
        proof = {'schema_version': 1, 'service': service, 'compose_project': compose_project,
                 'observed_files': list(old), 'canonical_files': list(new),
                 'environment_context_digest': environment_context_digest, 'semantic_digest': result['digest']}
        proof['fingerprint'] = _digest(proof)
    return {**result, 'proof': proof}
