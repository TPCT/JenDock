"""Durable same-image managed environment changes, exclusively under control.LOCK.

No environment values appear in this journal. Recovery observes the existing
rollout journal; it never repeats an uncertain container replacement or start.
"""
import copy
import json
import re
import time
import uuid
import registry
import managed_services
import service_runtime
import ingress_bindings
import resource_policy

UNSET=object()

TERMINAL={'succeeded','rolled-back','failed'}


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS service_runtime_requests(id TEXT PRIMARY KEY,ciphertext BLOB NOT NULL)')
    for action in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS service_runtime_request_{action.lower()} BEFORE {action} ON service_runtime_requests BEGIN SELECT RAISE(ABORT,'Runtime request receipts are immutable'); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS service_runtime_request_replace BEFORE INSERT ON service_runtime_requests WHEN EXISTS(SELECT 1 FROM service_runtime_requests WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT,'Runtime request receipts are immutable'); END")
    db.execute('''CREATE TABLE IF NOT EXISTS service_runtime_operations(
        id TEXT PRIMARY KEY,project TEXT NOT NULL,environment TEXT NOT NULL,
        service_id TEXT NOT NULL,state TEXT NOT NULL,document TEXT NOT NULL,
        created REAL NOT NULL,updated REAL NOT NULL)''')


def pending(db,project,environment):
    import managed_lifecycle
    if managed_lifecycle.pending(db,project,environment):return True
    initialize(db)
    return bool(db.execute("SELECT 1 FROM service_runtime_operations WHERE project=? AND environment=? AND state NOT IN ('succeeded','rolled-back','failed')",(project,environment)).fetchone())


def load(db,operation_id):
    initialize(db)
    row=db.execute('SELECT * FROM service_runtime_operations WHERE id=?',(operation_id,)).fetchone()
    if not row:raise ValueError('Runtime operation was not found.')
    return {**dict(row),**json.loads(row['document'])}


def public(db,operation_id):
    row=load(db,operation_id)
    messages={'accepted':'Runtime settings saved; application is pending.','applying':'Runtime settings are being applied.',
              'succeeded':'Environment saved and runtime verified.','rolled-back':'New settings failed health checks; the previous runtime was restored.',
              'failed':'Runtime settings were not applied.','recovery-required':'Runtime outcome requires reconciliation before further changes.'}
    return {'operation_id':operation_id,'status':row['state'],'message':messages[row['state']]}


def _write(db,operation_id,state,document):
    db.execute('UPDATE service_runtime_operations SET state=?,document=?,updated=? WHERE id=?',
               (state,registry.canonical(document),time.time(),operation_id))
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='control_runs'").fetchone():
        result=public(db,operation_id)
        db.execute("UPDATE control_runs SET status=?,detail=?,updated=? WHERE request_id=? AND operation IN ('environment-save','container-limits-save','resource-apply') AND project=(SELECT project FROM service_runtime_operations WHERE id=?)",
                   (result['status'],result['message'],time.time(),operation_id,operation_id))


def _document(row):
    return {k:row[k] for k in ('item','definition_id','next_definition_id','project_revision','environment_revision','policy_revision','applied_before')} | {'resource_transition':row.get('resource_transition')}


