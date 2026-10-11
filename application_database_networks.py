"""Journaled, exact application-to-database bridge ownership.

Caller holds worker control.LOCK for the whole operation. No SQLite transaction
spans a Docker call. Unknown command outcomes are observed, never blindly retried.
"""
import json
import os
import re
import time
import uuid
import application_database_bindings as bindings
import database_target
from database_helper import exchange
import registry

PREFIX='net.shiftcodes.application-database.'
class NetworkError(ValueError):pass


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS application_database_networks(binding_id TEXT PRIMARY KEY,topology_digest TEXT NOT NULL,document TEXT NOT NULL,updated REAL NOT NULL)')


def _command(docker,args):
    if not isinstance(docker,str) or not docker.startswith('/'):raise NetworkError('Trusted absolute Docker executable is required.')
    env={'PATH':'/usr/local/bin:/usr/bin:/bin'}
    if os.environ.get('DOCKER_HOST'):env['DOCKER_HOST']=os.environ['DOCKER_HOST']
    try:return exchange([docker,*args],b'',timeout=8,env=env).decode().strip()
    except Exception:raise NetworkError('Database network outcome is unavailable; reconcile recorded intent.') from None


def _json(docker,args):
    try:return json.loads(_command(docker,args))
    except (ValueError,UnicodeError):raise NetworkError('Database network observation is invalid.') from None


def _labels(metadata,journal):
    return {PREFIX+'binding':metadata['pin']['binding_id'],PREFIX+'topology':metadata['topology_digest'],
            PREFIX+'project':metadata['project'],PREFIX+'environment':metadata['environment'],PREFIX+'nonce':journal['nonce']}


def _name(metadata):return 'appdb-'+metadata['pin']['binding_id']


def _load(db,metadata):
    row=db.execute('SELECT * FROM application_database_networks WHERE binding_id=?',(metadata['pin']['binding_id'],)).fetchone()
    if row is None:return None
    try:
        value=json.loads(row['document'])
        if row['topology_digest']!=metadata['topology_digest'] or value['topology']!=metadata['topology'] or value['phase'] not in ('prepared','create-intent','network-created','connect-intent','verified') or not re.fullmatch('[a-f0-9]{32}',value['nonce']):raise ValueError()
        if value.get('before') and bindings.common._digest({k:v for k,v in value['before'].items() if k!='fingerprint'})!=value['before']['fingerprint']:raise ValueError()
        return value
    except (ValueError,KeyError,TypeError):raise NetworkError('Database network journal identity is invalid.') from None


def _save(db,metadata,journal,phase,authorize):
    with registry.transaction(db):
        authorize();journal['phase']=phase
        db.execute('INSERT INTO application_database_networks VALUES(?,?,?,?) ON CONFLICT(binding_id) DO UPDATE SET document=excluded.document,updated=excluded.updated',
                   (metadata['pin']['binding_id'],metadata['topology_digest'],registry.canonical(journal),time.time()))


def _authority(db,metadata,authorize):
    if not callable(authorize):raise NetworkError('Current attachment authority is required.')
    current=bindings.resolve_runtime(db,metadata['project'],metadata['environment'],metadata['service_id'],metadata['pin'],authorize=authorize)
    if current!=metadata:raise NetworkError('Database attachment authority changed.')


def _candidate(db,metadata,resolver):
    if not callable(resolver):raise NetworkError('Fresh database ownership observations are required.')
    candidate=database_target.resolve_ownership(metadata,resolve_candidate=resolver)
    database_target.check_registered(db,candidate)
    return candidate


def _migration_fence(db,metadata,candidate):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_migrations'").fetchone():return
    rows=db.execute("SELECT m.proof,t.document FROM job_migrations m LEFT JOIN job_migration_targets t ON t.job_id=m.job_id WHERE m.state IN ('planned','applying','recovery-required') LIMIT 1001").fetchall()
    if len(rows)>1000:raise NetworkError('Migration recovery inventory exceeds its bound.')
    for row in rows:
        try:
            target=json.loads(row['document']);bound=target['binding']
            if bound is None:continue
            if bound['project']!=metadata['project'] or bound['environment']!=metadata['environment']:continue
            if bound['definition']['ownership']==metadata['definition']['ownership']:
                raise NetworkError('Migration evidence must be reconciled before database network changes.')
            if row['proof'] and json.loads(row['proof'])['candidate']['container_id']==candidate['container_id']:
                raise NetworkError('Migration evidence must be reconciled before database network changes.')
        except NetworkError:raise
        except (ValueError,KeyError,TypeError):raise NetworkError('Migration ownership evidence is unavailable.') from None


