"""Scoped, immutable domain intent. No DNS, Docker, origin, or certificate effects."""
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import time
import uuid

import application_operations as ops
import build_profiles
import integrations
import managed_services as services
import registry
import resource_policy

FIELDS={'service_id','hostname','zone_id','dns_type','target','proxied','ttl'}
PINS={'preview_fingerprint','expected_environment_revision','expected_source_revision','expected_integration_revision','expected_origin_revision','expected_policy_revision','expected_project_revision'}

def _digest(value):return hashlib.sha256(registry.canonical(value).encode()).hexdigest()
def _error(code,message,status=409):return ops.OperationError(code,message,status)

def initialize(db):
    services.initialize(db);integrations.initialize(db);build_profiles.initialize(db)
    db.execute('CREATE TABLE IF NOT EXISTS ingress_definitions(id TEXT PRIMARY KEY,document TEXT NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS ingress_routes(id TEXT PRIMARY KEY,project TEXT NOT NULL,environment TEXT NOT NULL,hostname TEXT NOT NULL UNIQUE,definition_id TEXT NOT NULL,revision INTEGER NOT NULL,status TEXT NOT NULL,created REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS ingress_operations(id TEXT PRIMARY KEY,subject TEXT NOT NULL,credential_id TEXT,request_key TEXT NOT NULL,fingerprint TEXT NOT NULL,route_id TEXT NOT NULL,status TEXT NOT NULL,result TEXT NOT NULL,created REAL NOT NULL,UNIQUE(subject,request_key))')
    for action in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS ingress_definition_{action.lower()} BEFORE {action} ON ingress_definitions BEGIN SELECT RAISE(ABORT,'Ingress definitions are immutable'); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS ingress_definition_replace BEFORE INSERT ON ingress_definitions WHEN EXISTS(SELECT 1 FROM ingress_definitions WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT,'Ingress definitions are immutable'); END")

def _hostname(value):
    if not isinstance(value,str):raise _error('invalid_hostname','Enter an ASCII hostname.',400)
    value=value.lower().rstrip('.') if value.endswith('.') and not value.endswith('..') else value.lower()
    if not 1<=len(value)<=253 or '.' not in value or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',part) for part in value.split('.')):
        raise _error('invalid_hostname','Enter a hostname without a URL, port, wildcard, or path.',400)
    try:ipaddress.ip_address(value)
    except ValueError:return value
    raise _error('invalid_hostname','A DNS hostname is required.',400)

def _origin():
    targets={}
    for kind,name,version in (('A','INGRESS_ORIGIN_IPV4',4),('AAAA','INGRESS_ORIGIN_IPV6',6)):
        raw=os.environ.get(name)
        if raw:
            try:
                address=ipaddress.ip_address(raw)
                if address.version!=version or address.is_unspecified or address.is_multicast or address.is_loopback or address.is_link_local:raise ValueError()
                targets[kind]=str(address)
            except ValueError:raise _error('origin_unavailable','Configured host origin identity is invalid.') from None
    if os.environ.get('INGRESS_ORIGIN_HOSTNAME'):targets['CNAME']=_hostname(os.environ['INGRESS_ORIGIN_HOSTNAME'])
    if not targets:raise _error('origin_unavailable','Configure the trusted host origin identity before creating domains.')
    return targets,_digest(targets)