def _unchanged(db,row,authorize):
    service_runtime._authorize(authorize)
    current=managed_services.get(db,row['service_id']);item=row['item']
    if current['definition_id']!=row['definition_id'] or current['definition']!=item['definition'] or current['binding']!=item['binding']:
        raise ValueError('Managed service changed during runtime transition.')
    if (registry.get_project(db,row['project'])['revision']!=row['project_revision']
        or registry.get_environment(db,row['project'],row['environment'])['revision']!=row['environment_revision']
        or resource_policy.read(db,row['project'])['revision']!=row['policy_revision']):
        raise ValueError('Runtime configuration authority changed.')
    transition=row.get('resource_transition')
    if transition is not None:
        expected=copy.deepcopy(transition['before'])
        expected['environments'][row['environment']]['services'][item['binding']['container_name']]=item['next_resources']
        owner=registry.get_environment(db,row['project'],row['environment'])
        if (transition['project']!=row['project'] or transition['revision']!=row['policy_revision']
            or transition['before']!=resource_policy.read(db,row['project'])['document'] or transition['after']!=expected
            or any(transition['owner'].get(k)!=owner[k] for k in ('name','revision','recipe_revision'))):
            raise ValueError('Accepted capacity transition changed.')
        resource_policy.validate(expected);managed_services.validate_capacity(db,row['project'],expected)
    if ingress_bindings.capture(db,row['project'],row['environment'],row['service_id'])!=item['ingress_snapshot']:
        raise ValueError('Runtime ingress authority changed.')
    # A saved settings change cannot begin while an unrelated delivery owns peers.
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='managed_deliveries'").fetchone()
    if exists:
        for entry in db.execute("SELECT document FROM managed_deliveries WHERE state IN ('queued','running','reconciling','recovery-required')"):
            plan=json.loads(entry['document'])
            if (plan['project'],plan['environment'])==(row['project'],row['environment']):raise ValueError('Managed delivery owns this environment.')


def accept(db,operation_id,service_id,values,*,expected_container_id,key_path,authorize,database_attachment=UNSET,resources=None,resource_transition=None,request=None):
    service_runtime._authorize(authorize);initialize(db)
    if not isinstance(operation_id,str) or not re.fullmatch('[A-Za-z0-9-]{16,80}',operation_id):raise ValueError('Invalid runtime operation identity.')
    if db.execute('SELECT 1 FROM service_runtime_operations WHERE id=?',(operation_id,)).fetchone():
        raise ValueError('Runtime operation already exists; inspect its recorded result.')
    service=managed_services.get(db,service_id);p,e=service['project'],service['environment']
    if not service.get('binding') or service['binding']['container_id']!=expected_container_id or service['state'] not in ('running','stopped'):
        raise ValueError('Managed runtime identity changed.')
    if pending(db,p,e):raise ValueError('An existing runtime operation requires reconciliation.')
    peers=managed_services.environment_definitions(db,p,e)
    selected=next(peer for peer in peers if peer['id']==service_id)
    if database_attachment is not UNSET:
        import application_database_bindings
        application_database_bindings.resolve_selection(db,p,e,service_id,database_attachment)
    if resources is not None:
        import direct_limits
        resources=direct_limits.limits(resources)
        if resource_transition is None and resources!=selected['effective_resources']:raise ValueError('New caps require an accepted capacity transition.')
    elif resource_transition is not None:raise ValueError('Capacity transition requires new caps.')
    current=service_runtime.status(db,p,e,service_id)
    document={'definition_id':service['definition_id'],'next_definition_id':str(uuid.uuid4()),
              'project_revision':registry.get_project(db,p)['revision'],
              'environment_revision':registry.get_environment(db,p,e)['revision'],
              'policy_revision':resource_policy.read(db,p)['revision'],'applied_before':current['applied'],'resource_transition':resource_transition,
              'item':{'definition':service['definition'],'binding':service['binding'],'resources':selected['effective_resources'],
                      'artifact':service['definition']['artifact'],'operation_id':service['operation_id'],'runtime_change':True,
                      'ingress_snapshot':ingress_bindings.capture(db,p,e,service_id)}}
    if resources is not None:document['item']['next_resources']=resources
    def guard():_unchanged(db,dict(document,project=p,environment=e,service_id=service_id),authorize)
    guard()
    with registry.transaction(db):
        guard()
        reference=service_runtime.save(db,p,e,service_id,values,expected=current['desired'],key_path=key_path,authorize=guard)
        new=copy.deepcopy(service['definition']);new.setdefault('runtime',{})['environment']=reference
        if resources is not None:new['resources']=copy.deepcopy(resources)
        if database_attachment is not UNSET:
            application_database_bindings.resolve_selection(db,p,e,service_id,database_attachment)
            new['runtime']['database_attachment']=copy.deepcopy(database_attachment)
        document['item']['next_definition']=new
        now=time.time()
        db.execute('INSERT INTO service_runtime_operations VALUES(?,?,?,?,?,?,?,?)',
                   (operation_id,p,e,service_id,'accepted',registry.canonical(document),now,now))
        if request is not None:
            import database_bindings
            receipt={'purpose':'managed-runtime-request','operation_id':operation_id,'project':p,'environment':e,'service_id':service_id,'request':_request(request)}
            if receipt['request']['request_id']!=operation_id or receipt['request']['project']!=p or receipt['request']['container_id']!=expected_container_id or receipt['request']['container']!=service['binding']['container_name']:raise ValueError('Runtime request identity differs.')
            encrypted=database_bindings._cipher(key_path).encrypt(registry.canonical(receipt).encode())
            db.execute('INSERT INTO service_runtime_requests VALUES(?,?)',(operation_id,encrypted))
    return public(db,operation_id)


