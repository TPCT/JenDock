"""Persistent environment authorization for automatic, typed migrations.

Configuration is not runtime verification. Webhook execution rechecks enabled
configuration and credential revisions, not the lifetime of its author's session.
"""
import json
import re
import time
import uuid
import database_bindings as common
import migration_credentials as credentials
import registry

BindingError=common.BindingError
GRAMMAR='laravel12-stock-postgresql-v1'
FIELDS={'engine','adapter_version','ownership','database_name','database_port','secret_id','secret_revision','framework','grammar','migration_paths','ledger_schema'}


def initialize(db):
    credentials.initialize(db)
    db.execute('CREATE TABLE IF NOT EXISTS migration_binding_definitions(id TEXT PRIMARY KEY,document TEXT NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS migration_bindings(id TEXT PRIMARY KEY,project TEXT NOT NULL,environment TEXT NOT NULL,revision INTEGER NOT NULL,enabled INTEGER NOT NULL,definition_id TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(project,environment))')
    for action in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS migration_definition_immutable_{action.lower()} BEFORE {action} ON migration_binding_definitions BEGIN SELECT RAISE(ABORT,'Migration definitions are immutable'); END")


def _definition(values,project,environment):
    if not isinstance(values,dict) or set(values)!=FIELDS:raise BindingError('Migration configuration fields are invalid.')
    # Reuse endpoint/reference validation without borrowing inspection authority.
    common._definition({k:values[k] for k in ('engine','adapter_version','ownership','database_name','database_port','secret_id','secret_revision')}|{'allowed_schemas':['public'],'policy_revision':1},project,environment)
    if values['engine']!='postgresql' or type(values['adapter_version']) is not int or values['adapter_version']!=1 or values['ledger_schema']!='public':raise BindingError('Migration adapter or ledger schema is unsupported.')
    if values['framework'] not in ('alembic','laravel') or values['grammar']!=(GRAMMAR if values['framework']=='laravel' else None):raise BindingError('An explicit supported migration grammar is required.')
    paths=values['migration_paths']
    if (not isinstance(paths,list) or not 1<=len(paths)<=8 or any(not isinstance(p,str) or len(p)>240 or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*',p) or any(part in ('.','..','.git') for part in p.split('/')) for p in paths)
            or len(set(paths))!=len(paths) or any(a.startswith(b+'/') for a in paths for b in paths if a!=b)):
        raise BindingError('Migration directories must be distinct bounded relative paths.')
    return json.loads(registry.canonical({'schema_version':1,'project':project,'environment':environment,**values}))


def _row(db,project,environment,identifier):
    row=db.execute('SELECT * FROM migration_bindings WHERE id=? AND project=? AND environment=?',(common._id(identifier),project,environment)).fetchone()
    if row is None:raise BindingError('Migration configuration is unavailable in this environment.')
    return row


def _public(db,row):
    saved=db.execute('SELECT document,digest FROM migration_binding_definitions WHERE id=?',(row['definition_id'],)).fetchone()
    try:
        definition=json.loads(saved['document'])
        if common._digest(definition)!=saved['digest'] or definition!=_definition({k:v for k,v in definition.items() if k not in ('schema_version','project','environment')},row['project'],row['environment']):raise ValueError()
    except (ValueError,TypeError,KeyError):raise BindingError('Migration configuration integrity is invalid.') from None
    secret=db.execute('SELECT enabled,revision FROM migration_credentials WHERE id=? AND project=? AND environment=?',(definition['secret_id'],row['project'],row['environment'])).fetchone()
    status='configured' if secret and secret['enabled'] and secret['revision']==definition['secret_revision'] else 'credential-stale'
    if not row['enabled']:status='disabled'
    return {k:row[k] for k in ('id','project','environment','revision','definition_id','created','updated')}|{'enabled':bool(row['enabled']),'definition':definition,'definition_digest':saved['digest'],'status':status,'verified':False}


def save_binding(db,principal,project,environment,values,*,expected_revision,binding_id=None,revalidate=None,request_id=None):
    common._authorize(db,principal,project,environment,'databases:manage',revalidate);initialize(db)
    context=common._request_context(principal,project,environment,'migration-binding-save',request_id)
    payload={'values':values,'expected_revision':expected_revision,'binding_id':binding_id}
    def perform():
        common._revision(expected_revision);definition=_definition(values,project,environment)
        prior=_row(db,project,environment,binding_id) if binding_id is not None else None
        if expected_revision!=(prior['revision'] if prior else 0):raise BindingError('Migration configuration revision changed.')
        if prior is None and db.execute('SELECT 1 FROM migration_bindings WHERE project=? AND environment=?',(project,environment)).fetchone():raise BindingError('Update the existing environment migration configuration.')
        secret=credentials._row(db,project,environment,definition['secret_id'])
        if not secret['enabled'] or secret['revision']!=definition['secret_revision']:raise BindingError('Migration credential is disabled or changed.')
        identifier=prior['id'] if prior else uuid.uuid4().hex;did=uuid.uuid4().hex;now=time.time()
        db.execute('INSERT INTO migration_binding_definitions VALUES(?,?,?,?)',(did,registry.canonical(definition),common._digest(definition),now))
        if prior:db.execute('UPDATE migration_bindings SET revision=?,enabled=1,definition_id=?,updated=? WHERE id=?',(expected_revision+1,did,now,identifier))
        else:db.execute('INSERT INTO migration_bindings VALUES(?,?,?,?,?,?,?,?)',(identifier,project,environment,1,1,did,now,now))
        common._event(db,principal,project,environment,identifier,expected_revision+1,'migration-binding-saved')
        return _public(db,_row(db,project,environment,identifier))
    return common._save(db,principal,project,environment,context,payload,perform,revalidate=revalidate)


def get_binding(db,principal,project,environment,binding_id,*,revalidate=None):
    common._authorize(db,principal,project,environment,'databases:manage',revalidate);initialize(db)
    return _public(db,_row(db,project,environment,binding_id))


def list_bindings(db,principal,project,environment,*,revalidate=None):
    common._authorize(db,principal,project,environment,'databases:manage',revalidate);initialize(db)
    return [_public(db,r) for r in db.execute('SELECT * FROM migration_bindings WHERE project=? AND environment=?',(project,environment))]


def disable_binding(db,principal,project,environment,binding_id,*,expected_revision,revalidate=None):
    common._authorize(db,principal,project,environment,'databases:manage',revalidate);initialize(db);common._revision(expected_revision)
    with registry.transaction(db):
        common._authorize(db,principal,project,environment,'databases:manage',revalidate)
        row=_row(db,project,environment,binding_id)
        if row['revision']!=expected_revision:raise BindingError('Migration configuration revision changed.')
        db.execute('UPDATE migration_bindings SET revision=revision+1,enabled=0,updated=? WHERE id=?',(time.time(),binding_id))
        common._event(db,principal,project,environment,binding_id,expected_revision+1,'migration-binding-disabled')
        return _public(db,_row(db,project,environment,binding_id))


def capture(db,project,environment):
    initialize(db)
    row=db.execute('SELECT * FROM migration_bindings WHERE project=? AND environment=? AND enabled=1',(project,environment)).fetchone()
    if row is None:return None
    saved=_public(db,row)
    if saved['status']!='configured':raise BindingError('Migration credential is disabled or changed.')
    registered=registry.get_project(db,project);env=registry.get_environment(db,project,environment)
    return {k:saved[k] for k in ('id','project','environment','revision','definition_digest','definition')}|{'project_revision':registered['revision'],'environment_revision':env['revision']}


def validate_capture(db,captured):
    if not isinstance(captured,dict):raise BindingError('Migration capture is invalid.')
    try:current=capture(db,captured['project'],captured['environment'])
    except (KeyError,TypeError):raise BindingError('Migration capture is invalid.') from None
    if current!=captured:raise BindingError('Migration configuration or environment changed after selection.')
    return current