def _values(values,*,accepting=False):
    if not isinstance(values,dict) or set(values)!=(FIELDS|PINS if accepting else FIELDS):raise _error('invalid_input','Only structured domain settings and required preview evidence are accepted.',400)
    if accepting:
        for key in ('expected_project_revision','expected_environment_revision','expected_policy_revision','expected_integration_revision','expected_source_revision'):
            value=values[key]
            if key=='expected_source_revision' and value is None:continue
            if type(value) is not int or value<0:raise _error('invalid_preview','Preview revisions must be integers.',400)
        for key in ('preview_fingerprint','expected_origin_revision'):
            if not isinstance(values[key],str) or not re.fullmatch('[a-f0-9]{64}',values[key]):raise _error('invalid_preview','Preview identity must be a digest.',400)
    result={key:values[key] for key in FIELDS};result['hostname']=_hostname(result['hostname'])
    if not isinstance(result['service_id'],str) or not 1<=len(result['service_id'])<=100 or not isinstance(result['zone_id'],str) or not re.fullmatch('[a-f0-9]{32}',result['zone_id']):raise _error('invalid_input','Choose a managed service and verified DNS zone.',400)
    if result['dns_type'] not in ('A','AAAA','CNAME') or type(result['proxied']) is not bool or type(result['ttl']) is not int or result['ttl']!=1 and not 60<=result['ttl']<=86400 or result['proxied'] and result['ttl']!=1:raise _error('invalid_dns','Choose a supported DNS type and TTL; proxied DNS uses automatic TTL.',400)
    if not isinstance(result['target'],str):raise _error('invalid_origin','Choose the configured host origin.',400)
    try:
        result['target']=_hostname(result['target']) if result['dns_type']=='CNAME' else str(ipaddress.ip_address(result['target']))
    except ValueError:raise _error('invalid_origin','Choose the configured host origin.',400) from None
    return result

def _authorize(db,principal,scope,project,environment):
    record=ops._service_authorize(db,principal,scope,project,environment);ops.recheck_credential(db,principal)
    return record

def _service_evidence(db,principal,project,environment,service_id):
    service=services.get(db,service_id);definition=service['definition'];binding=service.get('binding')
    if service['project']!=project or service['environment']!=environment:raise _error('service_unavailable','Choose a service in this environment.')
    if service['state']!='running' or not binding or binding.get('state')!='running' or not re.fullmatch('[a-f0-9]{64}',str(binding.get('container_id',''))) or binding.get('definition_digest')!=services.digest(definition) or binding.get('image_id')!=definition['artifact']['image'] or type(binding.get('observed')) not in (int,float) or not 0<binding['observed']<=time.time():
        raise _error('service_unverified','The application must have a recorded verified running binding. Domain setup never starts it.')
    artifact=services.artifact_for_service(db,project,definition['artifact_id'])
    if artifact!=definition['artifact'] or 'http_endpoint' not in artifact:raise _error('endpoint_unavailable','Choose an application with a trusted HTTP endpoint.')
    build_profiles.validate_http_endpoint(artifact['http_endpoint'])
    source_environment=artifact.get('source_environment')
    if principal.allowed_environments is not None and source_environment in registry.ENVIRONMENTS and (project,source_environment) not in principal.allowed_environments:raise _error('forbidden','Source environment is not permitted.',403)
    return service,artifact

def _definition(db,principal,project,environment,values,scope):
    project_row=_authorize(db,principal,scope,project,environment)
    env=services.ensure_environment_eligible(db,project,environment)
    service,artifact=_service_evidence(db,principal,project,environment,values['service_id'])
    definition=service['definition'];binding=service['binding'];source_environment=artifact.get('source_environment')
    source=build_profiles.get_private(db,project,environment)
    policy=resource_policy.read(db,project)
    connection=db.execute("SELECT revision,enabled,metadata FROM integration_connections WHERE provider='cloudflare'").fetchone()
    if not connection or not connection['enabled']:raise _error('integration_unavailable','Enable and verify the Cloudflare integration.')
    metadata=json.loads(connection['metadata'])
    if not isinstance(metadata,dict) or not isinstance(metadata.get('zones'),list) or any(not isinstance(zone,dict) for zone in metadata['zones']):raise _error('integration_unavailable','Verified zone metadata is unavailable.')
    if metadata.get('api_access')!='verified' or metadata.get('zone_read')!='verified':raise _error('integration_unavailable','Verify Cloudflare zone access first.')
    if metadata.get('token_kind') not in ('user','account'):raise _error('integration_unavailable','Verified token identity is unavailable.')
    zones=[zone for zone in metadata.get('zones',[]) if zone.get('id')==values['zone_id']]
    if len(zones)!=1 or zones[0].get('status')!='active' or not re.fullmatch('[a-f0-9]{32}',str(zones[0].get('account_id',''))):raise _error('zone_unavailable','Choose one active verified zone with a known account.')
    zone=zones[0];zone_name=_hostname(zone['name'])
    if metadata.get('token_kind')=='account' and metadata.get('account_id')!=zone['account_id']:raise _error('zone_unavailable','The zone belongs to another integration account.')
    if values['hostname']!=zone_name and not values['hostname'].endswith('.'+zone_name):raise _error('invalid_hostname','The hostname must belong to the selected zone.')
    origins,origin_revision=_origin()
    if origins.get(values['dns_type'])!=values['target'] or values['dns_type']=='CNAME' and values['target']==values['hostname']:raise _error('invalid_origin','DNS target must match the configured host origin identity.')
    return {'schema_version':1,'project':project,'environment':environment,**values,'service_definition_id':service['definition_id'],'service_definition_digest':services.digest(definition),
            'container_id':binding['container_id'],'binding_digest':_digest(binding),'artifact_id':definition['artifact_id'],'artifact_digest':services.digest(artifact),'source_environment':source_environment,'http_endpoint':artifact['http_endpoint'],
            'account_id':zone['account_id'],'zone_name':zone_name,'integration_revision':connection['revision'],'integration_observation_digest':_digest(metadata),
            'origin_revision':origin_revision,'project_revision':project_row['revision'],'environment_revision':env['revision'],'source_revision':source['source_revision'] if source else None,'policy_revision':policy['revision']}

