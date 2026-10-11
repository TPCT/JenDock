"""Post-settings proof maintenance using its own durable runtime operation authority."""
import json
import sqlite3
import time
from pathlib import Path
import registry
import managed_services
import ingress_bindings
import ingress_runtime as runtime
import ingress_route_generations as generations
from ingress_proof_refresh import route_authority
import service_runtime_operations as operations


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS ingress_runtime_refresh(operation_id TEXT PRIMARY KEY,state TEXT NOT NULL,document TEXT NOT NULL,updated REAL NOT NULL)')


def enqueue(db,operation_id):
    if not db.in_transaction:raise ValueError('Runtime proof refresh requires the binding commit transaction.')
    initialize(db)
    db.execute("INSERT OR IGNORE INTO ingress_runtime_refresh VALUES(?,'pending','{}',?)",(operation_id,time.time()))


def scan(db):
    initialize(db);operations.initialize(db)
    for row in db.execute("SELECT id,document FROM service_runtime_operations WHERE state IN ('succeeded','rolled-back')"):
        if json.loads(row['document'])['item'].get('ingress_snapshot'):
            db.execute("INSERT OR IGNORE INTO ingress_runtime_refresh VALUES(?,'pending','{}',?)",(row['id'],time.time()))


def authority(db,operation_id,document):
    saved=operations.load(db,operation_id)
    if saved['state'] not in ('succeeded','rolled-back') or not saved.get('verified_binding'):
        raise ValueError('Runtime operation has not committed verified evidence.')
    item=saved['item'];new=saved['state']=='succeeded'
    definition=item['next_definition'] if new else item['definition']
    definition_id=saved['next_definition_id'] if new else saved['definition_id']
    current=managed_services.get(db,saved['service_id'])
    if (current['definition_id']!=definition_id or current['definition']!=definition
        or current['binding']!=saved['verified_binding'] or current['operation_id']!=item['operation_id']):
        raise ValueError('Runtime binding changed after commit.')
    plan={'project':saved['project'],'environment':saved['environment'],'operation_id':operation_id,
          'state':saved['state'],'definition_id':definition_id,'definition':definition,'binding':saved['verified_binding']}
    if document.get('plan_digest') and runtime.digest(plan)!=document['plan_digest']:raise ValueError('Runtime proof authority changed.')
    if document.get('gateway'):
        gateway=document['gateway']
        row=db.execute('SELECT * FROM ingress_runtime_intents WHERE project=? AND environment=?',(plan['project'],plan['environment'])).fetchone()
        if not row or row['digest']!=runtime.digest(gateway) or json.loads(row['definition'])!=gateway or row['root']!=document['root'] or row['phase']!='verified':raise ValueError('Owned gateway changed.')
        if ingress_bindings.current_routes(db,gateway)!=document['routes']:raise ValueError('Current backend vector changed.')
        if route_authority(db,gateway,document['routes'])!=document['authorities']:raise ValueError('Original route authority changed.')
    return plan


def process_once(db,*,root,docker,refresh=runtime.refresh_gateway_proof,recover=runtime.recover_gateway_proof):
    if db.in_transaction:raise ValueError('Proof effects cannot run inside a database transaction.')
    scan(db)
    row=db.execute("SELECT q.* FROM ingress_runtime_refresh q JOIN service_runtime_operations d ON d.id=q.operation_id WHERE d.state IN ('succeeded','rolled-back') AND q.state IN ('pending','running','recovery-required') AND (q.state='pending' OR q.updated<?) ORDER BY q.updated LIMIT 1",(time.time()-30,)).fetchone()
    if row is None:return False
    job=row['operation_id'];document=json.loads(row['document'])
    try:
        plan=authority(db,job,document)
        if not document:
            gateway_row=db.execute('SELECT * FROM ingress_runtime_intents WHERE project=? AND environment=?',(plan['project'],plan['environment'])).fetchone()
            if not gateway_row:raise ValueError('Owned gateway is missing.')
            gateway=json.loads(gateway_row['definition'])
            if gateway['schema_version']!=3:raise ValueError('Gateway requires an explicit proof upgrade.')
            if gateway_row['root']!=str(Path(root).resolve()):raise ValueError('Worker gateway root differs.')
            routes=ingress_bindings.current_routes(db,gateway)
            key=runtime.digest({'runtime_operation':job,'gateway':runtime.digest(gateway),'routes':routes})
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
            db.execute("UPDATE ingress_runtime_refresh SET state='running',document=?,updated=? WHERE operation_id=?",(registry.canonical(document),time.time(),job))
        callback=recover if operation else refresh
        callback(db,document['gateway'],document['operation_id'],root=root,docker=docker,authorize=authorize)
        with registry.transaction(db):
            authorize()
            db.execute("UPDATE ingress_runtime_refresh SET state='succeeded',updated=? WHERE operation_id=?",(time.time(),job))
    except (ValueError,OSError,TimeoutError,sqlite3.OperationalError):
        db.execute("UPDATE ingress_runtime_refresh SET state='recovery-required',updated=? WHERE operation_id=?",(time.time(),job))
    return True
