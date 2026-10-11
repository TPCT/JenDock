"""Post-delivery proof maintenance; worker holds control.LOCK, effects outside SQL.

The committed delivery and fresh original domain principals authorize maintenance.
This can only republish the exact owned route's current verified backend pins.
"""
import json
import sqlite3
from pathlib import Path
import time
import ingress_bindings
import ingress_route_generations as generations
import ingress_runtime as runtime
import managed_delivery
import managed_services
import registry


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS ingress_delivery_refresh(job_id INTEGER PRIMARY KEY,state TEXT NOT NULL,document TEXT NOT NULL,updated REAL NOT NULL)')


def enqueue(db,job_id):
    if not db.in_transaction:raise ValueError('Proof maintenance must be scheduled with the binding commit.')
    initialize(db)
    db.execute("INSERT OR IGNORE INTO ingress_delivery_refresh VALUES(?,'pending','{}',?)",(job_id,time.time()))


def scan(db):
    initialize(db)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='managed_deliveries'").fetchone():return
    # Terminal scan also repairs an interrupted upgrade before queue creation.
    for row in db.execute("SELECT job_id,document FROM managed_deliveries WHERE state IN ('succeeded','failed')").fetchall():
        plan=json.loads(row['document'])
        if any(entry.get('ingress_current') for entry in plan['services']):
            db.execute("INSERT OR IGNORE INTO ingress_delivery_refresh VALUES(?,'pending','{}',?)",(row['job_id'],time.time()))


def route_authority(db,gateway,routes):
    import domain_execution
    import ingress
    result=[]
    for route in routes:
        row=db.execute('SELECT * FROM ingress_routes WHERE project=? AND environment=? AND hostname=?',(gateway['project'],gateway['environment'],route['hostname'])).fetchone()
        if not row:raise ValueError('Accepted domain authority is required for proof maintenance.')
        operation=db.execute('SELECT * FROM ingress_operations WHERE route_id=? ORDER BY created LIMIT 1',(row['id'],)).fetchone()
        if not operation:raise ValueError('Original domain operation is unavailable.')
        principal=domain_execution.resolve_authority(db,dict(operation))
        ingress._authorize(db,principal,'domains:create',gateway['project'],gateway['environment'])
        _,definition=domain_execution._definition(db,dict(operation))
        if definition['service_id']!=route['service_id'] or definition['hostname']!=route['hostname']:raise ValueError('Accepted route target changed.')
        source=definition.get('source_environment')
        if principal.allowed_environments is not None and source is not None and (gateway['project'],source) not in principal.allowed_environments:raise ValueError('Original source environment is not permitted.')
        current=ingress._definition(db,principal,gateway['project'],gateway['environment'],{k:definition[k] for k in ingress.FIELDS},'domains:create')
        dynamic={'service_definition_id','service_definition_digest','container_id','binding_digest','artifact_id','artifact_digest'}
        if set(current)!=set(definition) or any(current[k]!=definition[k] for k in definition if k not in dynamic):raise ValueError('Stable domain authority changed.')
        result.append({'route_id':row['id'],'operation_id':operation['id'],'subject':operation['subject'],'credential_id':operation['credential_id']})
    return result


def authority(db,job_id,document):
    row,plan=managed_delivery._load(db,job_id)
    if row['state'] not in ('succeeded','failed') or row['worker'] is not None:raise ValueError('Delivery must release its lease before proof maintenance.')
    target=managed_delivery.load_target(db,job_id)
    if (plan['project'],plan['environment'])!=(target['project'],target['environment']):raise ValueError('Immutable delivery target changed.')
    if document.get('plan_digest') and runtime.digest(plan)!=document['plan_digest']:raise ValueError('Committed delivery evidence changed.')
    managed_delivery._environment_unchanged(db,plan)
    for entry in plan['services']:
        if entry['state'] not in ('succeeded','rolled-back','unchanged','skipped-stopped','pending'):raise ValueError('Unresolved delivery backend cannot refresh proof.')
        if entry['state']=='pending':
            if row['state']!='failed':raise ValueError('Pending service belongs to unfinished delivery.')
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='managed_rollout_journals'").fetchone() and db.execute('SELECT 1 FROM managed_rollout_journals WHERE operation_id=?',(str(job_id)+'-'+entry['service_id'],)).fetchone():raise ValueError('Untouched service has runtime effects to reconcile.')
        current=managed_services.get(db,entry['service_id'])
        expected=entry['next_definition_id'] if entry['state']=='succeeded' else entry['definition_id']
        binding=entry.get('verified_binding',entry['binding'])
        if current['definition_id']!=expected or current['binding']!=binding or current['operation_id']!=entry['operation_id']:raise ValueError('Application changed after committed delivery.')
    if document.get('gateway'):
        gateway=document['gateway']
        row=db.execute('SELECT * FROM ingress_runtime_intents WHERE project=? AND environment=?',(plan['project'],plan['environment'])).fetchone()
        if not row or row['digest']!=runtime.digest(gateway) or json.loads(row['definition'])!=gateway or row['root']!=document['root'] or row['phase']!='verified':raise ValueError('Owned gateway changed after scheduling.')
        if ingress_bindings.current_routes(db,gateway)!=document['routes']:raise ValueError('Current backend vector changed after scheduling.')
        if route_authority(db,gateway,document['routes'])!=document['authorities']:raise ValueError('Original route authority changed.')
    return plan