def preview(db,principal,project,environment,values):
    _authorize(db,principal,'domains:read',project,environment);initialize(db)
    settings=_values(values);definition=_definition(db,principal,project,environment,settings,'domains:read')
    collision=db.execute('SELECT 1 FROM ingress_routes WHERE hostname=?',(settings['hostname'],)).fetchone()
    if collision:raise _error('hostname_reserved','This hostname is already reserved by a managed domain.')
    pins={'expected_'+key:definition[key] for key in ('project_revision','environment_revision','source_revision','policy_revision','integration_revision','origin_revision')}
    return {'definition':definition,'preview_fingerprint':_digest(definition),**pins,'dns_live_checked':False,'provider_ownership':'unchecked',**executor_status(db),'runtime_verification':'pending','status':'preview','dns':'unverified','origin':'unverified','tls':'unverified','ready':False}

def execution_enabled():
    return os.environ.get('INGRESS_ENABLED') == '1'


def executor_status(db):
    """Bounded read of worker evidence; availability is not route readiness."""
    enabled=execution_enabled()
    executor={'enabled':enabled,'configured':False,'state':'disabled' if not enabled else 'unobserved','observed_at':None,'stale':True}
    row=db.execute("SELECT updated,value FROM snapshots WHERE name='ingress-worker'").fetchone() if db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshots' AND type='table'").fetchone() else None
    if row:
        try:
            if not isinstance(row['value'],str) or len(row['value'])>4096:raise ValueError()
            value=json.loads(row['value']);observed=row['updated']
            if not isinstance(value,dict) or type(value.get('configured')) is not bool or value.get('state') not in ('disabled','available','unavailable','blocked') or type(observed) not in (int,float) or not 0<observed<=time.time():raise ValueError()
            stale=time.time()-observed>90
            executor.update(configured=value['configured'],observed_at=observed,stale=stale,
                            state='disabled' if not enabled else 'stale' if stale else value['state'])
        except (ValueError,TypeError):
            executor['state']='disabled' if not enabled else 'invalid-observation'
    return {'executor_available':enabled and executor['configured'] and executor['state']=='available' and not executor['stale'],'executor':executor}


def _require_executor(db):
    if not executor_status(db)['executor_available']:raise _error('executor_unavailable','The domain worker has no recent available observation. Existing operations remain available for review and recovery.')