def _commit(db,row,binding,*,new,authorize):
    with registry.transaction(db):
        _unchanged(db,row,authorize)
        item=row['item'];definition=item['next_definition'] if new else item['definition']
        definition_id=row['next_definition_id'] if new else row['definition_id']
        original=item['binding'];mutable={'container_id','definition_digest','image_id','observed','state','database_network'}
        if (definition.get('runtime') or {}).get('database_attachment'):
            import application_database_bindings as attachments
            import application_database_networks as networks
            metadata=attachments.resolve_runtime(db,row['project'],row['environment'],row['service_id'],definition['runtime']['database_attachment'],authorize=authorize)
            expected_network=networks.recorded_attachment(db,metadata)
        else:expected_network=None
        if binding.get('database_network')!=expected_network:raise ValueError('Runtime database network binding changed.')
        if (not isinstance(binding,dict) or binding.get('state')!=original['state']
            or binding.get('definition_digest')!=managed_services.digest(definition)
            or binding.get('image_id')!=definition['artifact']['image']
            or not re.fullmatch('[a-f0-9]{64}',str(binding.get('container_id','')))
            or {k:v for k,v in binding.items() if k not in mutable}!={k:v for k,v in original.items() if k not in mutable}
            or type(binding.get('observed')) not in (int,float) or not 0<=time.time()-binding['observed']<=90):
            raise ValueError('Verified runtime replacement evidence is invalid.')
        now=time.time()
        transition=row.get('resource_transition')
        if new and transition is not None:
            db.execute('INSERT OR REPLACE INTO resource_policies VALUES(?,?,?,?)',(row['project'],transition['revision']+1,registry.canonical(transition['after']),now))
        if new:
            db.execute('INSERT INTO service_definitions VALUES(?,?,?,?)',(definition_id,registry.canonical(definition),managed_services.digest(definition),now))
        changed=db.execute('UPDATE managed_services SET definition_id=?,updated=? WHERE id=? AND definition_id=?',
                           (definition_id,now,row['service_id'],row['definition_id'])).rowcount
        if changed!=1:raise ValueError('Runtime service CAS failed.')
        db.execute('UPDATE service_bindings SET document=?,updated=? WHERE service_id=?',(registry.canonical(binding),now,row['service_id']))
        ingress_bindings.advance(db,item['ingress_snapshot'],definition_id=definition_id,definition_digest=managed_services.digest(definition),
                                container_id=binding['container_id'],alias=binding['compose_service'],http_endpoint=definition['artifact'].get('http_endpoint'))
        if new:
            service_runtime.mark_applied(db,row['project'],row['environment'],row['service_id'],definition['runtime']['environment'],
                                         expected_applied=row['applied_before'],authorize=authorize)
        document=_document(row);document['verified_binding']=binding
        _write(db,row['id'],'succeeded' if new else 'rolled-back',document)
        if item.get('ingress_snapshot'):
            import runtime_proof_refresh
            runtime_proof_refresh.enqueue(db,row['id'])
    return public(db,row['id'])


