"""Private bearer MCP bootstrap. SDK owns protocol; application operations own writes.

Run with uvicorn mcp_server:app_from_env --factory --no-proxy-headers.
Bind only to loopback or a private network; no Docker socket belongs in this process.
This is not an OAuth authorization server or OAuth-only connector compatibility claim.
"""
from collections import OrderedDict, deque
from contextvars import ContextVar
from contextlib import contextmanager
import json
import os
import sqlite3
import time

import anyio
import jsonschema
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp import types
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
import application_operations as ops
import mcp_credentials
import ingress

_credential = ContextVar('mcp_credential')
SCOPES = {'project_list': 'projects:read', 'project_get': 'projects:read',
          'project_create': 'projects:create', 'service_list': 'services:read',
          'service_get': 'services:read', 'service_create': 'services:create',
          'service_templates': 'services:create',
          'operation_status': 'operations:read',
          'domain_options': 'domains:read', 'domain_list': 'domains:read',
          'domain_get': 'domains:read', 'domain_operation_status': 'domains:read'}
MUTATING_TOOLS = frozenset({'project_create', 'service_create'})


def string(n=120): return {'type': 'string', 'minLength': 1, 'maxLength': n}
def obj(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}

PAGE = {'cursor': {'type': 'string', 'pattern': '^[0-9]{1,8}$'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 25}}
ENV = {'type': 'string', 'enum': ['development', 'production']}
KEY = {'type': 'string', 'pattern': '^[A-Za-z0-9-]{16,80}$'}
REV = {'type': 'integer', 'minimum': 1, 'maximum': 2147483647}
DEFINITION = obj({'name': string(48), 'artifact_id': string(), 'template_id': string(64),
                  'template_revision': REV, 'environment_revision': REV,
                  'policy_revision': {'type': 'integer', 'minimum': 0, 'maximum': 2147483647},
                  'resources': obj({'memory_mib': {'type': 'integer', 'minimum': 1, 'maximum': 1048576},
                                    'cpu': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1024}}, ['memory_mib', 'cpu']),
                  'desired_state': {'type': 'string', 'enum': ['stopped', 'running'], 'default': 'stopped'}},
                 ['name', 'artifact_id', 'template_id', 'template_revision', 'environment_revision', 'policy_revision', 'resources'])
SCHEMAS = {
    'domain_options': obj({'project_id': string(), 'environment': ENV}, ['project_id', 'environment']),
    'domain_list': obj({'project_id': string(), 'environment': ENV, 'limit': PAGE['limit'], 'cursor': {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}}, ['project_id', 'environment']),
    'domain_get': obj({'project_id': string(), 'environment': ENV, 'route_id': string(32)}, ['project_id', 'environment', 'route_id']),
    'domain_operation_status': obj({'project_id': string(), 'environment': ENV, 'operation_id': string(32)}, ['project_id', 'environment', 'operation_id']),
    'project_list': obj({**PAGE, 'cursor': string()}), 'project_get': obj({'project_id': string()}, ['project_id']),
    'project_create': obj({'project_id': string(), 'repository': string(200), 'repository_id': {'type': 'integer', 'minimum': 1, 'maximum': 9223372036854775807},
                           'label': string(), 'integration_ref': {'type': 'string', 'enum': ['github']},
                           'idempotency_key': KEY}, ['project_id', 'repository', 'repository_id', 'idempotency_key']),
    'service_list': obj({'project_id': string(), 'environment': ENV, **PAGE}, ['project_id', 'environment']),
    'service_templates': obj({'project_id': string(), 'environment': ENV}, ['project_id', 'environment']),
    'service_get': obj({'service_id': string()}, ['service_id']),
    'service_create': obj({'project_id': string(), 'environment': ENV, 'definition': DEFINITION,
                           'idempotency_key': KEY}, ['project_id', 'environment', 'definition', 'idempotency_key']),
    'operation_status': obj({'operation_id': string(), 'after_event': {'type':'integer', 'minimum':0, 'maximum':9223372036854775807, 'default':0}, 'limit': PAGE['limit']}, ['operation_id']),
}


def select(value, keys):
    return {k: value[k] for k in keys if k in value}


def public_project(value, principal=None):
    result = select(value, ('id', 'label', 'repository', 'repository_id', 'revision', 'created', 'updated'))
    result['environments'] = {name: select(env, ('name', 'environment', 'branch', 'revision', 'state', 'automatic'))
                              for name, env in value.get('environments', {}).items()
                              if principal is None or principal.allowed_environments is None or (value['id'], name) in principal.allowed_environments}
    return result


def public_service(value):
    result = select(value, ('id', 'project', 'environment', 'name', 'state', 'status', 'desired_state', 'revision', 'created', 'updated'))
    result['definition'] = select(value.get('definition', {}), ('name', 'artifact_id', 'template_id', 'template_revision', 'environment_revision', 'policy_revision', 'desired_state'))
    resources = value.get('definition', {}).get('resources', {})
    result['definition']['resources'] = select(resources, ('memory_mib', 'cpu'))
    result['container'] = select(value.get('container') or {}, ('id', 'name', 'evidence', 'observed_at')) or None
    return result


