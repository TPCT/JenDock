"""Closed, authenticated database configuration operations for the worker.

These operations save unverified configuration; they never grant query authority.
The caller must authenticate the Unix peer before dispatching this module.
"""
import database_bindings as bindings
from database_auth import ConsoleAuthority
from application_operations import OperationError

FIELDS = {
    'database-credentials': {'limit','cursor'},
    'database-credential-save': {'values', 'expected_revision', 'credential_id', 'request_id'},
    'database-credential-revoke': {'credential_id', 'expected_revision'},
    'database-bindings': {'limit','cursor'},
    'database-binding-get': {'binding_id'},
    'database-binding-save': {'values', 'expected_revision', 'binding_id', 'request_id'},
    'database-binding-disable': {'binding_id', 'expected_revision'},
}


def execute(db, body, *, credentials_file=None, key_path=None):
    if not isinstance(body, dict) or body.get('action') not in FIELDS:
        raise OperationError('invalid_input', 'Unknown database operation.', 400)
    action = body['action']
    if set(body) - {'action', 'project', 'environment', 'authentication'} - FIELDS[action]:
        raise OperationError('invalid_input', 'Unexpected database operation fields.', 400)
    if action in ('database-credential-save','database-binding-save') and not body.get('request_id'):
        raise OperationError('invalid_input','A request ID is required to save database configuration.',400)
    authority = ConsoleAuthority(db, body.get('authentication'), credentials_file)
    args = (db, authority.principal, body.get('project'), body.get('environment'))
    options = {'revalidate': authority.recheck}
    if action == 'database-credentials':
        result = {'credentials': bindings.list_credentials(*args, limit=body.get('limit',25),cursor=body.get('cursor'), **options)}
    elif action == 'database-credential-save':
        result = {'credential': bindings.save_credential(*args, body.get('values'),
            expected_revision=body.get('expected_revision'), key_path=key_path,
            credential_id=body.get('credential_id'), request_id=body.get('request_id'), **options)}
    elif action == 'database-credential-revoke':
        result = {'credential': bindings.revoke_credential(*args, body.get('credential_id'),
            expected_revision=body.get('expected_revision'), **options)}
    elif action == 'database-bindings':
        result = {'bindings': bindings.list_bindings(*args, limit=body.get('limit',25),cursor=body.get('cursor'), **options)}
    elif action == 'database-binding-get':
        result = {'binding': bindings.get_binding(*args, body.get('binding_id'), **options)}
    elif action == 'database-binding-save':
        result = {'binding': bindings.save_binding(*args, body.get('values'),
            expected_revision=body.get('expected_revision'), binding_id=body.get('binding_id'), request_id=body.get('request_id'), **options)}
    else:
        result = {'binding': bindings.disable_binding(*args, body.get('binding_id'),
            expected_revision=body.get('expected_revision'), **options)}
    if action in ('database-credential-save','database-binding-save'):
        # Save returns a durable receipt, including on replay; current state can
        # have changed since that receipt was first committed.
        result['state_requires_reload']=True
    if action in ('database-credentials','database-bindings'):
        rows=result['credentials' if action=='database-credentials' else 'bindings']
        result['next_cursor']=rows[-1]['id'] if len(rows)==body.get('limit',25) else None
    authority.recheck()
    return result
