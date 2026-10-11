"""Trusted workload identity derived from the immutable managed definition.

These values are injected at provisioning, never saved as editable runtime data.
The digest covers the accepted definition before injection, avoiding a circular pin.
"""
import hashlib
import registry

PREFIX = 'DEPLOY_MONITOR_'


def reserved(key):
    return isinstance(key, str) and key.startswith(PREFIX)


def validate_user_values(values):
    if any(reserved(key) for key in values):
        raise ValueError('Platform variables are managed by the workload definition.')


def user_values(values):
    """Copy observed app values without capturing injected platform authority."""
    return {key: value for key, value in values.items() if not reserved(key)}


def metadata(definition):
    workload = definition.get('artifact', {}).get('workload')
    if workload is None:
        return {}
    return {
        'DEPLOY_MONITOR_PROJECT': definition['project'],
        'DEPLOY_MONITOR_ENVIRONMENT': definition['environment'],
        'DEPLOY_MONITOR_SERVICE_ID': definition['service_id'],
        'DEPLOY_MONITOR_DEFINITION_DIGEST': hashlib.sha256(registry.canonical(definition).encode()).hexdigest(),
        'DEPLOY_MONITOR_WORKLOAD': registry.canonical(workload),
    }