def public_templates(value, environment, principal):
    # Legacy-unclassified artifacts require a separate explicit adoption decision.
    # They cannot be implicitly treated as Development artifacts by this adapter.
    templates = []
    for item in value.get('templates', []):
        if item.get('source_environment') != environment:
            continue
        entry = select(item, ('id', 'revision', 'label', 'artifact_id', 'image', 'image_name',
                              'source_job', 'source_sha', 'source_environment'))
        entry['desired_states'] = [state for state in item.get('desired_states', [])
                                   if state == 'stopped' or state == 'running' and 'services:start' in principal.scopes]
        templates.append(entry)
    return {**select(value, ('environment_revision', 'policy_revision')), 'templates': templates,
            'empty_reason': None if templates else 'No verified artifact is available for the selected environment. Publish an environment-specific build before creating a service.'}


def public_operation(value):
    # Worker exception detail and links are deliberately not forwarded.
    return select(value, ('operation_id', 'status', 'resource_id', 'revision'))


def page(items, args):
    start, limit = int(args.get('cursor', '0')), args.get('limit', 25)
    return {'items': items[start:start + limit], 'next_cursor': str(start + limit) if start + limit < len(items) else None}


def result(payload, error=False):
    encoded = json.dumps(payload, allow_nan=False)
    if len(encoded.encode()) > 262144:
        return result({'error': {'code': 'result_too_large', 'message': 'Request a smaller page.'}}, True)
    return types.CallToolResult(content=[types.TextContent(type='text', text=encoded)], structuredContent=payload, isError=error)


def create_app(db_path, key_path, *, allowed_hosts, allowed_origins=(), owner_resolver=None, enable_creation=False):
    if not allowed_hosts or any('*' in h for h in (*allowed_hosts, *allowed_origins)) or 'null' in allowed_origins:
        raise ValueError('Exact Host and Origin allowlists are required.')

    # AnyIO propagates request ContextVars into each worker. Keep the limit
    # explicit instead of using the shared default executor's capacity.
    work_limiter = anyio.CapacityLimiter(8)

    @contextmanager
    def connection():
        db = sqlite3.connect(db_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try: yield db
        finally: db.close()

    def principal(db):
        return mcp_credentials.principal_for_credential(db, _credential.get(), owner_resolver=owner_resolver)

    def available(p):
        return [name for name, scope in SCOPES.items() if scope in p.scopes and (enable_creation or name not in MUTATING_TOOLS)]

    def list_tools_sync():
        with connection() as db:
            p = principal(db)
            return types.ListToolsResult(tools=[types.Tool(name=name, inputSchema=SCHEMAS[name],
                description=('Create using a stable idempotency key. Service desired_state=running starts this service; queued is not success.'
                             if name in MUTATING_TOOLS else 'Read authorized public deployment information.'),
                annotations=types.ToolAnnotations(readOnlyHint=name not in MUTATING_TOOLS, destructiveHint=False,
                                                  idempotentHint=True, openWorldHint=name in MUTATING_TOOLS))
                for name in available(p)])

    def call_tool_sync(params):
        try:
            with connection() as db:
                p = principal(db)
                if params.name not in available(p):
                    return result({'error': {'code': 'forbidden', 'message': 'This tool is not available.'}}, True)
                a = params.arguments or {}
                errors = list(jsonschema.Draft202012Validator(SCHEMAS[params.name]).iter_errors(a))
                if errors:
                    return result({'error': {'code': 'invalid_input', 'field': '.'.join(map(str, errors[0].absolute_path)),
                                             'message': 'Check this field against the advertised tool schema.'}}, True)
                name = params.name
                if name == 'project_list':
                    values = ops.list_projects(db, p, limit=a.get('limit', 25), cursor=a.get('cursor'))
                    payload = {'projects': [public_project(v) for v in values['projects']], 'next_cursor': values['next_cursor']}
                elif name == 'project_get': payload = public_project(ops.get_project(db, p, a['project_id']))
                elif name == 'service_list': payload = page([public_service(v) for v in ops.list_services(db, p, a['project_id'], a['environment'])], a)
                elif name == 'service_templates': payload = public_templates(ops.service_templates(db, p, a['project_id'], a['environment']), a['environment'], p)
                elif name == 'service_get': payload = public_service(ops.get_service(db, p, a['service_id']))
                elif name == 'operation_status': payload = ops.operation_status(db, p, a['operation_id'], after_event=a.get('after_event', 0), limit=a.get('limit', 25))
                elif name == 'domain_options': payload = ingress.domain_options(db, p, a['project_id'], a['environment'])
                elif name == 'domain_list': payload = ingress.list_routes(db, p, a['project_id'], a['environment'], limit=a.get('limit', 25), cursor=a.get('cursor'))
                elif name == 'domain_get': payload = ingress.get_route(db, p, a['project_id'], a['environment'], a['route_id'])
                elif name == 'domain_operation_status': payload = ingress.get_operation(db, p, a['project_id'], a['environment'], a['operation_id'])
                elif name == 'project_create':
                    value = ops.project_input(a['project_id'], {k: a[k] for k in ('repository', 'repository_id', 'label') if k in a}, a.get('integration_ref', 'github'))
                    created = ops.create_project(db, p, value, a['idempotency_key'], key_path)
                    payload = {'project': public_project(created['project'], p), 'operation': public_operation(created['operation'])}
                else:
                    created = ops.create_service(db, p, ops.service_input(a['project_id'], a['environment'], a['definition']), a['idempotency_key'])
                    payload = {'service': public_service(created['service']), 'operation': public_operation(created['operation'])}
                return result(payload)
        except ops.OperationError as error:
            code = error.code if error.status not in (403, 404) else 'not_available'
            return result({'error': {'code': code, 'message': 'Operation could not be completed. Check authorization, input and current revisions.'}}, True)
        except Exception:
            # No request arguments, token, provider exception or traceback in responses/logs.
            return result({'error': {'code': 'not_available', 'message': 'Requested information or operation is unavailable.'}}, True)

    async def list_tools(ctx, params):
        return await anyio.to_thread.run_sync(list_tools_sync, limiter=work_limiter)

    async def call_tool(ctx, params):
        return await anyio.to_thread.run_sync(call_tool_sync, params, limiter=work_limiter)

    server = Server('Shiftcodes deployment', version='1.0', on_list_tools=list_tools, on_call_tool=call_tool,
                    get_tool_input_schema=lambda name: SCHEMAS.get(name))
    sdk = server.streamable_http_app(streamable_http_path='/_deploy/mcp', json_response=True, stateless_http=True,
        max_request_body_size=65536, transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=list(allowed_hosts), allowed_origins=list(allowed_origins)))
    app = BearerBoundary(sdk, connection, allowed_hosts, allowed_origins, owner_resolver, work_limiter)
    return CORSMiddleware(app, allow_origins=list(allowed_origins), allow_methods=['POST'],
                          allow_headers=['Authorization', 'Content-Type', 'MCP-Protocol-Version', 'Mcp-Method', 'Mcp-Name'],
                          expose_headers=['MCP-Protocol-Version']) if allowed_origins else app


