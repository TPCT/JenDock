"""Leased domain orchestration. Call process_once while holding control.LOCK once.

No SQLite transaction is held across Docker, host RPC, or provider operations.
Gateway callbacks are trusted worker capabilities, never request-supplied data.
"""
from dataclasses import dataclass
import contextlib
import json
import re
import time

import application_operations as ops
import cloudflare_dns
import registry

PHASES=('dns-preview','gateway','grant','stage','activate','dns','awaiting-tls','certificate','stage-tls','activate-tls','origin-verified')
STATES=('queued','running','failed','reconciliation-required','awaiting-adoption','awaiting-tls','awaiting-external')

@dataclass(frozen=True)
class Claim:
    operation_id:str
    worker:str
    generation:int


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS domain_executions(
        operation_id TEXT PRIMARY KEY,definition_id TEXT NOT NULL,definition_digest TEXT NOT NULL,
        phase TEXT NOT NULL,state TEXT NOT NULL,worker TEXT,lease_generation INTEGER NOT NULL DEFAULT 0,
        lease_expires REAL,journal TEXT NOT NULL,error_code TEXT,updated REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS domain_execution_events(
        id INTEGER PRIMARY KEY,operation_id TEXT NOT NULL,event TEXT NOT NULL,
        subject TEXT NOT NULL,previous_credential_id TEXT,credential_id TEXT,created REAL NOT NULL)''')


def _transaction(db):return contextlib.nullcontext() if db.in_transaction else registry.transaction(db)


def _operation(db,operation_id):
    row=db.execute('SELECT * FROM ingress_operations WHERE id=?',(operation_id,)).fetchone()
    if not row:raise ValueError('Accepted domain operation is unavailable.')
    return dict(row)


def _definition(db,operation):
    row=db.execute('SELECT r.*,d.document,d.digest FROM ingress_routes r JOIN ingress_definitions d ON d.id=r.definition_id WHERE r.id=?',(operation['route_id'],)).fetchone()
    if not row:raise ValueError('Accepted domain definition is unavailable.')
    document=json.loads(row['document'])
    import ingress
    if ingress._digest(document)!=row['digest'] or any(document[k]!=row[k] for k in ('project','environment','hostname')):raise ValueError('Accepted domain identity changed.')
    return dict(row),document


def resolve_authority(db,operation):
    """Resolve current configured console owner or immutable credential, every time."""
    import mcp_credentials
    if operation['credential_id'] is not None:
        principal=mcp_credentials.principal_for_credential(db,operation['credential_id'])
    else:principal=mcp_credentials.configured_owner(operation['subject'])
    if not isinstance(principal,ops.Principal) or principal.subject_id!=operation['subject'] or principal.credential_id!=operation['credential_id']:
        raise ValueError('Domain operation authority is unavailable.')
    return principal


def enqueue(db,operation_id):
    initialize(db)
    with _transaction(db):
        operation=_operation(db,operation_id);row,definition=_definition(db,operation)
        old=db.execute('SELECT * FROM domain_executions WHERE operation_id=?',(operation_id,)).fetchone()
        if old:
            if old['definition_id']!=row['definition_id'] or old['definition_digest']!=row['digest']:raise ValueError('Domain execution identity changed.')
            return public_status(db,operation_id)
        db.execute('INSERT INTO domain_executions VALUES(?,?,?,?,?,NULL,0,NULL,?,NULL,?)',(operation_id,row['definition_id'],row['digest'],'dns-preview','queued','{}',time.time()))
        db.execute("UPDATE ingress_operations SET status='queued' WHERE id=?",(operation_id,))
    return public_status(db,operation_id)


def _row(db,operation_id):
    row=db.execute('SELECT * FROM domain_executions WHERE operation_id=?',(operation_id,)).fetchone()
    if not row:raise ValueError('Domain execution is unavailable.')
    if row['phase'] not in PHASES or row['state'] not in STATES:raise ValueError('Domain execution state is invalid.')
    return row


def claim_next(db,worker,lease_seconds=300):
    if not isinstance(worker,str) or not 1<=len(worker)<=120 or type(lease_seconds) is not int or not 30<=lease_seconds<=3600:raise ValueError('Invalid domain worker lease.')
    initialize(db)
    with registry.transaction(db):
        now=time.time()
        # An expired worker may have completed an external effect. Never requeue it.
        for expired in db.execute("SELECT operation_id FROM domain_executions WHERE state='running' AND lease_expires<=?",(now,)).fetchall():
            db.execute("UPDATE domain_executions SET state='reconciliation-required',worker=NULL,lease_expires=NULL,error_code='lease_expired',updated=? WHERE operation_id=?",(now,expired['operation_id']))
            db.execute("UPDATE ingress_operations SET status='reconciliation-required' WHERE id=?",(expired['operation_id'],))
        row=db.execute("SELECT * FROM domain_executions WHERE state='queued' ORDER BY updated,operation_id LIMIT 1").fetchone()
        if row is None:return None
        generation=row['lease_generation']+1
        db.execute("UPDATE domain_executions SET state='running',worker=?,lease_generation=?,lease_expires=?,updated=? WHERE operation_id=? AND state='queued'",(worker,generation,now+lease_seconds,now,row['operation_id']))
        db.execute("UPDATE ingress_operations SET status='running' WHERE id=?",(row['operation_id'],))
        return Claim(row['operation_id'],worker,generation)


def authorize_claim(db,claim,*,resolve_principal=None,action='write'):
    row=_row(db,claim.operation_id)
    if row['state']!='running' or row['worker']!=claim.worker or row['lease_generation']!=claim.generation or row['lease_expires']<=time.time():raise ValueError('Domain lease changed or expired.')
    operation=_operation(db,claim.operation_id);route,definition=_definition(db,operation)
    if route['definition_id']!=row['definition_id'] or route['digest']!=row['definition_digest']:raise ValueError('Domain definition changed after acceptance.')
    principal=(resolve_principal or resolve_authority)(db,operation)
    if not isinstance(principal,ops.Principal) or principal.subject_id!=operation['subject'] or principal.credential_id!=operation['credential_id']:raise ValueError('Domain operation owner changed.')
    import ingress
    scope='domains:adopt' if action=='adopt' else 'domains:create'
    ingress._authorize(db,principal,scope,definition['project'],definition['environment'])
    current=ingress._definition(db,principal,definition['project'],definition['environment'],{key:definition[key] for key in ingress.FIELDS},'domains:create')
    if current!=definition:raise ValueError('Domain evidence changed; a fresh preview is required.')
    return definition


def renew_claim(db,claim,*,lease_seconds=300,resolve_principal=None,action='write'):
    if type(lease_seconds) is not int or not 30<=lease_seconds<=3600:raise ValueError('Invalid domain lease duration.')
    with _transaction(db):
        definition=authorize_claim(db,claim,resolve_principal=resolve_principal,action=action)
        db.execute('UPDATE domain_executions SET lease_expires=?,updated=? WHERE operation_id=? AND worker=? AND lease_generation=?',(time.time()+lease_seconds,time.time(),claim.operation_id,claim.worker,claim.generation))
    return definition


def _commit(db,claim,journal,*,phase=None,state=None,error_code=None,resolve_principal=None,authorize=True):
    with registry.transaction(db):
        row=_row(db,claim.operation_id)
        if row['state']!='running' or row['worker']!=claim.worker or row['lease_generation']!=claim.generation or row['lease_expires']<=time.time():raise ValueError('Domain lease changed before commit.')
        if authorize:authorize_claim(db,claim,resolve_principal=resolve_principal,action='adopt' if row['phase']=='dns' and journal.get('adoption') else 'write')
        phase=phase or row['phase'];state=state or 'running'
        if phase not in PHASES or state not in STATES:raise ValueError('Invalid domain phase transition.')
        db.execute('UPDATE domain_executions SET phase=?,state=?,journal=?,error_code=?,updated=?,worker=?,lease_expires=? WHERE operation_id=?',(phase,state,registry.canonical(journal),error_code,time.time(),claim.worker if state=='running' else None,row['lease_expires'] if state=='running' else None,claim.operation_id))
        db.execute('UPDATE ingress_operations SET status=? WHERE id=?',(state,claim.operation_id))


def _resume_authority(db,principal,project,environment,operation_id,scope):
    import ingress
    ingress._authorize(db,principal,scope,project,environment)
    operation=_operation(db,operation_id);route,definition=_definition(db,operation)
    if operation['subject']!=principal.subject_id or definition['project']!=project or definition['environment']!=environment:raise ValueError('Domain recovery belongs to another owner or environment.')
    current=ingress._definition(db,principal,project,environment,{key:definition[key] for key in ingress.FIELDS},'domains:create')
    if current!=definition:raise ValueError('Domain evidence changed; recovery cannot borrow stale grants.')
    return operation,definition


def resume(db,principal,project,environment,operation_id):
    with registry.transaction(db):
        operation,_=_resume_authority(db,principal,project,environment,operation_id,'domains:create')
        row=_row(db,operation_id)
        if row['state'] not in ('failed','reconciliation-required','awaiting-tls'):raise ValueError('Domain operation is not awaiting recovery.')
        journal=json.loads(row['journal']);journal['recovery_requested']=True
        db.execute('INSERT INTO domain_execution_events(operation_id,event,subject,previous_credential_id,credential_id,created) VALUES(?,?,?,?,?,?)',(operation_id,'resume',principal.subject_id,operation['credential_id'],principal.credential_id,time.time()))
        # Same owner may explicitly replace revoked execution authority; no old grant is reused.
        db.execute('UPDATE ingress_operations SET credential_id=?,status=? WHERE id=?',(principal.credential_id,'queued',operation_id))
        db.execute("UPDATE domain_executions SET state='queued',journal=?,error_code=NULL,worker=NULL,lease_expires=NULL,updated=? WHERE operation_id=?",(registry.canonical(journal),time.time(),operation_id))
    return public_status(db,operation_id)


def approve_adoption(db,principal,project,environment,operation_id,record_id,preview_digest):
    if not isinstance(record_id,str) or not re.fullmatch('[a-f0-9]{32}',record_id) or not isinstance(preview_digest,str) or not re.fullmatch('[a-f0-9]{64}',preview_digest):raise ValueError('Exact DNS adoption evidence is required.')
    with registry.transaction(db):
        operation,_=_resume_authority(db,principal,project,environment,operation_id,'domains:adopt')
        row=_row(db,operation_id);journal=json.loads(row['journal']);preview=journal.get('dns_preview',{})
        if row['state']!='awaiting-adoption' or preview.get('preview_digest')!=preview_digest or len(preview.get('records',[]))!=1 or preview['records'][0]['id']!=record_id:raise ValueError('DNS adoption preview changed.')
        journal['adoption']={'record_id':record_id,'preview_digest':preview_digest}
        db.execute('INSERT INTO domain_execution_events(operation_id,event,subject,previous_credential_id,credential_id,created) VALUES(?,?,?,?,?,?)',(operation_id,'approve-adoption',principal.subject_id,operation['credential_id'],principal.credential_id,time.time()))
        db.execute("UPDATE ingress_operations SET credential_id=?,status='queued' WHERE id=?",(principal.credential_id,operation_id))
        db.execute("UPDATE domain_executions SET phase='gateway',state='queued',journal=?,error_code=NULL,updated=? WHERE operation_id=?",(registry.canonical(journal),time.time(),operation_id))
    return public_status(db,operation_id)


def _certificate_evidence(value,route,operation,hostname):
    if (not isinstance(value,dict) or value.get('route_id')!=route or value.get('operation_id')!=operation or value.get('status')!='verified'
            or value.get('sans')!=[hostname] or any(not re.fullmatch('[a-f0-9]{64}',str(value.get(k,''))) for k in ('generation','fingerprint'))
            or type(value.get('not_after')) not in (int,float) or not value['not_after']>time.time()):raise ValueError('Verified current certificate identity is required.')


def _tls_evidence(value,route,operation,certificate,statuses):
    if (not isinstance(value,dict) or value.get('route_id')!=route or value.get('operation_id')!=operation or value.get('status') not in statuses
            or value.get('generation')!=certificate['generation'] or value.get('fingerprint')!=certificate['fingerprint']
            or not re.fullmatch('[a-f0-9]{64}',str(value.get('digest','')))):raise ValueError('Verified TLS transition identity is required.')


def public_status(db,operation_id):
    row=_row(db,operation_id);operation=_operation(db,operation_id);_,definition=_definition(db,operation);journal=json.loads(row['journal'])
    dns=journal.get('dns_verified');origin=journal.get('http_verified')
    import ingress
    dns_evidence={'status':'unverified','evidence':'none','observed_at':None,'live_checked':False}
    origin_evidence={'status':'unverified','evidence':'none','observed_at':None,'live_checked':False}
    def timestamp(value):return type(value) in (int,float) and 0<value<=time.time()
    if dns is not None:
        if not isinstance(dns,dict) or set(dns)!={'record_id','observed'} or not re.fullmatch('[a-f0-9]{32}',str(dns['record_id'])) or not timestamp(dns['observed']):raise ValueError('Recorded DNS verification is invalid.')
        dns_evidence=ingress._dns_evidence(db,operation['route_id'],definition)
        if dns_evidence.get('status')!='verified' or dns_evidence.get('record_id')!=dns['record_id']:raise ValueError('Recorded DNS ownership is unavailable.')
    if origin is not None:
        stage=journal.get('stage')
        if not isinstance(origin,dict) or set(origin)!={'digest','observed'} or not re.fullmatch('[a-f0-9]{64}',str(origin['digest'])) or not timestamp(origin['observed']) or not isinstance(stage,dict) or stage.get('digest')!=origin['digest'] or stage.get('route_id')!=operation['route_id'] or stage.get('operation_id')!=operation_id:raise ValueError('Recorded HTTP verification is invalid.')
        origin_evidence={'status':'http-verified','evidence':'recorded','observed_at':origin['observed'],'live_checked':False}
    tls_evidence={'status':'unverified','evidence':'none','observed_at':None,'live_checked':False}
    tls=journal.get('tls_verified')
    if tls is not None:
        cert=journal.get('certificate');stage=journal.get('tls_stage')
        if (not isinstance(tls,dict) or set(tls)!={'digest','generation','fingerprint','not_after','observed'} or not timestamp(tls['observed'])
                or not isinstance(cert,dict) or not isinstance(stage,dict)
                or any(tls[k]!=cert.get(k) for k in ('generation','fingerprint','not_after')) or tls['digest']!=stage.get('digest')
                or type(tls['not_after']) not in (int,float)
                or cert.get('route_id')!=operation['route_id'] or cert.get('operation_id')!=operation_id or cert.get('sans')!=[definition['hostname']]
                or any(not re.fullmatch('[a-f0-9]{64}',str(tls[k])) for k in ('generation','fingerprint','digest'))):raise ValueError('Recorded TLS verification is invalid.')
        _tls_evidence(stage,operation['route_id'],operation_id,cert,('staged','active'))
        tls_evidence={'status':'origin-verified' if tls['not_after']>time.time() else 'expired','evidence':'recorded','observed_at':tls['observed'],'live_checked':False,'fingerprint':tls['fingerprint'],'not_after':tls['not_after']}
    return {'id':operation_id,'route_id':operation['route_id'],'project':definition['project'],'environment':definition['environment'],
            'status':row['state'],'phase':row['phase'],'updated_at':row['updated'],'error_code':row['error_code'],
            'dns':dns_evidence,'origin':origin_evidence,
            'tls':tls_evidence,'ready':False,**ingress.executor_status(db),
            'next_action':{'awaiting-adoption':'review-dns-adoption','reconciliation-required':'resume','failed':'review-and-resume','awaiting-tls':'resume','awaiting-external':'verify-external'}.get(row['state']),
            'adoption_preview':journal.get('dns_preview') if row['state']=='awaiting-adoption' else None}


def process_once(db,*,worker_id,gateway,host_client,key_path,resolve_principal=None,provider_transport=None,lease_seconds=300):
    """Execute one bounded phase; caller owns the non-reentrant control lock.

    gateway(db,definition,operation_id,authorize=callback,reconcile=bool) must
    use persisted runtime identity and return a freshly verified gateway binding.
    """
    if db.in_transaction:raise ValueError('Domain effects require an independent database connection without an open transaction.')
    claim=claim_next(db,worker_id,lease_seconds)
    if claim is None:return False
    journal=json.loads(_row(db,claim.operation_id)['journal']);phase=_row(db,claim.operation_id)['phase']
    def authorize(action='write'):return renew_claim(db,claim,lease_seconds=max(lease_seconds,900) if phase=='certificate' else lease_seconds,resolve_principal=resolve_principal,action=action)
    try:
        definition=authorize();operation=_operation(db,claim.operation_id);route_id=operation['route_id']
        reconcile=bool(journal.get('pending_effect'))
        if phase=='dns-preview':
            evidence=cloudflare_dns.preview(db,route_id,key_path,transport=provider_transport,authorize=authorize)
            journal['dns_preview']=evidence
            _commit(db,claim,journal,phase='gateway' if not evidence['adoption_required'] else phase,state='queued' if not evidence['adoption_required'] else 'awaiting-adoption',resolve_principal=resolve_principal)
            return True
        if phase=='awaiting-tls':
            capabilities=host_client.call('capabilities',{})
            if not isinstance(capabilities,dict) or capabilities.get('schema')!=1 or capabilities.get('http') is not True or type(capabilities.get('tls')) is not bool:raise ValueError('Host capabilities are invalid.')
            _commit(db,claim,journal,phase='certificate' if capabilities['tls'] else 'awaiting-tls',state='queued' if capabilities['tls'] else 'awaiting-tls',resolve_principal=resolve_principal)
            return True
        if phase=='certificate':
            # Existing exact intent is mandatory: replay performs provider reads only.
            if not db.execute('SELECT 1 FROM ingress_dns_intents WHERE operation_id=? AND route_id=?',(claim.operation_id,route_id)).fetchone() or not db.execute('SELECT 1 FROM ingress_dns_ownership WHERE operation_id=? AND route_id=?',(claim.operation_id,route_id)).fetchone():raise ValueError('Owned DNS evidence is required before issuance.')
            adoption=journal.get('adoption') or {}
            fresh=cloudflare_dns.apply(db,route_id,claim.operation_id,key_path,preview_digest=journal['dns_preview']['preview_digest'],adopt_record_id=adoption.get('record_id'),transport=provider_transport,authorize=authorize)
            if fresh.get('dns')!='verified' or fresh.get('record_id')!=journal['dns_verified']['record_id']:raise ValueError('Owned DNS changed before issuance.')
            journal['dns_verified']['observed']=time.time()
        if phase=='dns':
            # Recheck fresh host/gateway identity immediately before DNS publication.
            verified=host_client.call('verify',{'route_id':route_id})
            if not isinstance(verified,dict) or verified.get('route_id')!=route_id or verified.get('operation_id')!=claim.operation_id or verified.get('digest')!=journal['stage']['digest'] or verified.get('status')!='active':raise ValueError('Host HTTP identity is unverified.')
            authorize('adopt' if journal.get('adoption') else 'write')
        journal['pending_effect']=phase
        _commit(db,claim,journal,resolve_principal=resolve_principal)
        authorize('adopt' if phase=='dns' and journal.get('adoption') else 'write')
        if phase=='gateway':
            binding=gateway(db,definition,claim.operation_id,authorize=authorize,reconcile=reconcile)
            if not isinstance(binding,dict) or not re.fullmatch('[a-f0-9]{64}',str(binding.get('container_id',''))) or type(binding.get('port')) is not int:raise ValueError('Verified gateway binding is required.')
            journal['gateway']={key:binding[key] for key in ('container_id','port')}
        elif phase=='grant':
            grant=host_client.call('issue_grant',{'route_id':route_id,'operation_id':claim.operation_id,'expected_revision':0})
            if not isinstance(grant,dict) or grant.get('route_id')!=route_id or grant.get('operation_id')!=claim.operation_id or grant.get('status')!='granted':raise ValueError('Host grant was not verified.')
            journal['grant']=grant
        elif phase=='stage':
            stage=host_client.call('stage_http',{'route_id':route_id,'operation_id':claim.operation_id,'expected_digest':None})
            if not isinstance(stage,dict) or stage.get('route_id')!=route_id or stage.get('operation_id')!=claim.operation_id or stage.get('status') not in ('staged','active') or not re.fullmatch('[a-f0-9]{64}',str(stage.get('digest',''))):raise ValueError('Host stage identity is unavailable.')
            journal['stage']=stage
        elif phase=='activate':
            if reconcile:
                observed=host_client.call('inspect',{'route_id':route_id})
                if not isinstance(observed,dict) or observed.get('operation_id')!=claim.operation_id or observed.get('digest')!=journal['stage']['digest']:raise ValueError('Host recovery identity changed.')
                if observed.get('status')=='staged':
                    host_client.call('activate_http',{'route_id':route_id,'operation_id':claim.operation_id,'expected_digest':None,'digest':journal['stage']['digest']})
                elif observed.get('status')!='active':host_client.call('recover',{'route_id':route_id})
            else:
                host_client.call('activate_http',{'route_id':route_id,'operation_id':claim.operation_id,'expected_digest':None,'digest':journal['stage']['digest']})
            verified=host_client.call('verify',{'route_id':route_id})
            if not isinstance(verified,dict) or verified.get('route_id')!=route_id or verified.get('operation_id')!=claim.operation_id or verified.get('digest')!=journal['stage']['digest'] or verified.get('status')!='active':raise ValueError('Host HTTP identity is unverified.')
            journal['http_verified']={'digest':verified['digest'],'observed':time.time()}
        elif phase=='dns':
            adoption=journal.get('adoption') or {}
            result=cloudflare_dns.apply(db,route_id,claim.operation_id,key_path,preview_digest=journal['dns_preview']['preview_digest'],adopt_record_id=adoption.get('record_id'),transport=provider_transport,authorize=authorize)
            if result.get('dns')!='verified' or result.get('route_id')!=route_id:raise ValueError('DNS ownership is unverified.')
            journal['dns_verified']={'record_id':result['record_id'],'observed':time.time()}
        elif phase=='certificate':
            result=host_client.call('request_certificate',{'route_id':route_id,'operation_id':claim.operation_id})
            _certificate_evidence(result,route_id,claim.operation_id,definition['hostname'])
            journal['certificate']=result
        elif phase=='stage-tls':
            cert=journal['certificate'];_certificate_evidence(cert,route_id,claim.operation_id,definition['hostname'])
            result=host_client.call('stage_tls',{'route_id':route_id,'operation_id':claim.operation_id,'expected_digest':None,'generation':cert['generation'],'fingerprint':cert['fingerprint']})
            _tls_evidence(result,route_id,claim.operation_id,cert,('staged','active'))
            journal['tls_stage']=result
        elif phase=='activate-tls':
            identity={'route_id':route_id,'operation_id':claim.operation_id};cert=journal['certificate'];stage=journal['tls_stage']
            if reconcile:
                observed=host_client.call('inspect_tls',identity)
                _tls_evidence(observed,route_id,claim.operation_id,cert,('staged','active','reload-intent','installing','restoring-file'))
                if observed['digest']!=stage['digest']:raise ValueError('TLS recovery configuration changed.')
                if observed['status'] not in ('staged','active'):
                    observed=host_client.call('recover_tls',identity)
                    _tls_evidence(observed,route_id,claim.operation_id,cert,('staged','active'))
                    if observed['digest']!=stage['digest']:raise ValueError('TLS recovered configuration changed.')
                if observed['status']=='staged':host_client.call('activate_tls',dict(identity,digest=stage['digest']))
            else:host_client.call('activate_tls',dict(identity,digest=stage['digest']))
            verified=host_client.call('verify_tls',identity)
            _tls_evidence(verified,route_id,claim.operation_id,cert,('active',))
            if verified['digest']!=stage['digest']:raise ValueError('TLS served configuration changed.')
            journal['tls_verified']={'digest':verified['digest'],'generation':cert['generation'],'fingerprint':cert['fingerprint'],'not_after':cert['not_after'],'observed':time.time()}
        elif phase=='origin-verified':
            journal.pop('pending_effect',None);journal.pop('recovery_requested',None)
            _commit(db,claim,journal,state='awaiting-external',resolve_principal=resolve_principal)
            return True
        else:raise ValueError('Domain phase is unavailable.')
        journal.pop('pending_effect',None);journal.pop('recovery_requested',None)
        next_phase=PHASES[PHASES.index(phase)+1]
        if phase=='dns':
            capabilities=host_client.call('capabilities',{})
            if not isinstance(capabilities,dict) or capabilities.get('schema')!=1 or capabilities.get('http') is not True or type(capabilities.get('tls')) is not bool:raise ValueError('Host capabilities are invalid.')
            if capabilities['tls']:next_phase='certificate'
        _commit(db,claim,journal,phase=next_phase,state='awaiting-tls' if next_phase=='awaiting-tls' else 'queued',resolve_principal=resolve_principal)
    except Exception:
        # Retain the pre-effect intent even if the response or commit was lost.
        persisted=json.loads(_row(db,claim.operation_id)['journal'])
        try:_commit(db,claim,persisted,state='reconciliation-required' if persisted.get('pending_effect') else 'failed',error_code='phase_unverified',resolve_principal=resolve_principal,authorize=False)
        except ValueError:pass  # A stale worker cannot change the new lease's state.
    return True
