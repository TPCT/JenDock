"""Leased renewal attempts; caller holds control.LOCK, never SQL across RPC.

The serving domain journal is promoted only after exact candidate TLS verification.
Unknown issuance and reload outcomes are reconciled, never blindly repeated.
"""
import contextlib
import json
import math
import time
import uuid
import cloudflare_dns
import domain_execution as execution
import registry

WINDOW_SECONDS=30*86400
CHECK_SECONDS=86400
LEASE_SECONDS=900


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS domain_renewals(
        operation_id TEXT PRIMARY KEY,renewal_id TEXT NOT NULL,phase TEXT NOT NULL,state TEXT NOT NULL,
        worker TEXT,lease_generation INTEGER NOT NULL,lease_expires REAL,next_at REAL NOT NULL,
        failures INTEGER NOT NULL,document TEXT NOT NULL,error_code TEXT,updated REAL NOT NULL)''')
    db.execute('CREATE TABLE IF NOT EXISTS domain_renewal_history(renewal_id TEXT PRIMARY KEY,operation_id TEXT NOT NULL,state TEXT NOT NULL,document TEXT NOT NULL,completed REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS domain_renewal_scan(id INTEGER PRIMARY KEY CHECK(id=1),cursor TEXT NOT NULL)')


def _row(db,operation_id):
    row=db.execute('SELECT * FROM domain_renewals WHERE operation_id=?',(operation_id,)).fetchone()
    if not row:raise ValueError('Renewal attempt is unavailable.')
    return row


def schedule(db):
    initialize(db)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='domain_executions' AND type='table'").fetchone():return
    now=time.time()
    with registry.transaction(db):
        saved=db.execute('SELECT cursor FROM domain_renewal_scan WHERE id=1').fetchone()
        cursor=saved['cursor'] if saved else ''
        rows=db.execute("SELECT * FROM domain_executions WHERE operation_id>? AND phase IN ('origin-verified','external') AND state IN ('ready','awaiting-external','queued') ORDER BY operation_id LIMIT 100",(cursor,)).fetchall()
        for row in rows:
            try:
                prior=db.execute('SELECT * FROM domain_renewals WHERE operation_id=?',(row['operation_id'],)).fetchone()
                if prior and (prior['state']!='complete' or prior['next_at']>now):continue
                journal=json.loads(row['journal']);cert=journal.get('certificate');stage=journal.get('tls_stage');verified=journal.get('tls_verified')
                if not isinstance(cert,dict) or not isinstance(stage,dict) or not isinstance(verified,dict):continue
                expiry=cert.get('not_after')
                if type(expiry) not in (int,float) or not math.isfinite(expiry) or expiry-now>WINDOW_SECONDS:continue
                operation=execution._operation(db,row['operation_id']);_,definition=execution._definition(db,operation)
                execution._tls_evidence(stage,operation['route_id'],operation['id'],cert,('staged','active'))
                if any(verified.get(k)!=cert.get(k) for k in ('generation','fingerprint','not_after')) or verified.get('digest')!=stage['digest']:continue
                document={'base_certificate':cert,'base_stage':stage,'definition_id':row['definition_id'],'definition_digest':row['definition_digest']}
                db.execute('INSERT OR REPLACE INTO domain_renewals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (row['operation_id'],uuid.uuid4().hex,'issue','queued',None,(prior['lease_generation'] if prior else 0),None,now,0,registry.canonical(document),None,now))
            except (ValueError,KeyError,TypeError,AttributeError):
                # A retired/malformed record cannot starve other owned routes.
                # SQL failures deliberately escape rather than masking storage faults.
                continue
        next_cursor=rows[-1]['operation_id'] if len(rows)==100 else ''
        db.execute('INSERT OR REPLACE INTO domain_renewal_scan VALUES(1,?)',(next_cursor,))


def claim(db,worker):
    now=time.time()
    with registry.transaction(db):
        # The durable phase/document retain whether an effect may have happened.
        db.execute("UPDATE domain_renewals SET state='queued',worker=NULL,lease_expires=NULL,next_at=?,error_code='lease_expired' WHERE state='running' AND lease_expires<=?",(now,now))
        row=db.execute("SELECT * FROM domain_renewals WHERE state='queued' AND next_at<=? ORDER BY next_at,operation_id LIMIT 1",(now,)).fetchone()
        if not row:return None
        generation=row['lease_generation']+1
        db.execute("UPDATE domain_renewals SET state='running',worker=?,lease_generation=?,lease_expires=?,updated=? WHERE operation_id=?",(worker,generation,now+LEASE_SECONDS,now,row['operation_id']))
        return (row['operation_id'],worker,generation)


def _owned(db,claim):
    row=_row(db,claim[0])
    if row['state']!='running' or row['worker']!=claim[1] or row['lease_generation']!=claim[2] or row['lease_expires']<=time.time():raise ValueError('Renewal lease changed.')
    return row


def authorize(db,claim,resolve_principal=None):
    row=_owned(db,claim);document=json.loads(row['document'])
    operation=execution._operation(db,claim[0]);_,definition=execution._definition(db,operation)
    current=execution._row(db,claim[0]);journal=json.loads(current['journal'])
    if (current['definition_id']!=document['definition_id'] or current['definition_digest']!=document['definition_digest']
            or current['phase'] not in ('origin-verified','external')
            or journal.get('certificate')!=document['base_certificate'] or journal.get('tls_stage')!=document['base_stage']):
        raise ValueError('Serving domain changed during renewal.')
    principal=(resolve_principal or execution.resolve_authority)(db,operation)
    if principal.subject_id!=operation['subject'] or principal.credential_id!=operation['credential_id']:raise ValueError('Renewal owner changed.')
    execution._current_authority(db,principal,operation,definition,current,allow_expired=True)
    return operation,definition,journal


def _save(db,claim,document,*,phase=None,complete=False,resolve_principal=None):
    already_authorized=db.in_transaction
    with contextlib.nullcontext() if already_authorized else registry.transaction(db):
        row=_owned(db,claim)
        if not already_authorized:authorize(db,claim,resolve_principal)
        if complete:
            db.execute('INSERT OR IGNORE INTO domain_renewal_history VALUES(?,?,?,?,?)',(row['renewal_id'],claim[0],'complete',registry.canonical(document),time.time()))
        certificate=document.get('candidate',document['base_certificate'])
        delay=min(CHECK_SECONDS,max(300,(certificate['not_after']-time.time())/2)) if complete else 0
        db.execute('UPDATE domain_renewals SET document=?,phase=?,state=?,worker=NULL,lease_expires=NULL,next_at=?,failures=0,error_code=NULL,updated=? WHERE operation_id=?',
            (registry.canonical(document),phase or row['phase'],'complete' if complete else 'queued',time.time()+delay,time.time(),claim[0]))


def _intent(db,claim,document,kind):
    document['pending']=kind
    with registry.transaction(db):
        _owned(db,claim)
        db.execute('UPDATE domain_renewals SET document=? WHERE operation_id=?',(registry.canonical(document),claim[0]))
        if kind=='activate':
            active=execution._row(db,claim[0]);serving=json.loads(active['journal'])
            serving['pending_effect']='renewal'
            db.execute('UPDATE domain_executions SET journal=? WHERE operation_id=?',(registry.canonical(serving),claim[0]))


def process_once(db,*,worker_id,host_client,key_path,resolve_principal=None,provider_transport=None):
    if db.in_transaction:raise ValueError('Renewal effects require an independent connection.')
    schedule(db);attempt=claim(db,worker_id)
    if attempt is None:return False
    row=_row(db,attempt[0]);document=json.loads(row['document']);phase=row['phase']
    def check(action='write'):
        with contextlib.nullcontext() if db.in_transaction else registry.transaction(db):
            result=authorize(db,attempt,resolve_principal)
            if action=='adopt':
                import ingress
                p=(resolve_principal or execution.resolve_authority)(db,result[0])
                ingress._authorize(db,p,'domains:adopt',result[1]['project'],result[1]['environment'])
            db.execute('UPDATE domain_renewals SET lease_expires=? WHERE operation_id=?',(time.time()+LEASE_SECONDS,attempt[0]))
        return result
    try:
        operation,definition,journal=check();identity={'route_id':operation['route_id'],'operation_id':operation['id']}
        if phase=='issue':
            # Existing verified intent is mandatory: this is provider replay only.
            intent=db.execute('SELECT * FROM ingress_dns_intents WHERE operation_id=? AND route_id=?',(operation['id'],operation['route_id'])).fetchone()
            owner=db.execute('SELECT * FROM ingress_dns_ownership WHERE operation_id=? AND route_id=?',(operation['id'],operation['route_id'])).fetchone()
            if not intent or not owner or intent['state']!='verified' or intent['record_id']!=owner['record_id']:raise ValueError('Owned DNS evidence is unavailable.')
            cloudflare_dns.apply(db,operation['route_id'],operation['id'],key_path,preview_digest=journal['dns_preview']['preview_digest'],
                adopt_record_id=(journal.get('adoption') or {}).get('record_id'),transport=provider_transport,authorize=check)
            request=dict(identity,renewal_id=row['renewal_id'])
            if document.get('pending')=='issue':
                result=host_client.call('reconcile_certificate',request)
                if isinstance(result,dict) and result.get('status')=='absent':
                    if any(result.get(key)!=value for key,value in request.items()):
                        raise ValueError('Absent certificate attempt identity changed.')
                    # The host proved this exact attempt never acquired a journal.
                    # Retain the durable intent and replay its same idempotency ID.
                    check()
                    result=host_client.call('renew_certificate',dict(request,expected_generation=document['base_certificate']['generation']))
            else:
                _intent(db,attempt,document,'issue');check()
                result=host_client.call('renew_certificate',dict(request,expected_generation=document['base_certificate']['generation']))
            if result.get('renewal_id')!=row['renewal_id']:raise ValueError('Certificate attempt identity changed.')
            if result.get('status')=='reconciliation-required' and result.get('retryable') is True:
                deadline=result.get('retry_after');history=document.get('failed_attempts',[])
                if (type(deadline) not in (int,float) or not math.isfinite(deadline) or not 0<deadline<=time.time()
                        or len(history)>=3 or result.get('route_id')!=identity['route_id'] or result.get('operation_id')!=identity['operation_id']):
                    raise ValueError('Certificate retry is unavailable.')
                with registry.transaction(db):
                    authorize(db,attempt,resolve_principal)
                    db.execute('INSERT OR IGNORE INTO domain_renewal_history VALUES(?,?,?,?,?)',
                        (row['renewal_id'],operation['id'],'failed-ended',registry.canonical(document),time.time()))
                    document['failed_attempts']=[*history,{'renewal_id':row['renewal_id'],'retry_after':deadline}]
                    document.pop('pending',None)
                    db.execute("UPDATE domain_renewals SET renewal_id=?,document=?,state='queued',worker=NULL,lease_expires=NULL,next_at=?,updated=? WHERE operation_id=?",
                        (uuid.uuid4().hex,registry.canonical(document),time.time()+60,time.time(),operation['id']))
                return True
            execution._certificate_evidence(result,operation['route_id'],operation['id'],definition['hostname'])
            check();document.pop('pending',None)
            if result['generation']==document['base_certificate']['generation']:
                if any(result[k]!=document['base_certificate'][k] for k in ('fingerprint','not_after')):raise ValueError('Unchanged certificate identity differs.')
                _save(db,attempt,document,complete=True,resolve_principal=resolve_principal);return True
            document['candidate']=result;_save(db,attempt,document,phase='stage',resolve_principal=resolve_principal);return True
        candidate=document['candidate']
        execution._certificate_evidence(candidate,operation['route_id'],operation['id'],definition['hostname'])
        if phase=='stage':
            _intent(db,attempt,document,'stage');check()
            stage=host_client.call('stage_tls',dict(identity,expected_digest=document['base_stage']['digest'],generation=candidate['generation'],fingerprint=candidate['fingerprint']))
            execution._tls_evidence(stage,operation['route_id'],operation['id'],candidate,('staged','active'))
            check();document['candidate_stage']=stage;document.pop('pending',None);_save(db,attempt,document,phase='activate',resolve_principal=resolve_principal);return True
        if phase!='activate':raise ValueError('Renewal phase is unavailable.')
        stage=document['candidate_stage']
        if document.get('pending')=='activate':
            current=host_client.call('inspect_tls',identity)
            execution._tls_evidence(current,operation['route_id'],operation['id'],candidate,('staged','active','reload-intent','installing','restoring-file'))
            if current['digest']!=stage['digest']:raise ValueError('TLS candidate changed.')
            if current['status'] not in ('staged','active'):
                current=host_client.call('recover_tls',identity)
                execution._tls_evidence(current,operation['route_id'],operation['id'],candidate,('staged','active'))
                if current['digest']!=stage['digest']:raise ValueError('Recovered TLS candidate changed.')
            if current['status']=='staged':
                check();host_client.call('activate_tls',dict(identity,digest=stage['digest']))
        else:
            _intent(db,attempt,document,'activate');check()
            host_client.call('activate_tls',dict(identity,digest=stage['digest']))
        result=host_client.call('verify_tls',identity)
        execution._tls_evidence(result,operation['route_id'],operation['id'],candidate,('active',))
        if result['digest']!=stage['digest']:raise ValueError('Served TLS candidate changed.')
        with registry.transaction(db):
            operation,definition,journal=authorize(db,attempt,resolve_principal)
            journal.update(certificate=candidate,tls_stage=stage,tls_verified={'digest':stage['digest'],'generation':candidate['generation'],
                'fingerprint':candidate['fingerprint'],'not_after':candidate['not_after'],'observed':time.time()},external_recheck_at=0)
            journal.pop('external_verified',None)
            if journal.get('pending_effect')=='renewal':journal.pop('pending_effect')
            db.execute("UPDATE domain_executions SET phase='external',state='awaiting-external',journal=?,worker=NULL,lease_expires=NULL,updated=? WHERE operation_id=?",(registry.canonical(journal),time.time(),operation['id']))
            db.execute("UPDATE ingress_operations SET status='awaiting-external' WHERE id=?",(operation['id'],))
            _save(db,attempt,document,complete=True)
    except Exception:
        # Keep pending intent so another worker may only reconcile uncertain effects.
        with registry.transaction(db):
            try:current=_owned(db,attempt)
            except ValueError:return True
            failures=current['failures']+1;delay=min(3600,60*2**min(failures-1,6))
            db.execute("UPDATE domain_renewals SET state='queued',worker=NULL,lease_expires=NULL,next_at=?,failures=?,error_code='renewal_unverified',updated=? WHERE operation_id=?",(time.time()+delay,failures,time.time(),attempt[0]))
    return True
