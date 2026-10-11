"""Trusted worker coordination; caller serializes with the deployment lock.

No database transaction spans Docker or a helper exchange. Unknown apply outcomes
remain fenced; reconciliation never replays mutation commands.
"""
import math
import re
import time
import registry
import migration_jobs as jobs
import migration_credentials
from database_target import resolve_ownership,check_registered


def _outcome(db,job_id,state,result):
    with registry.transaction(db):
        current=jobs.authorize(db,job_id)
        if current['state'] not in ('applying','recovery-required','verified'):raise ValueError('Migration outcome has no execution intent.')
        evidence={'status':result['status'],'proof_digest':result['proof_digest'],'observed_at':result['observed_at']}
        db.execute('UPDATE job_migrations SET state=?,evidence=?,updated=? WHERE job_id=?',(state,registry.canonical(evidence),time.time(),job_id))


def process(db,job_id,*,helper,resolve_candidate,key_path,authorize):
    if db.in_transaction:raise ValueError('Migration helper cannot run inside a state transaction.')
    record=jobs.load(db,job_id)
    if record['target']['binding'] is None:return True
    if helper.image!=record['target']['helper_image']:raise ValueError('Migration helper image changed after enqueue.')
    authorize();record=jobs.authorize(db,job_id)
    if record['state'] in ('captured','failed'):raise ValueError('Migration source or recovery outcome is not eligible for execution.')
    captured=record['target']['binding'];definition=captured['definition']
    candidate=resolve_ownership(captured,resolve_candidate=resolve_candidate);check_registered(db,candidate)
    expected=record['proof']['candidate'] if record['proof'] is not None else candidate
    if candidate!=expected:raise ValueError('Migration database identity changed.')
    def guard():
        authorize();jobs.authorize(db,job_id)
        current=resolve_ownership(captured,resolve_candidate=resolve_candidate)
        check_registered(db,current)
        if current!=expected:raise ValueError('Migration database identity changed.')
    def revalidate(identifier):
        if identifier!=expected['container_id']:raise ValueError('Migration namespace changed.')
        guard()
    secret=migration_credentials.credential_for_use(db,captured['project'],captured['environment'],definition['secret_id'],definition['secret_revision'],key_path)
    request={'protocol_version':1,'engine':'postgresql','framework':definition['framework'],
        'grammar':definition['grammar'] or 'alembic-postgresql-v1','database':definition['database_name'],
        'user':secret['username'],'password':secret['password'],'port':definition['database_port'],'sources':record['sources']}
    def invoke(operation,proof=None):
        payload=dict(request,operation=operation)
        if proof is not None:payload['proof']=proof
        return helper.invoke(expected['container_id'],payload,revalidate=revalidate,authorize=guard)
    if record['state']=='source-ready':
        result=invoke('inspect_plan')
        if (not isinstance(result,dict) or result.get('status')!='planned' or not isinstance(result.get('proof'),dict)
                or not isinstance(result['proof'].get('digest'),str) or not re.fullmatch('[a-f0-9]{64}',result['proof']['digest'])):raise ValueError('Migration plan evidence is invalid.')
        jobs.record_plan(db,job_id,{'candidate':expected,'helper_proof':result['proof']})
        record=jobs.load(db,job_id)
    proof=record['proof']['helper_proof']
    if record['state']=='planned':
        guard();jobs.begin_apply(db,job_id)
        try:
            result=invoke('apply',proof)
            if not isinstance(result,dict) or result.get('status')!='committed' or result.get('proof_digest')!=proof['digest']:raise ValueError('Migration commit evidence is uncertain.')
        except Exception:
            jobs.mark_uncertain(db,job_id)
            raise ValueError('Migration execution requires reconciliation.') from None
    try:
        result=invoke('reconcile',proof)
        if (not isinstance(result,dict) or result.get('status') not in ('committed','not_applied','reconciliation_required')
                or result.get('proof_digest')!=proof['digest'] or type(result.get('observed_at')) not in (int,float)
                or not math.isfinite(result['observed_at']) or result['observed_at']<=0):raise ValueError('Migration reconciliation evidence is invalid.')
        guard()
        if result['status']=='committed':_outcome(db,job_id,'verified',result);return True
        if result['status']=='not_applied':_outcome(db,job_id,'failed',result);return False
        if jobs.load(db,job_id)['state']=='verified':
            with registry.transaction(db):db.execute("UPDATE job_migrations SET state='recovery-required',updated=? WHERE job_id=?",(time.time(),job_id))
        else:jobs.mark_uncertain(db,job_id)
        return False
    except Exception:
        current=jobs.load(db,job_id)
        if current['state'] in ('applying','recovery-required'):jobs.mark_uncertain(db,job_id)
        elif current['state']=='verified':
            with registry.transaction(db):db.execute("UPDATE job_migrations SET state='recovery-required',updated=? WHERE job_id=?",(time.time(),job_id))
        raise ValueError('Migration outcome could not be reconciled.') from None
