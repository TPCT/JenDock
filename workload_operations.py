"""Durable single failed-job retries. No transport, principal or Docker authority implied.

Callbacks: authorize(actor, accepted_request, action) must check live authority and
all accepted pins; transport(package_request, reauthorize) must call the guard
immediately before exact-container I/O. No SQLite transaction spans that I/O.
A dispatch intent is never dispatched twice, including after process death.
"""
import hashlib
import json
import re
import time
import uuid
import registry
import workload_protocol

class WorkloadError(ValueError):
    pass

# These exact pinned PHP Protocol.php errors occur before enqueue, inside the
# transaction that rolls back before ProtocolCommand emits `unsupported`.
# Do not broaden this set to dependency/timeout/response/transport failures.
REJECTIONS=frozenset({'failure_changed','unsupported_payload','unsupported_retry_policy','request_conflict','unregistered_queue'})
TERMINAL=frozenset({'requeued','rejected'})

FIELDS={'request_id','project','environment','service_id','container_id','definition_digest','adapter_revision','connection_id','queue','failed_job_id','failure_revision'}

def _match(value,pattern):
    return isinstance(value,str) and re.fullmatch(pattern,value) is not None

def _request(value):
    if not isinstance(value,dict) or set(value)!=FIELDS:raise WorkloadError('invalid_request')
    value=dict(value)
    patterns={'request_id':r'[A-Za-z0-9_-]{16,80}','project':r'[a-z0-9][a-z0-9-]{0,63}','service_id':r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}','failed_job_id':r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}','container_id':r'[0-9a-f]{64}','definition_digest':r'[0-9a-f]{64}','failure_revision':r'[0-9a-f]{64}','connection_id':r'[A-Za-z_][A-Za-z0-9_]{0,62}','queue':r'[A-Za-z0-9_-]{1,80}'}
    if any(not _match(value[k],p) for k,p in patterns.items()) or value['environment'] not in ('development','production') or type(value['adapter_revision']) is not int or value['adapter_revision']!=1:raise WorkloadError('invalid_request')
    return value

