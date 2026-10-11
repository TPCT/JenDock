import dataclasses
import json
import unittest
from unittest.mock import patch
import domain_execution as execution
import registry
import test_cloudflare_dns as fixtures


class DomainExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.DNSOwnershipTests();self.fixture.setUp();self.db=self.fixture.db
        self.principal=self.fixture.fixture.principal
        self.operation=self.db.execute('SELECT id FROM ingress_operations').fetchone()[0]
        execution.enqueue(self.db,self.operation)
        self.calls=[];self.active=False;self.staged=False;self.lost=False;self.revoked=False
    def tearDown(self):self.fixture.tearDown()
    def resolver(self,db,operation):
        if self.revoked:raise ValueError('revoked fixture secret')
        return self.principal
    def gateway(self,db,definition,operation_id,*,authorize,reconcile):
        self.assertFalse(db.in_transaction);authorize();self.calls.append(('gateway',reconcile))
        return {'container_id':'e'*64,'port':12000}
    def call(self,method,request):
        self.assertFalse(self.db.in_transaction);self.calls.append((method,request))
        base={'route_id':self.fixture.route,'operation_id':self.operation,'digest':'f'*64}
        if method=='capabilities':return {'schema':1,'http':True,'tls':False}
        if method=='issue_grant':return dict(base,status='granted')
        if method=='stage_http':self.staged=True;return dict(base,status='staged')
        if method=='activate_http':
            self.active=True
            if self.lost:raise TimeoutError('secret host message')
        return dict(base,status='active' if self.active else 'staged')
    def step(self):return execution.process_once(self.db,worker_id='worker',gateway=self.gateway,host_client=self,key_path='unused',resolve_principal=self.resolver,provider_transport=self.fixture.transport)
    def status(self):return execution.public_status(self.db,self.operation)
    def until(self,phase):
        for _ in range(8):
            if self.status()['phase']==phase:return
            self.step()
        self.fail(self.status())
    def test_complete_phases_no_transaction_over_effect_and_tls_not_ready(self):
        for _ in range(6):self.assertTrue(self.step())
        status=self.status();self.assertEqual(status['status'],'awaiting-tls');self.assertFalse(status['ready'])
        self.assertEqual(status['dns']['status'],'verified');self.assertEqual(status['origin']['status'],'http-verified')
        self.assertEqual(status['tls']['status'],'unverified');self.assertFalse(self.step());self.assertEqual(len(self.fixture.writes),1)
    def test_expired_lease_never_restarts_and_old_generation_cannot_commit(self):
        claim=execution.claim_next(self.db,'old')
        self.db.execute('UPDATE domain_executions SET lease_expires=0')
        self.assertIsNone(execution.claim_next(self.db,'new'));self.assertEqual(self.status()['status'],'reconciliation-required')
        execution.resume(self.db,self.principal,'app','development',self.operation)
        current=execution.claim_next(self.db,'new');self.assertGreater(current.generation,claim.generation)
        with self.assertRaises(ValueError):execution._commit(self.db,claim,{},resolve_principal=self.resolver)
    def test_lost_activation_response_reconciles_without_second_reload(self):
        self.until('activate');self.lost=True;self.step()
        self.assertEqual(self.status()['status'],'reconciliation-required')
        execution.resume(self.db,self.principal,'app','development',self.operation);self.step()
        self.assertEqual(self.status()['phase'],'dns')
        self.assertEqual(sum(name=='activate_http' for name,_ in self.calls),1)
    def test_revoked_authority_fails_before_effect_and_sanitizes_error(self):
        self.until('gateway');self.revoked=True;self.step()
        self.assertEqual(self.status()['status'],'failed');self.assertEqual(self.calls,[])
        self.assertNotIn('secret',json.dumps(self.status()))
    def test_enqueue_participates_in_callers_transaction(self):
        self.db.execute('DELETE FROM domain_executions')
        with self.assertRaises(RuntimeError):
            with registry.transaction(self.db):
                execution.enqueue(self.db,self.operation);raise RuntimeError('rollback')
        self.assertEqual(self.db.execute('SELECT count(*) FROM domain_executions').fetchone()[0],0)
    def test_adoption_requires_scope_and_exact_preview(self):
        self.fixture.records=[self.fixture.foreign()];self.step()
        self.assertEqual(self.status()['status'],'awaiting-adoption');digest=self.status()['adoption_preview']['preview_digest']
        with self.assertRaises(ValueError):execution.approve_adoption(self.db,self.principal,'app','development',self.operation,'d'*32,digest)
        self.principal=dataclasses.replace(self.principal,scopes=self.principal.scopes|{'domains:adopt'})
        execution.approve_adoption(self.db,self.principal,'app','development',self.operation,'d'*32,digest)
        for _ in range(5):self.step()
        self.assertEqual(self.status()['status'],'awaiting-tls');self.assertEqual(self.fixture.writes[0][0],'PATCH')
    def test_dns_requires_exact_active_host_identity(self):
        self.until('dns');self.active=False;self.step()
        self.assertEqual(self.fixture.writes,[])
        self.assertNotEqual(self.status()['dns']['status'],'verified')

    def test_authority_revoked_after_gateway_effect_retains_intent(self):
        self.until('gateway')
        original=self.gateway
        def revoke(*args,**kwargs):
            result=original(*args,**kwargs);self.revoked=True;return result
        self.gateway=revoke;self.step()
        self.assertEqual(self.status()['status'],'reconciliation-required')
        self.revoked=False;self.gateway=original
        execution.resume(self.db,self.principal,'app','development',self.operation);self.step()
        self.assertEqual(self.calls[-1],('gateway',True))
        self.assertEqual(self.db.execute('SELECT event FROM domain_execution_events').fetchone()[0],'resume')
    def test_dns_write_lost_reply_is_reconciled_and_not_repeated(self):
        self.until('dns');self.fixture.lost=True;self.step()
        self.assertEqual(self.status()['status'],'awaiting-tls');self.assertEqual(len(self.fixture.writes),1)
    def test_resume_wrong_owner_or_environment_rejected(self):
        self.revoked=True;self.step()
        for principal,environment in ((dataclasses.replace(self.principal,subject_id='other'),'development'),(self.principal,'production')):
            with self.assertRaises(ValueError):execution.resume(self.db,principal,'app',environment,self.operation)
        self.assertEqual(self.status()['status'],'failed')

    def test_public_evidence_timestamps_require_exact_recorded_proof(self):
        for _ in range(6):self.step()
        status=self.status()
        for kind in ('dns','origin'):
            self.assertGreater(status[kind]['observed_at'],0);self.assertFalse(status[kind]['live_checked'])
        row=self.db.execute('SELECT journal FROM domain_executions').fetchone();journal=json.loads(row[0])
        journal['http_verified']['digest']='a'*64
        self.db.execute('UPDATE domain_executions SET journal=?',(registry.canonical(journal),))
        with self.assertRaises(ValueError):self.status()
        journal['http_verified']['digest']='f'*64
        self.db.execute('UPDATE domain_executions SET journal=?',(registry.canonical(journal),))
        self.db.execute('DELETE FROM ingress_dns_ownership')
        with self.assertRaises(ValueError):self.status()
