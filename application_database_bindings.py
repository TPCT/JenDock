"""Immutable application DB attachment intent; no Docker or network effects.

Only an explicitly accepted historical pin may resolve during lifecycle recovery.
Public projections never contain decrypted credentials or generated environment.
"""
import json
import re
import time
import uuid
from urllib.parse import quote
import adopted_workloads
import application_credentials as credentials
import database_bindings as common
import managed_services
import registry

BindingError=common.BindingError
FIELDS={'service_id','ownership','engine','database_name','database_port','secret_id','secret_revision','environment_mapping'}


def initialize(db):
    credentials.initialize(db)
    db.execute('CREATE TABLE IF NOT EXISTS application_database_bindings(id TEXT PRIMARY KEY,project TEXT NOT NULL,environment TEXT NOT NULL,service_id TEXT NOT NULL,revision INTEGER NOT NULL,enabled INTEGER NOT NULL,minimum_revision INTEGER NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(project,environment,service_id))')
    db.execute('CREATE TABLE IF NOT EXISTS application_database_definitions(binding_id TEXT NOT NULL,revision INTEGER NOT NULL,document TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(binding_id,revision))')
    for action in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS app_db_definition_{action.lower()} BEFORE {action} ON application_database_definitions BEGIN SELECT RAISE(ABORT,'Application DB definitions are immutable'); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS app_db_definition_replace BEFORE INSERT ON application_database_definitions WHEN EXISTS(SELECT 1 FROM application_database_definitions WHERE binding_id=NEW.binding_id AND revision=NEW.revision) BEGIN SELECT RAISE(ABORT,'Application DB definitions are immutable'); END")


def _service(db,project,environment,service_id):
    service=managed_services.get(db,service_id)
    if service['project']!=project or service['environment']!=environment or service['definition'].get('service_id')!=service_id or service['definition'].get('project')!=project or service['definition'].get('environment')!=environment:
        raise BindingError('Application service belongs to another environment.')
    return service


def _definition(values,project,environment):
    if not isinstance(values,dict) or set(values)!=FIELDS:raise BindingError('Application database attachment fields are invalid.')
    common._definition({k:values[k] for k in ('ownership','engine','database_name','database_port','secret_id','secret_revision')}|{'adapter_version':1,'allowed_schemas':['public'],'policy_revision':1},project,environment)
    if values['engine']!='postgresql' or values['ownership']['kind']!='adopted-compose':raise BindingError('A retained PostgreSQL database is required.')
    try:
        if str(uuid.UUID(values['service_id']))!=values['service_id']:raise ValueError()
    except (ValueError,TypeError,AttributeError):raise BindingError('An exact managed service is required.') from None
    mapping=values['environment_mapping']
    if (not isinstance(mapping,dict) or set(mapping) not in ({'host','port','database','username','password'},{'url'})
            or any(not isinstance(v,str) or not re.fullmatch('[A-Z][A-Z0-9_]{0,63}',v) for v in mapping.values())
            or len(set(mapping.values()))!=len(mapping)):
        raise BindingError('Choose distinct environment names for all connection fields or a single URL.')
    # Interpreter/loader variables must not become a credential injection channel.
    if any(v in {'PATH','HOME','SHELL','ENV','BASH_ENV','PYTHONPATH','PYTHONHOME','NODE_OPTIONS','JAVA_TOOL_OPTIONS','PHP_INI_SCAN_DIR','PHPRC','PERL5OPT','RUBYOPT'} or v.startswith(('LD_','DYLD_')) for v in mapping.values()):raise BindingError('Connection environment names cannot control the runtime loader.')
    return json.loads(registry.canonical({'schema_version':1,'project':project,'environment':environment,**values}))


