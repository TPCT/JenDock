"""Transactional current backend evidence; accepted gateway configuration stays immutable."""
import copy
import hashlib
import json
import re
import ingress_route_generations as generations


def _canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def _digest(value):return hashlib.sha256(_canonical(value).encode()).hexdigest()

def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS ingress_binding_sets(project TEXT,environment TEXT,gateway_digest TEXT NOT NULL,PRIMARY KEY(project,environment))')
    db.execute('CREATE TABLE IF NOT EXISTS ingress_backend_bindings(project TEXT,environment TEXT,service_id TEXT,gateway_digest TEXT NOT NULL,generation INTEGER NOT NULL,backend TEXT NOT NULL,PRIMARY KEY(project,environment,service_id))')

def seed(db,definition):
    """Called inside gateway intent transaction, before any runtime effects."""
    if not db.in_transaction:raise ValueError('Route evidence requires a caller transaction.')
    key=(definition['project'],definition['environment']);digest=_digest(definition)
    old=db.execute('SELECT gateway_digest FROM ingress_binding_sets WHERE project=? AND environment=?',key).fetchone()
    if old:
        if old['gateway_digest']!=digest:raise ValueError('Gateway route configuration changed.')
        current_routes(db,definition)
        return
    db.execute('INSERT INTO ingress_binding_sets VALUES(?,?,?)',(*key,digest))
    backends={}
    for route in definition['routes']:
        backend={k:copy.deepcopy(v) for k,v in route.items() if k!='hostname'}
        if backend['service_id'] in backends and backends[backend['service_id']]!=backend:raise ValueError('Conflicting backend evidence.')
        backends[backend['service_id']]=backend
    for sid,backend in backends.items():db.execute('INSERT INTO ingress_backend_bindings VALUES(?,?,?,?,?,?)',(*key,sid,digest,1,_canonical(backend)))

def capture(db,project,environment,service_id,*,allow_pending=False):
    if not allow_pending and generations.pending(db,project,environment):raise ValueError("Gateway route addition is pending; finish recovery before delivery.")
    expected=False;expected_digest=None;accepted=[]
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingress_runtime_intents'").fetchone():
        intent=db.execute('SELECT definition,digest FROM ingress_runtime_intents WHERE project=? AND environment=?',(project,environment)).fetchone()
        if intent:
            definition=json.loads(intent['definition'])
            if _digest(definition)!=intent['digest']:raise ValueError('Gateway route intent changed.')
            accepted=[route for route in generations.head(db,definition)['routes'] if route['service_id']==service_id]
            expected=bool(accepted)
            expected_digest=intent['digest']
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingress_binding_sets'").fetchone():
        if expected:raise ValueError('Current route evidence is missing; reconcile gateway first.')
        return None
    head=db.execute('SELECT gateway_digest FROM ingress_binding_sets WHERE project=? AND environment=?',(project,environment)).fetchone()
    if not head:
        if db.execute('SELECT 1 FROM ingress_backend_bindings WHERE project=? AND environment=?',(project,environment)).fetchone():raise ValueError('Gateway route evidence header is missing.')
        if expected:raise ValueError('Current route evidence is missing; reconcile gateway first.')
        return None
    if expected_digest is not None and head['gateway_digest']!=expected_digest:raise ValueError('Gateway route evidence differs from accepted intent.')
    row=db.execute('SELECT * FROM ingress_backend_bindings WHERE project=? AND environment=? AND service_id=?',(project,environment,service_id)).fetchone()
    if not row:
        if expected:raise ValueError('Current route evidence is missing; reconcile gateway first.')
        return None
    if expected_digest is not None and not expected:raise ValueError('Backend evidence is outside accepted gateway routes.')
    if row['gateway_digest']!=head['gateway_digest']:raise ValueError('Route configuration identity changed.')
    try:backend=json.loads(row['backend'])
    except (ValueError,TypeError):raise ValueError('Malformed route backend evidence.') from None
    fields={'service_id','definition_id','definition_digest','container_id','alias','http_endpoint'}
    if (type(row['generation']) is not int or row['generation']<1 or not isinstance(backend,dict) or set(backend)!=fields
            or backend['service_id']!=service_id
            or not isinstance(backend['definition_id'],str) or not 1<=len(backend['definition_id'])<=100
            or any(not isinstance(backend[k],str) or not re.fullmatch('[a-f0-9]{64}',backend[k]) for k in ('container_id','definition_digest'))
            or not isinstance(backend['alias'],str) or not re.fullmatch(re.escape(environment)+r'-[a-z][a-z0-9-]{0,47}',backend['alias'])):
        raise ValueError('Malformed route backend evidence.')
    endpoint=backend['http_endpoint']
    if (not isinstance(endpoint,dict) or set(endpoint)!={'port','health_path'} or type(endpoint['port']) is not int or not 1<=endpoint['port']<=65535
            or not isinstance(endpoint['health_path'],str) or not 1<=len(endpoint['health_path'])<=240
            or not re.fullmatch(r'/[A-Za-z0-9._~/-]*',endpoint['health_path'])
            or endpoint['health_path']!='/' and any(part in ('','.','..') for part in endpoint['health_path'][1:].split('/'))):
        raise ValueError('Malformed route endpoint evidence.')
    if any(backend['alias']!=route['alias'] or backend['http_endpoint']!=route['http_endpoint'] for route in accepted):
        raise ValueError('Route evidence differs from immutable alias or endpoint.')
    return {'project':project,'environment':environment,'service_id':service_id,'gateway_digest':row['gateway_digest'],'generation':row['generation'],'backend':backend}