def _lookup(docker,name):
    ids=_command(docker,['network','ls','--quiet','--no-trunc','--filter','name=^'+name+'$']).split()
    if len(ids)>1 or any(not re.fullmatch('[a-f0-9]{64}',v) for v in ids):raise NetworkError('Database network identity is ambiguous.')
    if not ids:return None
    value=_json(docker,['network','inspect',ids[0]])
    if not isinstance(value,list) or len(value)!=1:raise NetworkError('Database network identity is ambiguous.')
    return value[0]


def _network(row,metadata,journal):
    if (row.get('Name')!=_name(metadata) or not re.fullmatch('[a-f0-9]{64}',str(row.get('Id')))
            or journal.get('network_id') and row['Id']!=journal['network_id']
            or row.get('Driver')!='bridge' or row.get('Internal') is not True or row.get('Scope')!='local'
            or row.get('EnableIPv6') or row.get('Ingress') or row.get('Attachable') or row.get('Options')
            or row.get('Labels')!=_labels(metadata,journal)):
        raise NetworkError('Database bridge ownership or isolation changed.')


def _members(db,row,metadata,journal,docker,verify_application):
    members=row.get('Containers') or {}
    if not isinstance(members,dict) or len(members)>2:raise NetworkError('Database bridge membership is invalid.')
    target=journal['before']['container_id']
    for identifier,endpoint in members.items():
        if not re.fullmatch('[a-f0-9]{64}',identifier):raise NetworkError('Database bridge member identity is invalid.')
        if identifier==target:continue
        actual=_json(docker,['inspect',identifier])
        if not isinstance(actual,list) or len(actual)!=1 or actual[0].get('Id')!=identifier:raise NetworkError('Application peer observation is invalid.')
        actual=actual[0]
        record=db.execute('SELECT document FROM service_bindings WHERE service_id=?',(metadata['service_id'],)).fetchone()
        accepted=json.loads(record['document']) if record else None
        if accepted and accepted.get('container_id')==identifier:
            if actual.get('Image')!=accepted.get('image_id'):raise NetworkError('Application peer image changed.')
        elif callable(verify_application):
            if verify_application(actual) is not True:raise NetworkError('Application peer was not fully verified.')
        else:raise NetworkError('An unknown application joined the database bridge.')
        labels=actual.get('Config',{}).get('Labels') or {}
        if labels.get('net.shiftcodes.managed.service')!=metadata['service_id'] or labels.get('net.shiftcodes.managed.project')!=metadata['project'] or labels.get('net.shiftcodes.managed.environment')!=metadata['environment']:
            raise NetworkError('Application peer scope changed.')
        actual_networks=actual.get('NetworkSettings',{}).get('Networks',{})
        if set(actual_networks)!={row['Name'],metadata['project']+'-'+metadata['environment']+'-managed'}:
            raise NetworkError('Application peer has an unexpected network attachment.')
        attached=actual_networks.get(row['Name'])
        if not attached or attached.get('NetworkID')!=row['Id'] or attached.get('EndpointID')!=endpoint.get('EndpointID') or metadata['alias'] in (attached.get('Aliases') or []):raise NetworkError('Database alias or peer endpoint changed.')


def _stable(candidate):
    # Registry counters are reauthorized independently; they are not Docker identity.
    return {k:v for k,v in candidate.items() if k not in ('fingerprint','project_revision','environment_revision')}