def accept(db,principal,project,environment,values,idempotency_key,*,enqueue_execution=False):
    _authorize(db,principal,'domains:create',project,environment);settings=_values(values,accepting=True)
    if not isinstance(idempotency_key,str) or not re.fullmatch('[A-Za-z0-9-]{16,100}',idempotency_key):raise _error('invalid_request_id','Use a stable domain request ID.',400)
    initialize(db);fingerprint=_digest({'project':project,'environment':environment,'values':dict(values,**settings)})
    if enqueue_execution:
        import domain_execution
        domain_execution.initialize(db)
    with registry.transaction(db):
        _authorize(db,principal,'domains:create',project,environment)
        replay=db.execute('SELECT fingerprint,result,route_id FROM ingress_operations WHERE subject=? AND request_key=?',(principal.subject_id,idempotency_key)).fetchone()
        if replay:
            if replay['fingerprint']!=fingerprint:raise _error('idempotency_conflict','This request ID belongs to different domain settings.')
            evidence=db.execute('SELECT d.document,d.digest FROM ingress_routes r JOIN ingress_definitions d ON d.id=r.definition_id WHERE r.id=?',(replay['route_id'],)).fetchone()
            if not evidence:raise _error('route_unavailable','Accepted domain evidence is unavailable.')
            accepted=json.loads(evidence['document'])
            if _digest(accepted)!=evidence['digest']:raise _error('route_unavailable','Accepted domain evidence changed.')
            source_environment=accepted.get('source_environment')
            if principal.allowed_environments is not None and source_environment in registry.ENVIRONMENTS and (project,source_environment) not in principal.allowed_environments:raise _error('forbidden','Source environment is not permitted.',403)
            result=json.loads(replay['result'])
            if enqueue_execution:
                if not db.execute('SELECT 1 FROM domain_executions WHERE operation_id=?',(result['operation']['id'],)).fetchone():_require_executor(db)
                domain_execution.enqueue(db,result['operation']['id'])
                result['operation']=domain_execution.public_status(db,result['operation']['id'])
            return result
        if enqueue_execution:_require_executor(db)
        definition=_definition(db,principal,project,environment,settings,'domains:create')
        if values['preview_fingerprint']!=_digest(definition) or any(values['expected_'+key]!=definition[key] for key in ('project_revision','environment_revision','source_revision','policy_revision','integration_revision','origin_revision')):raise _error('stale_preview','Domain evidence changed. Refresh the preview before accepting.')
        route_id=uuid.uuid4().hex;definition_id=uuid.uuid4().hex;operation_id=uuid.uuid4().hex;now=time.time()
        result={'route':{'id':route_id,'project':project,'environment':environment,'hostname':settings['hostname'],'definition_id':definition_id,'revision':1,'status':'accepted','ready':False},'operation':{'id':operation_id,'status':'pending-provisioning','executor_available':False,'runtime_verification':'pending','dns':'unverified','origin':'unverified','tls':'unverified'}}
        try:
            db.execute('INSERT INTO ingress_routes VALUES(?,?,?,?,?,?,?,?)',(route_id,project,environment,settings['hostname'],definition_id,1,'accepted',now))
        except sqlite3.IntegrityError:raise _error('hostname_reserved','This hostname is already reserved by a managed domain.') from None
        db.execute('INSERT INTO ingress_definitions VALUES(?,?,?,?)',(definition_id,registry.canonical(definition),_digest(definition),now))
        db.execute('INSERT INTO ingress_operations VALUES(?,?,?,?,?,?,?,?,?)',(operation_id,principal.subject_id,principal.credential_id,idempotency_key,fingerprint,route_id,'pending-provisioning',registry.canonical(result),now))
        if enqueue_execution:
            domain_execution.enqueue(db,operation_id)
            result['operation']=domain_execution.public_status(db,operation_id)
        return result