def preflight(snapshot,*,alias,http_endpoint):
    if snapshot is None:return
    if alias!=snapshot['backend']['alias'] or http_endpoint!=snapshot['backend']['http_endpoint']:
        raise ValueError('Ingress endpoint or DNS alias changes require explicit gateway reconfiguration before deployment.')

def advance(db,snapshot,*,definition_id,definition_digest,container_id,alias,http_endpoint):
    if snapshot is None:return None
    if not db.in_transaction:raise ValueError('Route evidence requires a caller transaction.')
    preflight(snapshot,alias=alias,http_endpoint=http_endpoint)
    if not isinstance(definition_id,str) or not definition_id or any(not re.fullmatch('[a-f0-9]{64}',str(v)) for v in (definition_digest,container_id)):raise ValueError('Invalid current backend evidence.')
    row=db.execute('SELECT document,digest FROM service_definitions WHERE id=?',(definition_id,)).fetchone()
    if not row or row['digest']!=definition_digest:raise ValueError('Target service definition is not registered.')
    document=json.loads(row['document'])
    if _digest(document)!=definition_digest or any(document.get(k)!=snapshot[k] for k in ('project','environment','service_id')) or document.get('artifact',{}).get('http_endpoint')!=http_endpoint or alias!=snapshot['environment']+'-'+document.get('name',''):
        raise ValueError('Target route definition belongs to another service, environment, or endpoint.')
    service=db.execute('SELECT project,environment,definition_id FROM managed_services WHERE id=?',(snapshot['service_id'],)).fetchone()
    bound=db.execute('SELECT document FROM service_bindings WHERE service_id=?',(snapshot['service_id'],)).fetchone()
    binding=json.loads(bound['document']) if bound else {}
    if not service or service['project']!=snapshot['project'] or service['environment']!=snapshot['environment'] or service['definition_id']!=definition_id or binding.get('container_id')!=container_id or binding.get('compose_service')!=alias or binding.get('definition_digest')!=definition_digest:
        raise ValueError('Route evidence must match the current committed application binding.')
    current=capture(db,snapshot['project'],snapshot['environment'],snapshot['service_id'])
    target=dict(snapshot['backend'],definition_id=definition_id,definition_digest=definition_digest,container_id=container_id)
    if current and current['gateway_digest']==snapshot['gateway_digest'] and current['generation']==snapshot['generation']+1 and current['backend']==target:return current
    if current!=snapshot:raise ValueError('Route generation changed; reconcile before retrying.')
    if current['backend']==target:return current
    result=db.execute('UPDATE ingress_backend_bindings SET generation=generation+1,backend=? WHERE project=? AND environment=? AND service_id=? AND gateway_digest=? AND generation=? AND backend=?',(_canonical(target),snapshot['project'],snapshot['environment'],snapshot['service_id'],snapshot['gateway_digest'],snapshot['generation'],_canonical(snapshot['backend'])))
    if result.rowcount!=1:raise ValueError('Route generation changed; reconcile before retrying.')
    return capture(db,snapshot['project'],snapshot['environment'],snapshot['service_id'])

def current_routes(db,definition):
    routes=[]
    head=None
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingress_binding_sets'").fetchone():
        head=db.execute('SELECT gateway_digest FROM ingress_binding_sets WHERE project=? AND environment=?',(definition['project'],definition['environment'])).fetchone()
    if head and head['gateway_digest']!=_digest(definition):raise ValueError('Gateway route configuration changed.')
    for route in generations.head(db,definition)['routes']:
        snapshot=capture(db,definition['project'],definition['environment'],route['service_id'],allow_pending=True)
        if head and snapshot is None:raise ValueError('Current route evidence is missing.')
        if snapshot:
            preflight(snapshot,alias=route['alias'],http_endpoint=route['http_endpoint'])
            routes.append(dict(snapshot['backend'],hostname=route['hostname']))
        else:routes.append(copy.deepcopy(route))
    return routes