def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS workload_retry_operations(
    id TEXT PRIMARY KEY,actor TEXT NOT NULL,project TEXT NOT NULL,environment TEXT NOT NULL,
    request_id TEXT NOT NULL,request TEXT NOT NULL,fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,receipt TEXT,created REAL NOT NULL,updated REAL NOT NULL,
    UNIQUE(actor,project,environment,request_id))''')
    if 'reason' not in {row[1] for row in db.execute('PRAGMA table_info(workload_retry_operations)')}:
        db.execute('ALTER TABLE workload_retry_operations ADD COLUMN reason TEXT')
    db.execute("""CREATE TRIGGER IF NOT EXISTS workload_retry_identity BEFORE UPDATE ON workload_retry_operations
    WHEN NEW.id!=OLD.id OR NEW.actor!=OLD.actor OR NEW.project!=OLD.project OR NEW.environment!=OLD.environment OR NEW.request_id!=OLD.request_id OR NEW.request!=OLD.request OR NEW.fingerprint!=OLD.fingerprint OR NEW.created!=OLD.created
    BEGIN SELECT RAISE(ABORT,'Retry identity is immutable'); END""")
    db.execute("CREATE TRIGGER IF NOT EXISTS workload_retry_delete BEFORE DELETE ON workload_retry_operations BEGIN SELECT RAISE(ABORT,'Retry evidence is retained'); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS workload_retry_replace BEFORE INSERT ON workload_retry_operations WHEN EXISTS(SELECT 1 FROM workload_retry_operations WHERE id=NEW.id OR (actor=NEW.actor AND project=NEW.project AND environment=NEW.environment AND request_id=NEW.request_id)) BEGIN SELECT RAISE(ABORT,'Retry evidence is retained'); END")

def _authorize(authorize,actor,request,action):
    if not _match(actor,r'[^\x00-\x1f\x7f]{1,200}') or not callable(authorize):raise WorkloadError('authority_required')
    try:authorize(actor,dict(request),action)
    except Exception:raise WorkloadError('authorization_denied') from None

def _load(db,actor,operation_id,authorize,action):
    row=db.execute('SELECT * FROM workload_retry_operations WHERE id=?',(operation_id,)).fetchone()
    if row is None or row['actor']!=actor:raise WorkloadError('operation_not_found')
    row=dict(row);request=_request(json.loads(row['request']))
    if hashlib.sha256(registry.canonical(request).encode()).hexdigest()!=row['fingerprint']:raise WorkloadError('invalid_evidence')
    _authorize(authorize,actor,request,action);row['accepted']=request
    return row

def _public(row):
    result={'operation_id':row['id'],'request_id':row['request_id'],'status':row['state'],'business_execution':'not-observed'}
    result['target']={k:v for k,v in json.loads(row['request']).items() if k!='request_id'}
    if row['state']=='rejected':
        reason=row['reason']
        if reason not in REJECTIONS:raise WorkloadError('invalid_evidence')
        result['reason']=reason
    if row['receipt']:result['receipt']=json.loads(row['receipt'])
    return result

def accept(db,actor,request,*,authorize):
    request=_request(request);_authorize(authorize,actor,request,'read');initialize(db)
    encoded=registry.canonical(request);fingerprint=hashlib.sha256(encoded.encode()).hexdigest()
    with registry.transaction(db):
        row=db.execute('SELECT * FROM workload_retry_operations WHERE actor=? AND project=? AND environment=? AND request_id=?',(actor,request['project'],request['environment'],request['request_id'])).fetchone()
        if row:
            _authorize(authorize,actor,request,'replay')
            if row['fingerprint']!=fingerprint or row['request']!=encoded:raise WorkloadError('request_conflict')
            return _public(row)
        _authorize(authorize,actor,request,'accept')
        identity=uuid.uuid4().hex;now=time.time()
        db.execute('INSERT INTO workload_retry_operations(id,actor,project,environment,request_id,request,fingerprint,state,receipt,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(identity,actor,request['project'],request['environment'],request['request_id'],encoded,fingerprint,'accepted',None,now,now))
        return _public(_load(db,actor,identity,authorize,'read'))

def get(db,actor,operation_id,*,authorize):
    return _public(_load(db,actor,operation_id,authorize,'read'))

def _wire(row,operation):
    accepted=row['accepted'];request={'protocol':1,'operation':operation,'scope':{k:accepted[k] for k in ('project','environment','service_id','definition_digest')},'request_id':row['id']}
    if operation=='queue.retry':request.update({k:accepted[k] for k in ('queue','failed_job_id','failure_revision')})
    return request

def _receipt(response,request,row):
    response=workload_protocol.validate_response(response,request)
    if response['status']!='ok':raise WorkloadError('adapter_rejected')
    receipt=response['result']
    if receipt['status']=='absent':return None
    if receipt['failed_job_id']!=row['accepted']['failed_job_id'] or receipt['queue']!=row['accepted']['queue']:raise WorkloadError('invalid_receipt')
    return receipt


def dispatch(db,actor,operation_id,*,authorize,transport):
    if db.in_transaction:raise WorkloadError('transaction_open')
    with registry.transaction(db):
        row=_load(db,actor,operation_id,authorize,'dispatch')
        if row['state'] in TERMINAL:return _public(row)
        retry=row['state']=='accepted'
        if retry:db.execute("UPDATE workload_retry_operations SET state='dispatching',updated=? WHERE id=?",(time.time(),operation_id))
    return _observe(db,actor,operation_id,authorize,transport,'queue.retry' if retry else 'queue.receipt')

def reconcile(db,actor,operation_id,*,authorize,transport):
    if db.in_transaction:raise WorkloadError('transaction_open')
    row=_load(db,actor,operation_id,authorize,'reconcile')
    if row['state']=='accepted' or row['state'] in TERMINAL:return _public(row)
    return _observe(db,actor,operation_id,authorize,transport,'queue.receipt')

def _observe(db,actor,operation_id,authorize,transport,operation):
    row=_load(db,actor,operation_id,authorize,'reconcile' if operation=='queue.receipt' else 'dispatch')
    request=_wire(row,operation)
    def guard():_load(db,actor,operation_id,authorize,'reconcile' if operation=='queue.receipt' else 'dispatch')
    reason=None
    try:
        guard();response=workload_protocol.validate_response(transport(request,guard),request)
        if operation=='queue.retry' and response['status']=='unsupported' and response['error']['code'] in REJECTIONS:
            reason=response['error']['code'];receipt=None
        else:receipt=_receipt(response,request,row)
    except Exception:
        # No exception text or untrusted response is durable/public evidence.
        receipt=None
    with registry.transaction(db):
        current=_load(db,actor,operation_id,authorize,'reconcile_commit' if operation=='queue.receipt' else 'commit')
        if current['state'] in TERMINAL:return _public(current)
        db.execute('UPDATE workload_retry_operations SET state=?,receipt=?,reason=?,updated=? WHERE id=?',('requeued' if receipt else 'rejected' if reason else 'recovery-required',registry.canonical(receipt) if receipt else None,reason,time.time(),operation_id))
        return _public(_load(db,actor,operation_id,authorize,'read'))