def apply(db,operation_id,*,docker,compose,authorize):
    import managed_rollout
    row=load(db,operation_id)
    if row['state'] in TERMINAL:return public(db,operation_id)
    if row['state']!='accepted':return public(db,operation_id)
    def guard():_unchanged(db,row,authorize)
    guard()
    _write(db,operation_id,'applying',_document(row))
    try:
        binding=managed_rollout.rollout(db,operation_id,row['item'],docker=docker,compose=compose,authorize=guard)
        return _commit(db,row,binding,new=True,authorize=authorize)
    except managed_rollout.RolloutError as error:
        if error.restored_binding is not None:
            try:return _commit(db,row,error.restored_binding,new=False,authorize=authorize)
            except Exception:pass
    except Exception:pass
    _write(db,operation_id,'recovery-required',_document(row))
    return public(db,operation_id)


def reconcile(db,operation_id,*,docker,authorize):
    import managed_rollout
    row=load(db,operation_id)
    if row['state'] in TERMINAL:return public(db,operation_id)
    def guard():_unchanged(db,row,authorize)
    guard()
    observation=managed_rollout.reconcile(db,operation_id,row['item'],docker=docker,authorize=guard)
    if observation['outcome'] in ('verified-new','verified-old'):
        return _commit(db,row,observation['binding'],new=observation['outcome']=='verified-new',authorize=authorize)
    _write(db,operation_id,'recovery-required',_document(row))
    return public(db,operation_id)


def recover_pending(db,*,docker,compose=None,authorize_project):
    """Resume proven unstarted acceptance; uncertain effects are observation-only."""
    initialize(db)
    for record in db.execute("SELECT id,project FROM service_runtime_operations WHERE state NOT IN ('succeeded','rolled-back','failed') ORDER BY created LIMIT 20").fetchall():
        try:
            authorize=lambda:authorize_project(record['project'])
            if load(db,record['id'])['state']=='accepted' and compose is not None:
                apply(db,record['id'],docker=docker,compose=compose,authorize=authorize)
            else:reconcile(db,record['id'],docker=docker,authorize=authorize)
        except (ValueError,OSError):
            # The durable nonterminal operation keeps all subsequent mutations fenced.
            continue


def _request(body):
    if not isinstance(body,dict) or body.get('action')!='environment-save':raise ValueError('An environment request receipt is required.')
    result={k:body.get(k) for k in ('action','project','container','container_id','revision','request_id','changes')}
    result['actor']=body.get('actor','admin')
    if 'database_attachment' in body:result['database_attachment']=body['database_attachment']
    service_runtime._values(result['changes'])
    if any(not isinstance(result[k],str) or not result[k] for k in ('project','container','container_id','revision','request_id','actor')):raise ValueError('Runtime request context is invalid.')
    return result


def replay_request(db,body,*,key_path):
    """Compare a private encrypted accepted request before returning its receipt."""
    initialize(db)
    row=db.execute('SELECT id FROM service_runtime_operations WHERE id=?',(body.get('request_id'),)).fetchone()
    if row is None:return None  # Legacy generic operations have their existing path.
    accepted=load(db,row['id']);receipt=db.execute('SELECT ciphertext FROM service_runtime_requests WHERE id=?',(row['id'],)).fetchone()
    if receipt is None:raise ValueError('Original runtime request receipt is unavailable.')
    import database_bindings
    from cryptography.fernet import InvalidToken
    try:
        observed=json.loads(database_bindings._cipher(key_path).decrypt(receipt['ciphertext']))
        expected={'purpose':'managed-runtime-request','operation_id':accepted['id'],'project':accepted['project'],
                  'environment':accepted['environment'],'service_id':accepted['service_id'],'request':_request(body)}
        if observed!=expected:raise ValueError()
    except (ValueError,TypeError,InvalidToken):
        raise ValueError('This operation ID belongs to a different runtime request.') from None
    return public(db,row['id'])
