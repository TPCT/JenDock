"""Closed, authenticated database configuration operations for the worker.

These operations save unverified configuration; they never grant query authority.
The caller must authenticate the Unix peer before dispatching this module.
"""
import database_bindings as bindings
import migration_credentials
import migration_bindings
import application_credentials
import application_database_bindings
from database_auth import ConsoleAuthority, MCPAuthority
from application_operations import OperationError

FIELDS = {
    'database-options': {'reference'},
    'database-preview': {'binding_id','expected_revision'},
    'database-accept': {'preview_id','request_id'},
    'database-read': {'binding_id','expected_revision','request'},
    'database-credential-get': {'credential_id'},
    'database-credentials': {'limit','cursor'},
    'database-credential-save': {'values', 'expected_revision', 'credential_id', 'request_id'},
    'database-credential-revoke': {'credential_id', 'expected_revision'},
    'database-bindings': {'limit','cursor'},
    'database-binding-get': {'binding_id'},
    'database-binding-save': {'values', 'expected_revision', 'binding_id', 'request_id'},
    'database-binding-disable': {'binding_id', 'expected_revision'},
}

FIELDS.update({
    'database-migration-credential-get': {'credential_id'},
    'database-migration-credentials': {'limit','cursor'},
    'database-migration-credential-save': {'values','expected_revision','credential_id','request_id'},
    'database-migration-credential-revoke': {'credential_id','expected_revision'},
    'database-migration-binding-current': set(),
    'database-migration-binding-get': {'binding_id'},
    'database-migration-binding-save': {'values','expected_revision','binding_id','request_id'},
    'database-migration-binding-disable': {'binding_id','expected_revision'},
})
FIELDS.update({
    'database-application-credential-get': {'credential_id'},
    'database-application-credentials': {'limit','cursor'},
    'database-application-credential-save': {'values','expected_revision','credential_id','request_id'},
    'database-application-credential-revoke': {'credential_id','expected_revision'},
    'database-application-bindings': {'limit','cursor'},
    'database-application-binding-get': {'binding_id'},
    'database-application-binding-save': {'values','expected_revision','binding_id','request_id'},
    'database-application-binding-disable': {'binding_id','expected_revision'},
})
SAVE_ACTIONS=('database-application-credential-save','database-application-binding-save',
              'database-credential-save','database-binding-save',
              'database-migration-credential-save','database-migration-binding-save')