def _ownership(db,definition):
    project,environment=definition['project'],definition['environment'];owner=adopted_workloads.get(db,project,environment)
    if owner is None:raise BindingError('A retained adopted database owner is required.')
    ref=definition['ownership']
    if owner['recipe_revision']!=ref['revision']:raise BindingError('Database ownership revision changed.')
    evidence=adopted_workloads._evidence(db,project,environment)
    containers=[r for r in evidence['containers'] if r['service']==ref['reference']]
    if len(containers)!=1:raise BindingError('Database service ownership is ambiguous.')
    matches=[r for r in evidence['databases'] if r.get('container_id')==containers[0]['id']]
    identities={(r.get('engine'),r.get('database'),r.get('port')) for r in matches}
    if not identities or any(engine not in ('pgsql','postgres','postgresql') or name!=definition['database_name'] or port!=definition['database_port'] for engine,name,port in identities):raise BindingError('Database endpoint differs from adopted evidence.')
    return owner


def _row(db,project,environment,binding_id):
    row=db.execute('SELECT * FROM application_database_bindings WHERE id=? AND project=? AND environment=?',(common._id(binding_id),project,environment)).fetchone()
    if row is None:raise BindingError('Application database binding is unavailable in this environment.')
    return row


def _saved(db,row,revision):
    saved=db.execute('SELECT document,digest FROM application_database_definitions WHERE binding_id=? AND revision=?',(row['id'],revision)).fetchone()
    try:
        definition=json.loads(saved['document'])
        if common._digest(definition)!=saved['digest'] or definition!=_definition({k:definition[k] for k in FIELDS},row['project'],row['environment']) or definition['service_id']!=row['service_id']:raise ValueError()
        return definition,saved['digest']
    except (ValueError,KeyError,TypeError):raise BindingError('Application database definition integrity is invalid.') from None


def _public(db,row):
    # Disabled heads have no new definition; expose the last accepted definition.
    rev=db.execute('SELECT MAX(revision) FROM application_database_definitions WHERE binding_id=?',(row['id'],)).fetchone()[0]
    definition,digest=_saved(db,row,rev)
    secret=credentials._row(db,row['project'],row['environment'],definition['secret_id'])
    state='configured' if secret['enabled'] and secret['revision']==definition['secret_revision'] else 'credential-stale'
    if not row['enabled']:state='disabled'
    return {k:row[k] for k in ('id','project','environment','service_id','revision','created','updated')}|{'enabled':bool(row['enabled']),'definition':definition,'definition_revision':rev,'definition_digest':digest,'alias':'appdb-'+row['id'],'status':state,'application_status':'not-observed'}


def save_binding(db,principal,project,environment,values,*,expected_revision,binding_id=None,revalidate=None,request_id=None):
    common._authorize(db,principal,project,environment,'databases:manage',revalidate);initialize(db)
    context=common._request_context(principal,project,environment,'application-database-binding-save',request_id)
    payload={'values':values,'expected_revision':expected_revision,'binding_id':binding_id}
    def perform():
        common._revision(expected_revision);definition=_definition(values,project,environment)
        _service(db,project,environment,definition['service_id']);_ownership(db,definition)
        prior=_row(db,project,environment,binding_id) if binding_id is not None else None
        if expected_revision!=(prior['revision'] if prior else 0) or (prior and prior['service_id']!=definition['service_id']):raise BindingError('Application binding revision or service changed.')
        if prior:
            previous,_=_saved(db,prior,db.execute('SELECT MAX(revision) FROM application_database_definitions WHERE binding_id=?',(prior['id'],)).fetchone()[0])
            if any(previous[k]!=definition[k] for k in ('service_id','ownership','engine','database_name','database_port')):raise BindingError('Database topology changes require an explicit transition.')
        if prior is None and db.execute('SELECT 1 FROM application_database_bindings WHERE project=? AND environment=? AND service_id=?',(project,environment,definition['service_id'])).fetchone():raise BindingError('Update the existing service database binding.')
        secret=credentials._row(db,project,environment,definition['secret_id'])
        if not secret['enabled'] or secret['revision']!=definition['secret_revision']:raise BindingError('Select the current enabled application credential.')
        identifier=prior['id'] if prior else uuid.uuid4().hex;revision=expected_revision+1;now=time.time()
        if prior:db.execute('UPDATE application_database_bindings SET revision=?,enabled=1,updated=? WHERE id=?',(revision,now,identifier))
        else:db.execute('INSERT INTO application_database_bindings VALUES(?,?,?,?,?,?,?,?,?)',(identifier,project,environment,definition['service_id'],revision,1,1,now,now))
        db.execute('INSERT INTO application_database_definitions VALUES(?,?,?,?)',(identifier,revision,registry.canonical(definition),common._digest(definition)))
        common._event(db,principal,project,environment,identifier,revision,'application-database-binding-saved')
        return _public(db,_row(db,project,environment,identifier))
    return common._save(db,principal,project,environment,context,payload,perform,revalidate=revalidate)