class BearerBoundary:
    def __init__(self, app, connection, hosts, origins, owner_resolver, work_limiter):
        self.app, self.connection, self.hosts, self.origins = app, connection, set(hosts), set(origins)
        self.owner_resolver, self.rates = owner_resolver, OrderedDict()
        self.work_limiter = work_limiter

    def authenticate(self, token):
        # Connections never cross thread boundaries, including on close/error.
        with self.connection() as db:
            return mcp_credentials.authenticate(db, token, owner_resolver=self.owner_resolver)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http': return await self.app(scope, receive, send)
        headers = {}
        for k, v in scope['headers']:
            key = k.decode().lower()
            if key in headers and key in ('authorization', 'host', 'origin'):
                return await JSONResponse({'error': 'invalid_headers'}, status_code=400)(scope, receive, send)
            headers[key] = v.decode('latin1')
        status = None
        if headers.get('host') not in self.hosts: status = 421
        elif 'origin' in headers and headers['origin'] not in self.origins: status = 403
        elif any(h in headers for h in ('forwarded', 'x-forwarded-host', 'x-forwarded-proto', 'x-forwarded-for')): status = 400
        if status: return await JSONResponse({'error': 'invalid_request_origin'}, status_code=status)(scope, receive, send)
        auth = headers.get('authorization', '')
        try:
            if not auth.startswith('Bearer ') or len(auth) > 512: raise ValueError()
            p = await anyio.to_thread.run_sync(self.authenticate, auth[7:], limiter=self.work_limiter)
        except Exception:
            return await JSONResponse({'error': 'invalid_credential'}, status_code=401,
                                      headers={'WWW-Authenticate': 'Bearer realm="deploy-mcp"'})(scope, receive, send)
        now = time.monotonic()
        bucket = self.rates.setdefault(p.subject_id, deque())
        self.rates.move_to_end(p.subject_id)
        while bucket and bucket[0] < now - 60: bucket.popleft()
        if len(bucket) >= 120:
            return await JSONResponse({'error': 'rate_limited'}, status_code=429, headers={'Retry-After': '60'})(scope, receive, send)
        bucket.append(now)
        while len(self.rates) > 4096: self.rates.popitem(last=False)
        token = _credential.set(p.credential_id)
        try: await self.app(scope, receive, send)
        finally: _credential.reset(token)


def app_from_env():
    mcp_credentials.validate_owner_configuration()
    return create_app(os.environ['DEPLOY_DB'], os.environ['DEPLOY_KEY_PATH'],
                      allowed_hosts=os.environ['MCP_ALLOWED_HOSTS'].split(','),
                      allowed_origins=tuple(filter(None, os.getenv('MCP_ALLOWED_ORIGINS', '').split(','))),
                      enable_creation=os.getenv('MCP_ENABLE_CREATION') == '1')