def _siblings(db,metadata,journal,candidate,docker):
    """Only exact, separately verified same-DB bridge endpoints may accumulate."""
    allowed={}
    rows=db.execute('SELECT binding_id,topology_digest,document FROM application_database_networks WHERE binding_id<>? LIMIT 1001',(metadata['pin']['binding_id'],)).fetchall()
    if len(rows)>1000:raise NetworkError('Application database network inventory exceeds its bound.')
    for record in rows:
        try:
            other=json.loads(record['document'])
            if other['before']['container_id']!=candidate['container_id']:continue
            if other['phase']!='verified':raise NetworkError('A sibling database attachment requires reconciliation.')
            topology=other['topology'];bid=record['binding_id']
            if topology['project']!=metadata['project'] or topology['environment']!=metadata['environment'] or topology['binding_id']!=bid or bindings.common._digest(topology)!=record['topology_digest']:raise ValueError()
            head=bindings._row(db,metadata['project'],metadata['environment'],bid)
            revision=db.execute('SELECT MAX(revision) FROM application_database_definitions WHERE binding_id=?',(bid,)).fetchone()[0]
            definition,_=bindings._saved(db,head,revision);owner=bindings._ownership(db,definition)
            expected={'binding_id':bid,'project':metadata['project'],'environment':metadata['environment'],'service_id':head['service_id'],**{k:definition[k] for k in ('ownership','engine','database_name','database_port')},'ownership_digest':owner['recipe_digest'],'adoption_fingerprint':owner['adoption_fingerprint']}
            if topology!=expected:raise ValueError()
            meta={'pin':{'binding_id':bid},'project':metadata['project'],'environment':metadata['environment'],'service_id':head['service_id'],'alias':'appdb-'+bid,'topology_digest':record['topology_digest']}
            row=_lookup(docker,_name(meta))
            if row is None:raise NetworkError('A recorded sibling database bridge disappeared.')
            _network(row,meta,other);_members(db,row,meta,other,docker,None)
            after=other['after']
            if bindings.common._digest({k:v for k,v in after.items() if k!='fingerprint'})!=after['fingerprint']:raise ValueError()
            if {k:v for k,v in _stable(after).items() if k!='networks'}!={k:v for k,v in _stable(candidate).items() if k!='networks'}:raise ValueError()
            endpoint=next(n for n in after['networks'] if n['name']==row['Name'])
            actual=[n for n in candidate['networks'] if n['name']==row['Name']]
            member=(row.get('Containers') or {}).get(candidate['container_id'])
            if actual!=[endpoint] or endpoint['id']!=row['Id'] or endpoint['aliases']!=[meta['alias']] or not member or member.get('EndpointID')!=endpoint['endpoint_id']:raise ValueError()
            allowed[row['Name']]=endpoint
        except NetworkError:raise
        except (ValueError,KeyError,TypeError,StopIteration):raise NetworkError('Sibling database bridge evidence changed.') from None
    return allowed


def _normalized(candidate,allowed):
    value=_stable(candidate)
    for endpoint in value['networks']:
        if endpoint['name'] in allowed and endpoint!=allowed[endpoint['name']]:raise NetworkError('Recorded sibling database endpoint changed.')
    value['networks']=[n for n in value['networks'] if n['name'] not in allowed]
    return value


def _transition(db,candidate,journal,metadata,row,docker):
    before=journal['before'];networks=[n for n in candidate['networks'] if n['name']==row['Name']]
    if len(networks)!=1 or networks[0]['id']!=row['Id'] or networks[0]['aliases']!=[metadata['alias']] or not re.fullmatch('[a-f0-9]{64}',networks[0]['endpoint_id']):raise NetworkError('Database endpoint attachment is unverified.')
    allowed=_siblings(db,metadata,journal,candidate,docker)
    base=_normalized(candidate,allowed)
    base['networks']=[n for n in base['networks'] if n['name']!=row['Name']]
    if base!=_normalized(before,allowed):raise NetworkError('Database identity or original networks changed.')
    if journal.get('after') and _normalized(candidate,allowed)!=_normalized(journal['after'],allowed):raise NetworkError('Accepted database network endpoint changed.')
    endpoint=(row.get('Containers') or {}).get(before['container_id'])
    if not endpoint or endpoint.get('EndpointID')!=networks[0]['endpoint_id']:raise NetworkError('Database endpoint membership differs.')


def _descriptor(metadata,journal):
    return {'name':_name(metadata),'id':journal['network_id'],'alias':metadata['alias'],'binding_id':metadata['pin']['binding_id'],'topology_digest':metadata['topology_digest'],'database_container_id':journal['before']['container_id']}