def domain_options(db,principal,project,environment):
    """Scoped public discovery; cached evidence is explicitly distinct from live checks."""
    project_row=_authorize(db,principal,'domains:read',project,environment);initialize(db)
    env=services.ensure_environment_eligible(db,project,environment)
    source=build_profiles.get_private(db,project,environment);policy=resource_policy.read(db,project)
    choices=[]
    for item in services.list_services(db,project,environment):
        try:service,artifact=_service_evidence(db,principal,project,environment,item['id'])
        except ops.OperationError as error:
            if error.code in ('forbidden','service_unavailable','service_unverified','endpoint_unavailable'):continue
            raise
        choices.append({'id':service['id'],'name':service['definition']['name'],'definition_id':service['definition_id'],
                        'http_endpoint':dict(artifact['http_endpoint']),'state':'running','evidence':'recorded','runtime_verification':'pending'})
    zones=[];connection=db.execute("SELECT revision,enabled,metadata FROM integration_connections WHERE provider='cloudflare'").fetchone()
    integration_state='unavailable'
    if connection and connection['enabled']:
        try:metadata=json.loads(connection['metadata'])
        except (ValueError,TypeError):metadata=None
        if (isinstance(metadata,dict) and metadata.get('api_access')=='verified' and metadata.get('zone_read')=='verified'
                and metadata.get('token_kind') in ('user','account') and isinstance(metadata.get('zones'),list)):
            counts={}
            for zone in metadata['zones']:
                if isinstance(zone,dict) and isinstance(zone.get('id'),str):counts[zone['id']]=counts.get(zone['id'],0)+1
            for zone in metadata['zones']:
                if (not isinstance(zone,dict) or not isinstance(zone.get('id'),str) or not re.fullmatch('[a-f0-9]{32}',zone['id']) or counts.get(zone['id'])!=1
                        or zone.get('status')!='active' or not re.fullmatch('[a-f0-9]{32}',str(zone.get('account_id','')))
                        or metadata['token_kind']=='account' and zone['account_id']!=metadata.get('account_id')):continue
                try:name=_hostname(zone.get('name'))
                except ops.OperationError:continue
                zones.append({'id':zone['id'],'name':name,'account_id':zone['account_id']})
            integration_state='verified' if zones else 'no-eligible-zones'
    try:origins,origin_revision=_origin();origin_state='configured'
    except ops.OperationError:origins={};origin_revision=None;origin_state='unavailable'
    _authorize(db,principal,'domains:read',project,environment)
    return {'project':project,'environment':environment,'services':choices,'zones':zones,
            'origins':[{'dns_type':kind,'target':target} for kind,target in sorted(origins.items())],
            'project_revision':project_row['revision'],'environment_revision':env['revision'],'source_revision':source['source_revision'] if source else None,
            'policy_revision':policy['revision'],'integration_revision':connection['revision'] if connection else 0,'origin_revision':origin_revision,
            'integration_status':integration_state,'origin_status':origin_state,'dns_live_checked':False,**executor_status(db),
            'defaults':{'proxied':True,'ttl':1},'ready':False}


def _route_document(db,principal,project,environment,route_id):
    row=db.execute('SELECT r.*,d.document,d.digest FROM ingress_routes r LEFT JOIN ingress_definitions d ON d.id=r.definition_id WHERE r.id=? AND r.project=? AND r.environment=?',(route_id,project,environment)).fetchone()
    if row is None:raise _error('route_unavailable','Domain is unavailable in this environment.',404)
    try:
        definition=json.loads(row['document'])
        if (_digest(definition)!=row['digest'] or any(definition[k]!=row[k] for k in ('project','environment','hostname'))
                or type(row['revision']) is not int or row['revision']<1):raise ValueError()
        normalized=_values({key:definition[key] for key in FIELDS})
        if any(normalized[key]!=definition[key] for key in FIELDS):raise ValueError()
        build_profiles.validate_http_endpoint(definition['http_endpoint'])
    except (ValueError,TypeError,KeyError):raise _error('route_integrity','Stored domain identity is inconsistent.') from None
    source=definition.get('source_environment')
    if principal.allowed_environments is not None and source in registry.ENVIRONMENTS and (project,source) not in principal.allowed_environments:raise _error('forbidden','Source environment is not permitted.',403)
    return row,definition


def _dns_evidence(db,route_id,definition):
    result={'status':'unverified','evidence':'none','observed_at':None}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingress_dns_ownership'").fetchone():return result
    owner=db.execute('SELECT * FROM ingress_dns_ownership WHERE route_id=?',(route_id,)).fetchone()
    if not owner:return result
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingress_dns_intents'").fetchone():raise _error('route_integrity','Recorded DNS evidence is incomplete.')
    intent=db.execute('SELECT * FROM ingress_dns_intents WHERE operation_id=? AND route_id=?',(owner['operation_id'],route_id)).fetchone()
    if (not intent or intent['state']!='verified' or intent['record_id']!=owner['record_id'] or intent['marker']!=owner['marker']
            or owner['zone_id']!=definition['zone_id'] or not re.fullmatch('[a-f0-9]{32}',str(owner['record_id']))
            or not re.fullmatch('[a-f0-9]{64}',str(owner['record_digest'])) or type(owner['observed']) not in (int,float) or not 0<owner['observed']<=time.time()):raise _error('route_integrity','Recorded DNS ownership is inconsistent.')
    expected={'id':owner['record_id'],'name':definition['hostname'],'type':definition['dns_type'],'content':definition['target'],'ttl':definition['ttl'],'proxied':definition['proxied'],'comment':owner['marker']}
    if _digest(expected)!=owner['record_digest']:raise _error('route_integrity','Recorded DNS identity differs from the accepted domain.')
    return {'status':'verified','evidence':'recorded','observed_at':owner['observed'],'record_id':owner['record_id'],'live_checked':False}