def process_once(db,*,root,docker,refresh=runtime.refresh_gateway_proof,recover=runtime.recover_gateway_proof):
    if db.in_transaction:raise ValueError('Proof effects cannot run inside a database transaction.')
    scan(db)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='managed_deliveries'").fetchone():return False
    row=db.execute("SELECT q.* FROM ingress_delivery_refresh q JOIN managed_deliveries d ON d.job_id=q.job_id WHERE d.state IN ('succeeded','failed') AND q.state IN ('pending','running','recovery-required') AND (q.state='pending' OR q.updated<?) ORDER BY q.updated LIMIT 1",(time.time()-30,)).fetchone()
    if row is None:return False
    job=row['job_id'];document=json.loads(row['document'])
    try:
        plan=authority(db,job,document)
        if not document:
            gateway_row=db.execute('SELECT * FROM ingress_runtime_intents WHERE project=? AND environment=?',(plan['project'],plan['environment'])).fetchone()
            if not gateway_row:raise ValueError('Owned gateway is missing.')
            gateway=json.loads(gateway_row['definition'])
            if gateway['schema_version']!=3:raise ValueError('Gateway requires an explicit proof upgrade.')
            if gateway_row['root']!=str(Path(root).resolve()):raise ValueError('Worker gateway root differs.')
            routes=ingress_bindings.current_routes(db,gateway)
            key=runtime.digest({'job':job,'gateway':runtime.digest(gateway),'routes':routes})
            document={'gateway':gateway,'root':str(Path(root).resolve()),'routes':routes,'plan_digest':runtime.digest(plan),'authorities':route_authority(db,gateway,routes),'base_id':'proof-'+key,'attempts':[],'operation_id':'proof-'+key+'-0'}
        authorize=lambda:authority(db,job,document)
        authorize()
        operation=generations.operation(db,document['operation_id'])
        if operation and operation['phase']=='aborted':
            old=operation['document']
            if (old.get('kind')!='proof-refresh' or old.get('abort_reason')!='pre-signal-failure' or len(document['attempts'])>=3
                    or old['gateway_digest']!=runtime.digest(document['gateway']) or old['backends']!=document['routes']):raise ValueError('Refresh retry is not safely bounded.')
            _,config,_=runtime._paths(root,document['gateway'])
            if config.is_symlink() or config.read_text()!=old['previous'] or generations.head(db,document['gateway'])!=old['before']:raise ValueError('Pre-signal compensation is unverified.')
            document['attempts'].append({'operation_id':document['operation_id'],'phase':'aborted','document_digest':runtime.digest(old)})
            document['operation_id']=document['base_id']+'-'+str(len(document['attempts']))
            operation=None
        with registry.transaction(db):
            authorize()
            db.execute("UPDATE ingress_delivery_refresh SET state='running',document=?,updated=? WHERE job_id=?",(registry.canonical(document),time.time(),job))
        callback=recover if operation else refresh
        callback(db,document['gateway'],document['operation_id'],root=root,docker=docker,authorize=authorize)
        with registry.transaction(db):
            authorize()
            db.execute("UPDATE ingress_delivery_refresh SET state='succeeded',updated=? WHERE job_id=?",(time.time(),job))
    except (ValueError,OSError,TimeoutError,sqlite3.OperationalError):
        db.execute("UPDATE ingress_delivery_refresh SET state='recovery-required',updated=? WHERE job_id=?",(time.time(),job))
    return True
