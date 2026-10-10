"""Private immutable Compose provenance matching; never authorizes runtime alone."""
import copy
import hashlib
import hmac
import json
import re

from compose_identity import ordered_files


class ProvenanceError(ValueError):
    pass


FIELDS = frozenset(('schema_version', 'service', 'compose_project', 'observed_files',
                    'canonical_files', 'environment_context_digest', 'semantic_digest', 'fingerprint'))


def _files(value):
    try:
        return ordered_files(value)
    except (ValueError, TypeError):
        raise ProvenanceError('Compose provenance file identity is invalid.') from None


def validate_proof(recipe, service, proof):
    """Validate structure and recipe binding; runtime allowlists are separate."""
    if not isinstance(recipe, dict) or not isinstance(service, str) or not isinstance(proof, dict) or set(proof) != FIELDS:
        raise ProvenanceError('Compose provenance proof is invalid.')
    if type(proof['schema_version']) is not int or proof['schema_version'] != 1:
        raise ProvenanceError('Compose provenance schema is unsupported.')
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', service)
            or proof['service'] != service or proof['compose_project'] != recipe.get('compose_project')
            or not isinstance(proof['compose_project'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', proof['compose_project'])):
        raise ProvenanceError('Compose provenance does not match the registered service.')
    _files(proof['observed_files'])
    if _files(proof['canonical_files']) != _files(recipe.get('compose_files')):
        raise ProvenanceError('Compose provenance does not match the registered invocation.')
    for field in ('environment_context_digest', 'semantic_digest', 'fingerprint'):
        if not isinstance(proof[field], str) or not re.fullmatch('[a-f0-9]{64}', proof[field]):
            raise ProvenanceError('Compose provenance digest is invalid.')
    document = {key: value for key, value in proof.items() if key != 'fingerprint'}
    expected = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    if not hmac.compare_digest(expected, proof['fingerprint']):
        raise ProvenanceError('Compose provenance fingerprint is inconsistent.')
    return copy.deepcopy(proof)


def validate(recipe, service):
    """Return a validated private copy; no entry means exact legacy matching."""
    if not isinstance(recipe, dict):
        raise ProvenanceError('Compose recipe is invalid.')
    entries = recipe.get('service_provenance', {})
    if not isinstance(entries, dict):
        raise ProvenanceError('Compose provenance mapping is invalid.')
    if service not in entries:
        return None
    return validate_proof(recipe, service, entries[service])


def matches(recipe, group, service, files):
    """Exact registered service ownership plus canonical/verified invocation."""
    try:
        if (not isinstance(recipe, dict) or not isinstance(group, str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', group)
                or group != recipe.get('compose_project')):
            return False
        allowed = recipe.get('owned_services', recipe.get('services'))
        if (not isinstance(allowed, list) or not isinstance(service, str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', service) or service not in allowed):
            return False
        observed = _files(files)
        canonical = _files(recipe.get('compose_files'))
        proof = validate(recipe, service)
        return observed == canonical or proof is not None and observed == _files(proof['observed_files'])
    except (ProvenanceError, TypeError, ValueError):
        return False


def revalidate(recipe, service, resolver):
    """Rebuild proof from original invocation even after canonical recreation.

    Callback signature: resolver(recipe, service, original_observed_files).
    It returns a full proof or a reconciliation result containing ``proof``.
    No proof entry means canonical matching needs no reconciliation callback.
    """
    proof = validate(recipe, service)
    if proof is None:
        return None
    try:
        result = resolver(copy.deepcopy(recipe), service, list(proof['observed_files']))
        if isinstance(result, dict) and 'proof' in result:
            if result.get('equivalent') is False:
                raise ProvenanceError('Compose configuration reconciliation changed.')
            result = result['proof']
        current = validate_proof(recipe, service, result)
    except Exception:
        raise ProvenanceError('Compose provenance could not be revalidated. Refresh reconciliation before mutation.') from None
    if not hmac.compare_digest(current['fingerprint'], proof['fingerprint']):
        raise ProvenanceError('Compose provenance changed. Refresh reconciliation before mutation.')
    return current
