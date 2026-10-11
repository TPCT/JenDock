import copy
import json
import time
import unittest
from unittest.mock import patch
import domain_execution as execution
import domain_renewal as renewal
import registry
import test_domain_execution_tls as fixtures


class RenewalTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DomainTLSTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.f.tls_until('origin-verified');self.f.step();self.db=self.f.db
        self.before=json.loads(execution._row(self.db,self.f.operation)['journal'])
        self.calls=[];self.lost_issue=False;self.lost_reload=False;self.noop=False;self.revoked=False;self.state='active';self.renewal_id=None
        self.candidate=dict(self.before['certificate'],generation='4'*64,fingerprint='5'*64,not_after=time.time()+90*86400)
    def resolver(self,db,operation):
        if self.revoked:raise ValueError('private revocation detail')
        return self.f.principal
    def call(self,method,request):
        self.assertFalse(self.db.in_transaction);self.calls.append((method,dict(request)))
        if method in ('renew_certificate','reconcile_certificate'):
            if method=='renew_certificate':
                self.renewal_id=request['renewal_id'];self.assertEqual(request['expected_generation'],self.before['certificate']['generation'])
                if self.lost_issue:self.lost_issue=False;raise TimeoutError()
            else:self.assertEqual(request['renewal_id'],self.renewal_id);self.assertNotIn('expected_generation',request)
            return dict(self.before['certificate'] if self.noop else self.candidate,renewal_id=request['renewal_id'])
        value={k:self.candidate[k] for k in ('route_id','operation_id','generation','fingerprint')};value['digest']='6'*64
        if method=='stage_tls':self.assertEqual(request['expected_digest'],self.before['tls_stage']['digest']);self.state='staged'
        if method=='activate_tls':
            self.state='active'
            if self.lost_reload:self.lost_reload=False;raise TimeoutError()
        if method=='recover_tls':self.state='active'
        return dict(value,status=self.state)
    def step(self):return renewal.process_once(self.db,worker_id='renew-worker',host_client=self,key_path='unused',resolve_principal=self.resolver,provider_transport=self.f.fixture.transport)
    def due(self):self.db.execute('UPDATE domain_renewals SET next_at=0')
    def count(self,name):return sum(method==name for method,_ in self.calls)
    def test_success_promotes_only_after_verified_activation(self):
        self.step();self.assertEqual(json.loads(execution._row(self.db,self.f.operation)['journal'])['certificate'],self.before['certificate'])
        self.step();self.step()
        journal=json.loads(execution._row(self.db,self.f.operation)['journal'])
        self.assertEqual(journal['certificate']['generation'],'4'*64);self.assertNotIn('external_verified',journal)
        self.assertEqual(execution._row(self.db,self.f.operation)['state'],'awaiting-external')
        self.assertEqual(renewal._row(self.db,self.f.operation)['state'],'complete');self.assertFalse(self.step())
        self.assertEqual(self.count('renew_certificate'),1);self.assertEqual(self.count('activate_tls'),1)
        self.assertEqual(len(self.f.fixture.writes),1)
    def test_lost_issue_reply_reconciles_same_attempt_without_reissue(self):
        self.lost_issue=True;self.step();attempt=renewal._row(self.db,self.f.operation)['renewal_id']
        self.assertFalse(self.step());self.due();self.step()
        self.assertEqual(renewal._row(self.db,self.f.operation)['renewal_id'],attempt)
        self.assertEqual(self.count('renew_certificate'),1);self.assertEqual(self.count('reconcile_certificate'),1)
    def test_lost_reload_reply_only_observes_active_candidate(self):
        self.step();self.step();self.lost_reload=True;self.step()
        self.assertEqual(json.loads(execution._row(self.db,self.f.operation)['journal'])['pending_effect'],'renewal')
        self.due();self.step();self.assertEqual(self.count('activate_tls'),1);self.assertEqual(self.count('inspect_tls'),1)
    def test_unchanged_generation_does_not_reload(self):
        self.noop=True;self.step()
        self.assertEqual(renewal._row(self.db,self.f.operation)['state'],'complete');self.assertEqual(self.count('stage_tls'),0)
        self.assertEqual(json.loads(execution._row(self.db,self.f.operation)['journal'])['certificate'],self.before['certificate'])
    def test_revocation_preserves_serving_certificate_and_backs_off(self):
        self.revoked=True;self.step();self.assertEqual(self.calls,[])
        row=renewal._row(self.db,self.f.operation);self.assertEqual(row['error_code'],'renewal_unverified');self.assertGreater(row['next_at'],time.time())
        self.assertEqual(json.loads(execution._row(self.db,self.f.operation)['journal'])['certificate'],self.before['certificate'])
    def test_expired_lease_preserves_issue_intent_and_changes_generation(self):
        renewal.schedule(self.db);claim=renewal.claim(self.db,'old')
        document=json.loads(renewal._row(self.db,self.f.operation)['document']);renewal._intent(self.db,claim,document,'issue')
        self.renewal_id=renewal._row(self.db,self.f.operation)['renewal_id']
        self.db.execute('UPDATE domain_renewals SET lease_expires=0')
        self.step();self.assertEqual(self.count('renew_certificate'),0);self.assertEqual(self.count('reconcile_certificate'),1)
        with self.assertRaises(ValueError):renewal._owned(self.db,claim)
    def test_foreign_certificate_never_stages(self):
        self.candidate['operation_id']='foreign';self.step();self.assertEqual(self.count('stage_tls'),0)
        self.assertEqual(renewal._row(self.db,self.f.operation)['state'],'queued')

    def test_expired_owned_certificate_can_renew_but_is_not_ready(self):
        journal=json.loads(execution._row(self.db,self.f.operation)['journal'])
        journal['certificate']['not_after']=journal['tls_verified']['not_after']=time.time()-10
        self.db.execute('UPDATE domain_executions SET journal=?',(registry.canonical(journal),))
        self.before=copy.deepcopy(journal)
        self.assertFalse(execution.public_status(self.db,self.f.operation)['ready'])
        self.step();self.step();self.step()
        self.assertEqual(renewal._row(self.db,self.f.operation)['state'],'complete')
        self.assertEqual(self.count('renew_certificate'),1)
    def test_unknown_issuance_keeps_same_attempt_with_bounded_backoff(self):
        self.lost_issue=True;self.step();attempt=renewal._row(self.db,self.f.operation)['renewal_id']
        original=self.call
        def unknown(method,request):
            if method=='reconcile_certificate':
                self.calls.append((method,request));return dict(request,status='reconciliation-required')
            return original(method,request)
        self.call=unknown
        for _ in range(4):self.due();self.step()
        row=renewal._row(self.db,self.f.operation)
        self.assertEqual(row['renewal_id'],attempt);self.assertEqual(self.count('renew_certificate'),1)
        self.assertGreater(row['next_at']-time.time(),900);self.assertLessEqual(row['next_at']-time.time(),3600)
        self.assertEqual(json.loads(execution._row(self.db,self.f.operation)['journal'])['certificate'],self.before['certificate'])

    def test_only_proven_ended_failure_rotates_attempt_with_history(self):
        self.lost_issue=True;self.step();old=renewal._row(self.db,self.f.operation)['renewal_id']
        original=self.call
        def ended(method,request):
            if method=='reconcile_certificate':
                self.calls.append((method,request));return dict(request,status='reconciliation-required',retryable=True,retry_after=time.time()-1)
            return original(method,request)
        self.call=ended;self.due();self.step()
        row=renewal._row(self.db,self.f.operation)
        self.assertNotEqual(row['renewal_id'],old);self.assertEqual(self.count('renew_certificate'),1)
        self.assertEqual(self.db.execute('SELECT state FROM domain_renewal_history WHERE renewal_id=?',(old,)).fetchone()[0],'failed-ended')
        self.call=original;self.due();self.step();self.assertEqual(self.count('renew_certificate'),2)
    def test_retry_deadline_in_future_does_not_rotate_attempt(self):
        self.lost_issue=True;self.step();old=renewal._row(self.db,self.f.operation)['renewal_id']
        self.call=lambda method,request:dict(request,status='reconciliation-required',retryable=True,retry_after=time.time()+300)
        self.due();self.step();self.assertEqual(renewal._row(self.db,self.f.operation)['renewal_id'],old)

    def test_crash_before_request_arrives_replays_same_attempt_only_after_absent(self):
        renewal.schedule(self.db);claim=renewal.claim(self.db,'crashed-before-send')
        row=renewal._row(self.db,self.f.operation);attempt=row['renewal_id']
        renewal._intent(self.db,claim,json.loads(row['document']),'issue')
        self.db.execute('UPDATE domain_renewals SET lease_expires=0')
        original=self.call
        def absent(method,request):
            if method=='reconcile_certificate':
                self.calls.append((method,dict(request)));return dict(request,status='absent')
            return original(method,request)
        self.call=absent;self.step()
        self.assertEqual(self.count('reconcile_certificate'),1);self.assertEqual(self.count('renew_certificate'),1)
        self.assertEqual(renewal._row(self.db,self.f.operation)['renewal_id'],attempt)
        sent=next(request for method,request in self.calls if method=='renew_certificate')
        self.assertEqual(sent['renewal_id'],attempt)
        self.assertEqual(sent['expected_generation'],self.before['certificate']['generation'])
        self.assertEqual(renewal._row(self.db,self.f.operation)['phase'],'stage')
    def test_foreign_absent_identity_never_resends(self):
        self.lost_issue=True;self.step();attempt=renewal._row(self.db,self.f.operation)['renewal_id']
        for field in ('route_id','operation_id','renewal_id'):
            def absent(method,request):
                self.calls.append((method,dict(request)));return dict(request,status='absent',**{field:'foreign'})
            self.call=absent;self.due();self.step()
            self.assertEqual(self.count('renew_certificate'),1)
            self.assertEqual(renewal._row(self.db,self.f.operation)['renewal_id'],attempt)

    def test_scheduler_isolates_corrupt_and_retired_records(self):
        source=execution._row(self.db,self.f.operation)
        for operation,journal in [('000-corrupt','{broken'),('001-retired',source['journal'])]:
            self.db.execute('INSERT INTO domain_executions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (operation,source['definition_id'],source['definition_digest'],'external','ready',None,0,None,journal,None,0))
        renewal.schedule(self.db)
        self.assertIsNotNone(renewal._row(self.db,self.f.operation))
        self.assertEqual(self.db.execute('SELECT count(*) FROM domain_renewals').fetchone()[0],1)
    def test_scheduler_pages_past_one_hundred_nondue_records(self):
        source=execution._row(self.db,self.f.operation);journal=json.loads(source['journal'])
        journal['certificate']['not_after']=time.time()+100*86400
        for index in range(100):
            self.db.execute('INSERT INTO domain_executions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                ('000-'+str(index).zfill(4),source['definition_id'],source['definition_digest'],'external','ready',None,0,None,registry.canonical(journal),None,0))
        renewal.schedule(self.db)
        self.assertEqual(self.db.execute('SELECT count(*) FROM domain_renewals').fetchone()[0],0)
        renewal.schedule(self.db)
        self.assertIsNotNone(renewal._row(self.db,self.f.operation))
    def test_scheduler_does_not_swallow_storage_errors(self):
        import sqlite3
        with patch.object(execution,'_operation',side_effect=sqlite3.OperationalError('storage fault')):
            with self.assertRaises(sqlite3.OperationalError):renewal.schedule(self.db)