def execute(db, body, *, credentials_file=None, key_path=None, inspection_factory=None):
    if not isinstance(body, dict) or body.get('action') not in FIELDS:
        raise OperationError('invalid_input', 'Unknown database operation.', 400)
    action = body['action']
    if set(body) - {'action', 'project', 'environment', 'authentication'} - FIELDS[action]:
        raise OperationError('invalid_input', 'Unexpected database operation fields.', 400)
    if action in SAVE_ACTIONS and not body.get('request_id'):
        raise OperationError('invalid_input','A request ID is required to save database configuration.',400)
    envelope = body.get('authentication')
    if isinstance(envelope,dict) and envelope.get('kind')=='mcp':
        if action not in ('database-bindings','database-binding-get','database-read'):
            raise OperationError('forbidden','MCP database access is read-only.',403)
        authority = MCPAuthority(db,envelope,credentials_file)
    else:
        authority = ConsoleAuthority(db,envelope,credentials_file)
    args = (db, authority.principal, body.get('project'), body.get('environment'))
    options = {'revalidate': authority.recheck}
    if action == 'database-application-credential-get':
        result={'credential':application_credentials.get_credential(*args,body.get('credential_id'),**options)}
    elif action == 'database-application-credentials':
        result={'credentials':application_credentials.list_credentials(*args,limit=body.get('limit',25),cursor=body.get('cursor'),**options)}
    elif action == 'database-application-credential-save':
        result={'credential':application_credentials.save_credential(*args,body.get('values'),expected_revision=body.get('expected_revision'),key_path=key_path,credential_id=body.get('credential_id'),request_id=body.get('request_id'),**options)}
    elif action == 'database-application-credential-revoke':
        result={'credential':application_credentials.revoke_credential(*args,body.get('credential_id'),expected_revision=body.get('expected_revision'),**options)}
    elif action == 'database-application-bindings':
        result={'bindings':application_database_bindings.list_bindings(*args,limit=body.get('limit',25),cursor=body.get('cursor'),**options)}
    elif action == 'database-application-binding-get':
        result={'binding':application_database_bindings.get_binding(*args,body.get('binding_id'),**options)}
    elif action == 'database-application-binding-save':
        result={'binding':application_database_bindings.save_binding(*args,body.get('values'),expected_revision=body.get('expected_revision'),binding_id=body.get('binding_id'),request_id=body.get('request_id'),**options)}
    elif action == 'database-application-binding-disable':
        result={'binding':application_database_bindings.disable_binding(*args,body.get('binding_id'),expected_revision=body.get('expected_revision'),**options)}
    elif action == 'database-migration-credential-get':
        result={'credential':migration_credentials.get_credential(*args,body.get('credential_id'),**options)}
    elif action == 'database-migration-credentials':
        result={'credentials':migration_credentials.list_credentials(*args,limit=body.get('limit',25),cursor=body.get('cursor'),**options)}
    elif action == 'database-migration-credential-save':
        result={'credential':migration_credentials.save_credential(*args,body.get('values'),expected_revision=body.get('expected_revision'),key_path=key_path,credential_id=body.get('credential_id'),request_id=body.get('request_id'),**options)}
    elif action == 'database-migration-credential-revoke':
        result={'credential':migration_credentials.revoke_credential(*args,body.get('credential_id'),expected_revision=body.get('expected_revision'),**options)}
    elif action == 'database-migration-binding-current':
        rows=migration_bindings.list_bindings(*args,**options)
        result={'binding':rows[0] if rows else None}
    elif action == 'database-migration-binding-get':
        result={'binding':migration_bindings.get_binding(*args,body.get('binding_id'),**options)}
    elif action == 'database-migration-binding-save':
        result={'binding':migration_bindings.save_binding(*args,body.get('values'),expected_revision=body.get('expected_revision'),binding_id=body.get('binding_id'),request_id=body.get('request_id'),**options)}
    elif action == 'database-migration-binding-disable':
        result={'binding':migration_bindings.disable_binding(*args,body.get('binding_id'),expected_revision=body.get('expected_revision'),**options)}
    elif action in ('database-options','database-preview','database-accept','database-read'):
        if inspection_factory is None:raise OperationError('unavailable','Database inspection is not configured.',503)
        inspector=inspection_factory(db,authority)
        target=(body.get('project'),body.get('environment'))
        if action=='database-options':result=inspector.options(*target,body.get('reference'))
        elif action=='database-preview':result=inspector.preview(*target,body.get('binding_id'),body.get('expected_revision'))
        elif action=='database-accept':result=inspector.accept(*target,body.get('preview_id'),body.get('request_id'))
        else:result=inspector.read(*target,body.get('binding_id'),body.get('expected_revision'),body.get('request'))
    elif action == 'database-credential-get':
        result = {'credential': bindings.get_credential(*args,body.get('credential_id'),**options)}
    elif action == 'database-credentials':
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
    if action in SAVE_ACTIONS:
        # Save returns a durable receipt, including on replay; current state can
        # have changed since that receipt was first committed.
        result['state_requires_reload']=True
    if action in ('database-credentials','database-bindings','database-migration-credentials','database-application-credentials','database-application-bindings'):
        rows=result['credentials' if action in ('database-credentials','database-migration-credentials','database-application-credentials') else 'bindings']
        result['next_cursor']=rows[-1]['id'] if len(rows)==body.get('limit',25) else None
    authority.recheck()
    return result