def get_route(db,principal,project,environment,route_id):
    _authorize(db,principal,'domains:read',project,environment);initialize(db)
    row,definition=_route_document(db,principal,project,environment,route_id)
    public={key:definition[key] for key in ('service_id','hostname','zone_id','dns_type','target','proxied','ttl','http_endpoint')}
    public.update(id=row['id'],project=project,environment=environment,definition_id=row['definition_id'],revision=row['revision'],
                  status=row['status'] if row['status'] in ('accepted','active','failed','recovery-required') else 'unknown',ready=False,
                  dns=_dns_evidence(db,route_id,definition),origin={'status':'unverified','evidence':'none'},tls={'status':'unverified','evidence':'none'},
                  runtime_verification='pending',**executor_status(db))
    operation=db.execute('SELECT id FROM ingress_operations WHERE route_id=? ORDER BY created DESC LIMIT 1',(route_id,)).fetchone()
    public['operation_id']=operation['id'] if operation else None
    if operation and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='domain_executions'").fetchone() and db.execute('SELECT 1 FROM domain_executions WHERE operation_id=?',(operation['id'],)).fetchone():
        import domain_execution
        execution=domain_execution.public_status(db,operation['id'])
        public.update({key:execution[key] for key in ('status','phase','dns','origin','tls','next_action','executor_available','executor')})
    _authorize(db,principal,'domains:read',project,environment)
    return public


def list_routes(db,principal,project,environment,*,limit=25,cursor=None):
    _authorize(db,principal,'domains:read',project,environment);initialize(db)
    if type(limit) is not int or not 1<=limit<=100 or cursor is not None and (not isinstance(cursor,str) or not re.fullmatch('[a-f0-9]{32}',cursor)):raise _error('invalid_input','Choose a valid page size and domain cursor.',400)
    result=[]
    for row in db.execute('SELECT id FROM ingress_routes WHERE project=? AND environment=? AND id>? ORDER BY id',(project,environment,cursor or '')):
        try:route=get_route(db,principal,project,environment,row['id'])
        except ops.OperationError as error:
            if error.status==403:continue
            raise
        result.append(route)
        if len(result)>limit:break
    _authorize(db,principal,'domains:read',project,environment)
    return {'routes':result[:limit],'next_cursor':result[limit-1]['id'] if len(result)>limit else None}


def get_operation(db,principal,project,environment,operation_id):
    _authorize(db,principal,'domains:read',project,environment);initialize(db)
    row=db.execute('SELECT o.* FROM ingress_operations o JOIN ingress_routes r ON r.id=o.route_id WHERE o.id=? AND r.project=? AND r.environment=?',(operation_id,project,environment)).fetchone()
    if row is None:raise _error('operation_unavailable','Domain operation is unavailable in this environment.',404)
    route=get_route(db,principal,project,environment,row['route_id'])
    try:
        accepted=json.loads(row['result']);record=accepted['route']
        if accepted['operation']['id']!=row['id'] or any(record[k]!=route[k] for k in ('id','project','environment','hostname','definition_id')):raise ValueError()
    except (ValueError,TypeError,KeyError):raise _error('route_integrity','Stored domain operation identity is inconsistent.') from None
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='domain_executions'").fetchone() and db.execute('SELECT 1 FROM domain_executions WHERE operation_id=?',(operation_id,)).fetchone():
        import domain_execution
        execution=domain_execution.public_status(db,operation_id)
        if execution is not None:return execution
    return {'id':row['id'],'route_id':row['route_id'],'project':project,'environment':environment,
            'status':row['status'] if row['status'] in ('pending-provisioning','running','failed','recovery-required','succeeded') else 'unknown',
            'dns':route['dns'],'origin':route['origin'],'tls':route['tls'],'ready':False,**executor_status(db),'runtime_verification':'pending'}
