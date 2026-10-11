"""Exact managed container lifecycle, with durable intent and observation-only recovery.

The worker holds control.LOCK. No operation recreates a container or changes its
immutable definition. Effective desired state is a separate durable projection.
"""
import copy
import json
from pathlib import Path
import re
import time
import registry
import managed_services
import resource_policy

TERMINAL={'succeeded','failed'}


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS managed_lifecycle_operations(id TEXT PRIMARY KEY,project TEXT NOT NULL,environment TEXT NOT NULL,service_id TEXT NOT NULL,state TEXT NOT NULL,document TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS managed_lifecycle_desires(service_id TEXT PRIMARY KEY,state TEXT NOT NULL,operation_id TEXT NOT NULL,updated REAL NOT NULL)')
    db.execute("CREATE TRIGGER IF NOT EXISTS managed_lifecycle_capture_immutable BEFORE UPDATE OF id,project,environment,service_id,document,created ON managed_lifecycle_operations BEGIN SELECT RAISE(ABORT,'Lifecycle capture is immutable'); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS managed_lifecycle_capture_no_replace BEFORE INSERT ON managed_lifecycle_operations WHEN EXISTS(SELECT 1 FROM managed_lifecycle_operations WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT,'Lifecycle capture is immutable'); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS managed_lifecycle_capture_no_delete BEFORE DELETE ON managed_lifecycle_operations BEGIN SELECT RAISE(ABORT,'Lifecycle capture is immutable'); END")


def desired(db,service_id,fallback):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='managed_lifecycle_desires'").fetchone():return fallback
    row=db.execute('SELECT state FROM managed_lifecycle_desires WHERE service_id=?',(service_id,)).fetchone()
    return row['state'] if row else fallback


def pending(db,project,environment):
    initialize(db)
    return bool(db.execute("SELECT 1 FROM managed_lifecycle_operations WHERE project=? AND environment=? AND state NOT IN ('succeeded','failed')",(project,environment)).fetchone())


def load(db,operation_id):
    initialize(db)
    row=db.execute('SELECT * FROM managed_lifecycle_operations WHERE id=?',(operation_id,)).fetchone()
    if not row:raise ValueError('Managed lifecycle operation was not found.')
    return {**dict(row),'captured':json.loads(row['document'])}


def request_identity(body):
    """Nonsecret original request identity; never resolve a replacement target on replay."""
    return {key:body.get(key) for key in ('action','project','environment','container','container_id','revision','request_id')} | {'actor':body.get('actor','admin')}


def replay_request(db,body):
    initialize(db)
    row=db.execute('SELECT document FROM managed_lifecycle_operations WHERE id=?',(body.get('request_id'),)).fetchone()
    if not row:return None
    original=json.loads(row['document']).get('request')
    if original is None or original!=request_identity(body):
        raise ValueError('Operation ID belongs to a different lifecycle request.')
    return public(db,body['request_id'])


def public(db,operation_id):
    row=load(db,operation_id)
    messages={'accepted':'Container lifecycle request accepted.','applying':'Container lifecycle action is in progress.',
              'succeeded':'Container state and managed ownership were verified.',
              'failed':'Container lifecycle action did not complete.',
              'recovery-required':'Container lifecycle outcome requires observation before further actions.'}
    return {'operation_id':operation_id,'status':row['state'],'message':messages[row['state']]}


def _write(db,operation_id,state):
    db.execute('UPDATE managed_lifecycle_operations SET state=?,updated=? WHERE id=?',(state,time.time(),operation_id))
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='control_runs'").fetchone():
        result=public(db,operation_id)
        db.execute("UPDATE control_runs SET status=?,detail=?,updated=? WHERE request_id=? AND operation IN ('container-start','container-stop','container-restart') AND project=(SELECT project FROM managed_lifecycle_operations WHERE id=?)",
                   (state,result['message'],time.time(),operation_id,operation_id))


def _authority(db,row,authorize):
    if not callable(authorize):raise ValueError('Current lifecycle authority is required.')
    authorize();capture=row['captured'];p,e=row['project'],row['environment']
    if registry.get_project(db,p)['revision']!=capture['project_revision'] or registry.get_environment(db,p,e)['revision']!=capture['environment_revision'] or resource_policy.read(db,p)['revision']!=capture['policy_revision']:
        raise ValueError('Lifecycle environment authority changed.')
    peers=managed_services.environment_definitions(db,p,e)
    if {peer['id'] for peer in peers}!={peer['id'] for peer in capture['peers']}:raise ValueError('Lifecycle peer membership changed.')
    expected={peer['id']:peer for peer in capture['peers']}
    for peer in peers:
        prior=expected[peer['id']]
        if any(peer[k]!=prior[k] for k in ('definition_id','definition','operation_id','binding','effective_resources','state')):
            raise ValueError('Lifecycle managed ownership changed.')
    return peers


def observe(db,row,*,docker,authorize):
    """Full exact app/manifest/caps/env/network verification; no lifecycle effects."""
    import service_runtime
    import service_provisioning as provision
    from ingress_runtime import verified_network_members
    peers=_authority(db,row,authorize);capture=row['captured'];selected=next(p for p in peers if p['id']==row['service_id'])
    binding=selected['binding'];definition=selected['definition']
    if not binding or len(binding['compose_files'])!=1 or len(binding['network_ids'])!=1:raise ValueError('Exact managed lifecycle binding is required.')
    path=Path(binding['compose_files'][0])
    if not path.is_absolute() or any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('Lifecycle manifest path is not canonical.')
    before=path.read_bytes();manifest=json.loads(before)
    context=(service_runtime.StoppingContext if capture['action']=='stop' else service_runtime.Context)(db,lambda:_authority(db,row,authorize))
    descriptors={p['id']:context.attachment(p['definition'],docker) for p in peers}
    networks={'environment':{'external':True,'name':binding['network_name']}}
    for descriptor in descriptors.values():
        if descriptor:networks[descriptor['name']]={'external':True,'name':descriptor['name']}
    if (set(manifest)!={'name','services','networks'} or manifest['name']!=row['project'] or manifest['networks']!=networks
        or set(manifest['services'])!={provision._names(p['definition'])[0] for p in peers}):raise ValueError('Lifecycle manifest ownership changed.')
    result=None;caps={'mem_limit','memswap_limit','cpus','deploy'}
    for peer in peers:
        d=peer['definition'];owned=peer['binding'];descriptor=descriptors[peer['id']]
        if not owned or owned.get('database_network')!=descriptor:raise ValueError('Lifecycle database network binding changed.')
        spec=provision.service_config(d,peer['operation_id'],peer['effective_resources'],runtime_context=context,database_network=descriptor)
        actual_spec=manifest['services'][provision._names(d)[0]]
        if {k:v for k,v in spec.items() if k not in caps}!={k:v for k,v in actual_spec.items() if k not in caps}:raise ValueError('Lifecycle service manifest changed.')
        actual=provision._json([docker,'inspect',owned['container_id']])[0]
        if actual.get('Id')!=owned['container_id']:raise ValueError('Lifecycle container identity changed.')
        provision._verify(actual,d,peer['operation_id'],owned['image_id'],binding['network_ids'][0],owned['container_name'],path,peer['effective_resources'],runtime_context=context,docker=docker,database_network=descriptor)
        if peer['id']==row['service_id']:result=actual
        elif bool(actual.get('State',{}).get('Running'))!=(owned['state']=='running'):raise ValueError('Lifecycle peer runtime state changed.')
    network=provision._json([docker,'network','inspect',binding['network_ids'][0]])[0]
    provision._network(network,definition,binding['network_ids'][0])
    known={p['binding']['container_id'] for p in peers}
    known.update(verified_network_members(db,row['project'],row['environment'],docker=docker,application_network_id=binding['network_ids'][0],authorize=authorize))
    if set(network.get('Containers') or {})-known:raise ValueError('Unknown lifecycle network member.')
    if path.read_bytes()!=before:raise ValueError('Lifecycle manifest changed during observation.')
    _authority(db,row,authorize)
    return result


def accept(db,operation_id,service_id,action,*,expected_container_id,docker,authorize,request=None):
    if not callable(authorize):raise ValueError('Current lifecycle authority is required.')
    authorize();initialize(db)
    if action not in ('start','stop','restart') or not isinstance(operation_id,str) or not re.fullmatch('[A-Za-z0-9-]{16,80}',operation_id):raise ValueError('Invalid managed lifecycle request.')
    service=managed_services.get(db,service_id);p,e=service['project'],service['environment']
    if not service['binding'] or service['binding']['container_id']!=expected_container_id:raise ValueError('Lifecycle container was replaced.')
    import managed_delivery
    managed_delivery.capture(db,p,e)  # Existing delivery/settings/lifecycle fences.
    if pending(db,p,e):raise ValueError('A lifecycle operation needs recovery.')
    capture={'action':action,'target':'stopped' if action=='stop' else 'running',
             'peers':managed_services.environment_definitions(db,p,e),
             'project_revision':registry.get_project(db,p)['revision'],'environment_revision':registry.get_environment(db,p,e)['revision'],
             'policy_revision':resource_policy.read(db,p)['revision']}
    if request is not None:
        if (request.get('request_id'),request.get('project'),request.get('container_id'),request.get('action'))!=(operation_id,p,expected_container_id,'container-'+action) or request.get('environment',e)!=e:
            raise ValueError('Lifecycle request scope differs from its accepted target.')
        capture['request']=request_identity(request)
    row={'id':operation_id,'project':p,'environment':e,'service_id':service_id,'captured':capture}
    actual=observe(db,row,docker=docker,authorize=authorize)
    if bool(actual['State']['Running'])!=(service['binding']['state']=='running'):raise ValueError('Lifecycle runtime state differs from its current binding.')
    started=actual['State'].get('StartedAt')
    if not isinstance(started,str) or not started:raise ValueError('Lifecycle startup identity is unavailable.')
    capture['started_at']=started
    with registry.transaction(db):
        _authority(db,row,authorize)
        now=time.time()
        db.execute('INSERT INTO managed_lifecycle_operations VALUES(?,?,?,?,?,?,?,?)',(operation_id,p,e,service_id,'accepted',registry.canonical(capture),now,now))
        db.execute('INSERT INTO managed_lifecycle_desires VALUES(?,?,?,?) ON CONFLICT(service_id) DO UPDATE SET state=excluded.state,operation_id=excluded.operation_id,updated=excluded.updated',
                   (service_id,capture['target'],operation_id,now))
    return public(db,operation_id)


def _matches(row,actual):
    capture=row['captured'];state=actual.get('State',{})
    if bool(state.get('Running'))!=(capture['target']=='running'):return False
    if capture['target']=='running' and state.get('Health',{}).get('Status')!='healthy':return False
    if capture['action']=='restart' and (not state.get('StartedAt') or state['StartedAt']==capture['started_at']):return False
    return True


def _commit(db,row,actual,authorize):
    if not _matches(row,actual):raise ValueError('Lifecycle target state is not verified.')
    with registry.transaction(db):
        peers=_authority(db,row,authorize);service=next(p for p in peers if p['id']==row['service_id'])
        if actual['Id']!=service['binding']['container_id']:raise ValueError('Lifecycle cannot replace a container.')
        binding=dict(service['binding'],state=row['captured']['target'],observed=time.time());now=time.time()
        db.execute('UPDATE service_bindings SET document=?,updated=? WHERE service_id=?',(registry.canonical(binding),now,row['service_id']))
        db.execute('UPDATE managed_services SET state=?,updated=? WHERE id=?',(binding['state'],now,row['service_id']))
        states={binding['state'] if p['id']==row['service_id'] else p['binding']['state'] for p in peers}
        aggregate='running' if states=={'running'} else 'stopped' if states=={'stopped'} else 'degraded'
        db.execute('UPDATE registry_environments SET state=? WHERE project=? AND name=?',(aggregate,row['project'],row['environment']))
        _write(db,row['id'],'succeeded')
    return public(db,row['id'])


def apply(db,operation_id,*,docker,authorize,health_timeout=35):
    import service_provisioning as provision
    row=load(db,operation_id)
    if row['state']!='accepted':return public(db,operation_id)
    actual=observe(db,row,docker=docker,authorize=authorize)
    if row['captured']['action']!='restart' and _matches(row,actual):return _commit(db,row,actual,authorize)
    _authority(db,row,authorize);_write(db,operation_id,'applying')
    try:
        action=row['captured']['action'];service=next(p for p in row['captured']['peers'] if p['id']==row['service_id'])
        provision.command([docker,action,*(['--time','20'] if action in ('stop','restart') else []),service['binding']['container_id']],timeout=60)
        deadline=time.monotonic()+health_timeout
        while True:
            actual=observe(db,row,docker=docker,authorize=authorize)
            if _matches(row,actual):return _commit(db,row,actual,authorize)
            if time.monotonic()>=deadline:break
            time.sleep(.25)
    except (ValueError,OSError,TimeoutError):pass
    _write(db,operation_id,'recovery-required')
    return public(db,operation_id)


def reconcile(db,operation_id,*,docker,authorize):
    row=load(db,operation_id)
    if row['state'] in TERMINAL:return public(db,operation_id)
    actual=observe(db,row,docker=docker,authorize=authorize)
    if _matches(row,actual):return _commit(db,row,actual,authorize)
    _write(db,operation_id,'recovery-required')
    return public(db,operation_id)


def recover_pending(db,*,docker,authorize_project):
    initialize(db)
    for row in db.execute("SELECT id,project,state FROM managed_lifecycle_operations WHERE state NOT IN ('succeeded','failed') ORDER BY created LIMIT 20").fetchall():
        try:
            callback=apply if row['state']=='accepted' else reconcile
            callback(db,row['id'],docker=docker,authorize=lambda:authorize_project(row['project']))
        except (ValueError,OSError):continue