def recorded_attachment(db,metadata):
    """Historical descriptor only; caller must immediately strong-verify runtime.

    This is deliberately not authorization or proof that the network still exists.
    It breaks the peer-verification recursion when reconciling a replacement app.
    """
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_database_networks'").fetchone():raise NetworkError('Database network has no accepted evidence.')
    journal=_load(db,metadata)
    if journal is None or journal['phase']!='verified':raise NetworkError('Database network requires reconciliation.')
    return _descriptor(metadata,journal)


def verify_attachment(db,metadata,*,docker,authorize,resolve_candidate,verify_application=None):
    """Strong runtime observation without Docker effects or network journal repair."""
    if db.in_transaction:raise NetworkError('Network observation cannot hold a SQLite transaction.')
    _authority(db,metadata,authorize)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_database_networks'").fetchone():raise NetworkError('Database network has no accepted evidence.')
    journal=_load(db,metadata)
    if journal is None or journal['phase']!='verified':raise NetworkError('Database network requires reconciliation.')
    row=_lookup(docker,_name(metadata))
    if row is None:raise NetworkError('Database network disappeared.')
    _network(row,metadata,journal);_members(db,row,metadata,journal,docker,verify_application)
    _transition(db,_candidate(db,metadata,resolve_candidate),journal,metadata,row,docker)
    _authority(db,metadata,authorize)
    return _descriptor(metadata,journal)


def ensure_attachment(db,metadata,*,docker,authorize,resolve_candidate,verify_application=None):
    if db.in_transaction:raise NetworkError('Network effects cannot hold a SQLite transaction.')
    _authority(db,metadata,authorize);initialize(db)
    journal=_load(db,metadata)
    if journal and journal['phase']=='verified':return verify_attachment(db,metadata,docker=docker,authorize=authorize,resolve_candidate=resolve_candidate,verify_application=verify_application)
    candidate=_candidate(db,metadata,resolve_candidate);_migration_fence(db,metadata,candidate)
    row=_lookup(docker,_name(metadata))
    if journal is None:
        if row is not None:raise NetworkError('An existing database bridge has no durable creation intent.')
        journal={'topology':metadata['topology'],'nonce':uuid.uuid4().hex,'before':candidate,'phase':'prepared'}
        _save(db,metadata,journal,'prepared',authorize)
    if row is None:
        if journal['phase']!='prepared':raise NetworkError('Database bridge creation is uncertain; no duplicate was created.')
        if _stable(candidate)!=_stable(journal['before']):raise NetworkError('Database changed before bridge creation.')
        _save(db,metadata,journal,'create-intent',authorize);_authority(db,metadata,authorize)
        _command(docker,['network','create','--driver','bridge','--internal',*[arg for k,v in _labels(metadata,journal).items() for arg in ('--label',k+'='+v)],_name(metadata)])
        row=_lookup(docker,_name(metadata))
        if row is None:raise NetworkError('Database bridge creation was not observed.')
    elif journal['phase']=='prepared':raise NetworkError('Unjournaled database network appeared.')
    _network(row,metadata,journal);_members(db,row,metadata,journal,docker,verify_application)
    journal['network_id']=row['Id']
    if journal['phase'] in ('create-intent','network-created'):
        if _stable(candidate)!=_stable(journal['before']) or journal['before']['container_id'] in (row.get('Containers') or {}):raise NetworkError('Unjournaled database endpoint appeared.')
        _save(db,metadata,journal,'network-created',authorize)
        _migration_fence(db,metadata,candidate);_save(db,metadata,journal,'connect-intent',authorize)
        _authority(db,metadata,authorize)
        if _stable(_candidate(db,metadata,resolve_candidate))!=_stable(journal['before']):raise NetworkError('Database changed before attachment.')
        _command(docker,['network','connect','--alias',metadata['alias'],row['Id'],candidate['container_id']])
    row=_lookup(docker,_name(metadata))
    if row is None:raise NetworkError('Database network disappeared during attachment.')
    _network(row,metadata,journal);_members(db,row,metadata,journal,docker,verify_application)
    candidate=_candidate(db,metadata,resolve_candidate);_transition(db,candidate,journal,metadata,row,docker)
    _authority(db,metadata,authorize);journal['after']=candidate
    _save(db,metadata,journal,'verified',authorize)
    return _descriptor(metadata,journal)