def get_binding(db,principal,project,environment,binding_id,*,revalidate=None):
    common._authorize(db,principal,project,environment,'databases:manage',revalidate);initialize(db)
    return _public(db,_row(db,project,environment,binding_id))


def list_bindings(db,principal,project,environment,*,revalidate=None,limit=25,cursor=None):
    common._authorize(db,principal,project,environment,'databases:manage',revalidate);initialize(db)
    limit,cursor=common._page(limit,cursor)
    return [_public(db,row) for row in db.execute('SELECT * FROM application_database_bindings WHERE project=? AND environment=? AND id>? ORDER BY id LIMIT ?',(project,environment,cursor,limit))]


def disable_binding(db,principal,project,environment,binding_id,*,expected_revision,revalidate=None):
    common._authorize(db,principal,project,environment,'databases:manage',revalidate);initialize(db);common._revision(expected_revision)
    with registry.transaction(db):
        common._authorize(db,principal,project,environment,'databases:manage',revalidate)
        row=_row(db,project,environment,binding_id)
        if row['revision']!=expected_revision:raise BindingError('Application binding revision changed.')
        db.execute('UPDATE application_database_bindings SET minimum_revision=revision+1,revision=revision+1,enabled=0,updated=? WHERE id=?',(time.time(),binding_id))
        common._event(db,principal,project,environment,binding_id,expected_revision+1,'application-database-binding-disabled')
        return _public(db,_row(db,project,environment,binding_id))


def _resolve(db,project,environment,service_id,pin,*,historical,stopping=False):
    initialize(db)
    if not isinstance(pin,dict) or set(pin)!={'binding_id','revision','digest'}:raise BindingError('An exact application database pin is required.')
    common._revision(pin['revision'])
    row=_row(db,project,environment,pin['binding_id'])
    if row['service_id']!=service_id or (not stopping and (not row['enabled'] or not row['minimum_revision']<=pin['revision']<=row['revision'] or (not historical and row['revision']!=pin['revision']))):raise BindingError('Application database binding is disabled or changed.')
    definition,digest=_saved(db,row,pin['revision'])
    if pin['digest']!=digest:raise BindingError('Application database definition changed.')
    _service(db,project,environment,service_id);owner=_ownership(db,definition)
    secret=credentials._row(db,project,environment,definition['secret_id'])
    if not stopping and (not secret['enabled'] or not secret['minimum_revision']<=definition['secret_revision']<=secret['revision'] or (not historical and secret['revision']!=definition['secret_revision'])):raise BindingError('Application credential is disabled or changed.')
    topology={'binding_id':row['id'],'project':project,'environment':environment,'service_id':service_id,**{k:definition[k] for k in ('ownership','engine','database_name','database_port')},'ownership_digest':owner['recipe_digest'],'adoption_fingerprint':owner['adoption_fingerprint']}
    return {'topology':topology,'topology_digest':common._digest(topology),'pin':dict(pin),'project':project,'environment':environment,'service_id':service_id,'definition':definition,'alias':'appdb-'+row['id'],'ownership_digest':owner['recipe_digest'],'adoption_fingerprint':owner['adoption_fingerprint']}


def resolve_selection(db,project,environment,service_id,pin):
    """Trusted acceptance caller must already authorize service/environment."""
    return _resolve(db,project,environment,service_id,pin,historical=False)


