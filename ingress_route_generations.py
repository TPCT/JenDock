"""Durable effective gateway routes, separate from physical bootstrap identity."""
import copy
import hashlib
import json


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def exists(db):return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingress_route_heads'").fetchone())

def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS ingress_route_heads(project TEXT,environment TEXT,gateway_digest TEXT NOT NULL,generation INTEGER NOT NULL,routes TEXT NOT NULL,PRIMARY KEY(project,environment))')
    db.execute('CREATE TABLE IF NOT EXISTS ingress_route_operations(operation_id TEXT PRIMARY KEY,project TEXT NOT NULL,environment TEXT NOT NULL,document TEXT NOT NULL,phase TEXT NOT NULL)')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ingress_route_pending ON ingress_route_operations(project,environment) WHERE phase NOT IN ('committed','aborted')")

def head(db,d):
    if exists(db):
        row=db.execute('SELECT * FROM ingress_route_heads WHERE project=? AND environment=?',(d['project'],d['environment'])).fetchone()
        if row:
            if row['gateway_digest']!=digest(d) or type(row['generation']) is not int or row['generation']<1:raise ValueError('Effective gateway generation identity changed.')
            return {'generation':row['generation'],'routes':json.loads(row['routes'])}
    if d.get('schema_version') in (2,3) and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingress_runtime_intents'").fetchone():
        if db.execute('SELECT 1 FROM ingress_runtime_intents WHERE project=? AND environment=?',(d['project'],d['environment'])).fetchone():
            raise ValueError('Effective gateway generation evidence is missing.')
    return {'generation':1,'routes':copy.deepcopy(d['routes'])}

def token(d,h):return digest({'gateway_digest':digest(d),**h})

def pending(db,project,environment):
    if not exists(db):return None
    row=db.execute("SELECT * FROM ingress_route_operations WHERE project=? AND environment=? AND phase NOT IN ('committed','aborted')",(project,environment)).fetchone()
    return dict(row) if row else None

def seed(db,d):
    if not db.in_transaction:raise ValueError('Effective routes require transaction.')
    head(db,d)  # Existing schema-2 intent must never silently reconstruct lost evidence.
    db.execute('INSERT OR IGNORE INTO ingress_route_heads VALUES(?,?,?,?,?)',(d['project'],d['environment'],digest(d),1,canonical(d['routes'])))
    head(db,d)

def operation(db,operation_id):
    if not exists(db):return None
    row=db.execute('SELECT * FROM ingress_route_operations WHERE operation_id=?',(operation_id,)).fetchone()
    return {**dict(row),'document':json.loads(row['document'])} if row else None

def phase(db,operation_id,expected,target):
    result=db.execute('UPDATE ingress_route_operations SET phase=? WHERE operation_id=? AND phase=?',(target,operation_id,expected))
    if result.rowcount!=1:raise ValueError('Gateway route operation changed concurrently.')


def route_proof_token(project, environment, route):
    import registry
    import build_profiles
    import re
    registry.identifier(project)
    if environment not in registry.ENVIRONMENTS or not isinstance(route,dict) or set(route)!={'hostname','service_id','definition_id','definition_digest','container_id','alias','http_endpoint'}:raise ValueError('Invalid route proof identity.')
    if not isinstance(route['hostname'],str) or len(route['hostname'])>253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+',route['hostname']):raise ValueError('Normalized route hostname required.')
    if any(not re.fullmatch('[a-f0-9]{64}',str(route[key])) for key in ('container_id','definition_digest')):raise ValueError('Pinned backend identity required.')
    if any(not isinstance(route[key],str) or not 1<=len(route[key])<=100 for key in ('service_id','definition_id')):raise ValueError('Pinned service identity required.')
    if not isinstance(route['alias'],str) or not re.fullmatch(re.escape(environment)+r'-[a-z][a-z0-9-]{0,47}',route['alias']):raise ValueError('Owned backend alias required.')
    build_profiles.validate_http_endpoint(route['http_endpoint'])
    return digest({'schema':1,'project':project,'environment':environment,'route':route})