def verify_stopping_attachment(db,metadata,*,docker,authorize):
    """Observe exact owned topology for stopping; never connect or start a DB."""
    if db.in_transaction:raise NetworkError('Stop observation cannot hold a SQLite transaction.')
    def authority():
        if bindings.resolve_stopping(db,metadata['project'],metadata['environment'],metadata['service_id'],metadata['pin'],authorize=authorize)!=metadata:
            raise NetworkError('Application stop authority changed.')
    authority();journal=_load(db,metadata)
    if not journal or journal['phase']!='verified':raise NetworkError('Database network requires reconciliation.')
    accepted=journal['after'];identifier=accepted['container_id']
    observed=_json(docker,['inspect',identifier])
    if not isinstance(observed,list) or len(observed)!=1:raise NetworkError('Database identity is unavailable.')
    actual=observed[0]
    if actual.get('Id')!=identifier or actual.get('Image')!=accepted['image_id'] or actual.get('HostConfig',{}).get('NetworkMode')!=accepted['network_mode']:
        raise NetworkError('Database identity changed.')
    stopped=actual.get('State',{}).get('Running') is False
    mounts=actual.get('Mounts') or []
    for storage in accepted['storage']:
        matches=[m for m in mounts if m.get('Destination')==storage['destination']]
        if len(matches)!=1 or any(matches[0].get(k)!=storage[v] for k,v in [('Type','type'),('Source','source'),('RW','rw')]):raise NetworkError('Database storage changed.')
    live=actual.get('NetworkSettings',{}).get('Networks',{})
    expected={n['name']:n for n in accepted['networks']}
    # Other same-DB attachments must each have their own exact verified journal.
    rows=db.execute('SELECT binding_id,document FROM application_database_networks LIMIT 1001').fetchall()
    if len(rows)>1000:raise NetworkError('Database network inventory exceeds its bound.')
    for record in rows:
        other=json.loads(record['document'])
        if other['before']['container_id']!=identifier:continue
        if other['phase']!='verified':raise NetworkError('A database attachment requires reconciliation.')
        topology=other['topology'];bid=record['binding_id']
        head=bindings._row(db,metadata['project'],metadata['environment'],bid)
        revision=db.execute('SELECT MAX(revision) FROM application_database_definitions WHERE binding_id=?',(bid,)).fetchone()[0]
        definition,_=bindings._saved(db,head,revision);owner=bindings._ownership(db,definition)
        required={'binding_id':bid,'project':metadata['project'],'environment':metadata['environment'],'service_id':head['service_id'],**{k:definition[k] for k in ('ownership','engine','database_name','database_port')},'ownership_digest':owner['recipe_digest'],'adoption_fingerprint':owner['adoption_fingerprint']}
        if topology!=required:raise NetworkError('Sibling database ownership changed.')
        meta={'pin':{'binding_id':bid},'project':metadata['project'],'environment':metadata['environment'],'service_id':head['service_id'],'alias':'appdb-'+bid,'topology_digest':bindings.common._digest(topology)}
        row=_lookup(docker,_name(meta))
        if row is None:raise NetworkError('Database bridge disappeared.')
        _network(row,meta,other);_members(db,row,meta,other,docker,None)
        endpoint=next((n for n in other['after']['networks'] if n['name']==row['Name']),None)
        if not endpoint or endpoint['id']!=row['Id'] or endpoint['aliases']!=[meta['alias']]:raise NetworkError('Database endpoint changed.')
        member=(row.get('Containers') or {}).get(identifier)
        observed_endpoint=live.get(row['Name'],{}).get('EndpointID')
        detached=stopped and observed_endpoint=='' and member is None
        if not detached and (observed_endpoint!=endpoint['endpoint_id'] or not member or member.get('EndpointID')!=endpoint['endpoint_id']):raise NetworkError('Database membership changed.')
        expected[row['Name']]=endpoint
    if set(live)!=set(expected):raise NetworkError('Database networks changed.')
    for name,n in expected.items():
        value=live[name]
        endpoint_matches=value.get('EndpointID')==n['endpoint_id'] or (stopped and value.get('EndpointID')=='')
        if value.get('NetworkID')!=n['id'] or not endpoint_matches or sorted(value.get('Aliases') or [])!=n['aliases']:
            raise NetworkError('Database endpoint changed.')
    authority()
    return _descriptor(metadata,journal)