def resolve_runtime(db,project,environment,service_id,pin,*,authorize):
    if not callable(authorize):raise BindingError('Current application runtime authority is required.')
    authorize();result=_resolve(db,project,environment,service_id,pin,historical=True)
    authorize()
    if _resolve(db,project,environment,service_id,pin,historical=True)!=result:raise BindingError('Application database authority changed.')
    return result


def runtime_environment(db,project,environment,service_id,pin,key_path,*,authorize):
    """Private values only; never return this from HTTP/MCP or persist in recipes."""
    metadata=resolve_runtime(db,project,environment,service_id,pin,authorize=authorize);definition=metadata['definition']
    secret=credentials.credential_for_runtime(db,project,environment,definition['secret_id'],definition['secret_revision'],key_path,authorize=authorize)
    if resolve_runtime(db,project,environment,service_id,pin,authorize=authorize)!=metadata:raise BindingError('Application database authority changed.')
    mapping=definition['environment_mapping']
    values={'host':metadata['alias'],'port':str(definition['database_port']),'database':definition['database_name'],**secret}
    if 'url' in mapping:return {mapping['url']:'postgresql://'+quote(secret['username'],safe='')+':'+quote(secret['password'],safe='')+'@'+metadata['alias']+':'+str(definition['database_port'])+'/'+quote(definition['database_name'],safe='')}
    return {name:values[field] for field,name in mapping.items()}


def recorded_environment_keys(db,project,environment,service_id,pin):
    """Non-secret keys from an already bound immutable definition.

    The authorized container-details caller supplies its registered service pin.
    Disabled credentials/bindings do not erase values already in that container;
    this metadata grants no selection, decryption or runtime authority.
    """
    initialize(db)
    if not isinstance(pin,dict) or set(pin)!={'binding_id','revision','digest'}:
        raise BindingError('An exact recorded application database pin is required.')
    common._revision(pin['revision'])
    row=_row(db,project,environment,pin['binding_id'])
    if row['service_id']!=service_id:raise BindingError('Recorded application database service changed.')
    definition,digest=_saved(db,row,pin['revision'])
    if digest!=pin['digest']:raise BindingError('Recorded application database definition changed.')
    return sorted(definition['environment_mapping'].values())


def resolve_stopping(db,project,environment,service_id,pin,*,authorize):
    """Identity-only stop authority for the currently registered accepted pin."""
    if not callable(authorize):raise BindingError('Current stop authority is required.')
    def check():
        authorize()
        service=_service(db,project,environment,service_id)
        if service['definition'].get('runtime',{}).get('database_attachment')!=pin:
            raise BindingError('Stop requires the currently registered application attachment.')
        return _resolve(db,project,environment,service_id,pin,historical=True,stopping=True)
    result=check()
    if check()!=result:raise BindingError('Application stop authority changed.')
    return result


def verify_stopping_environment(db,project,environment,service_id,pin,actual_values,key_path,*,authorize):
    """Compare only; revoked historical secrets never leave this boundary."""
    metadata=resolve_stopping(db,project,environment,service_id,pin,authorize=authorize)
    definition=metadata['definition']
    secret,_=credentials._resolve(db,project,environment,definition['secret_id'],definition['secret_revision'],key_path,historical=True,stopping=True)
    mapping=definition['environment_mapping']
    values={'host':metadata['alias'],'port':str(definition['database_port']),'database':definition['database_name'],**secret}
    if 'url' in mapping:
        expected={mapping['url']:'postgresql://'+quote(secret['username'],safe='')+':'+quote(secret['password'],safe='')+'@'+metadata['alias']+':'+str(definition['database_port'])+'/'+quote(definition['database_name'],safe='')}
    else:expected={name:values[field] for field,name in mapping.items()}
    if not isinstance(actual_values,dict) or any(actual_values.get(k)!=v for k,v in expected.items()):raise BindingError('Accepted application database environment changed.')
    if resolve_stopping(db,project,environment,service_id,pin,authorize=authorize)!=metadata:raise BindingError('Application stop authority changed.')
    return True
