import json
import time
import unittest
import domain_execution as execution
import test_domain_execution as base


class DomainTLSTests(unittest.TestCase):
    tearDown=base.DomainExecutionTests.tearDown
    resolver=base.DomainExecutionTests.resolver
    gateway=base.DomainExecutionTests.gateway
    step=base.DomainExecutionTests.step
    status=base.DomainExecutionTests.status
    def setUp(self):
        base.DomainExecutionTests.setUp(self);self.tls_enabled=True;self.tls_state='absent';self.cert_issued=False;self.lose_certificate=False;self.lose_tls=False
    def call(self,method,request):
        if method=='capabilities':
            self.calls.append((method,request));return {'schema':1,'http':True,'tls':self.tls_enabled}
        if method in ('request_certificate','stage_tls','activate_tls','inspect_tls','recover_tls','verify_tls'):
            self.assertFalse(self.db.in_transaction);self.calls.append((method,request))
            evidence_base={'route_id':self.fixture.route,'operation_id':self.operation,'generation':'1'*64,'fingerprint':'2'*64,'digest':'3'*64}
            if method=='request_certificate':
                self.assertGreater(execution._row(self.db,self.operation)['lease_expires']-time.time(),850)
                self.cert_issued=True
                if self.lose_certificate:self.lose_certificate=False;raise TimeoutError()
                return dict(evidence_base,status='verified',sans=['dev.example.test'],not_after=time.time()+3600)
            if method=='stage_tls':self.tls_state='staged'
            if method=='activate_tls':
                self.tls_state='reload-intent' if self.lose_tls else 'active'
                if self.lose_tls:raise TimeoutError()
            if method=='recover_tls':self.tls_state='active'
            return dict(evidence_base,status=self.tls_state)
        return base.DomainExecutionTests.call(self,method,request)
    def tls_until(self,phase):
        for _ in range(15):
            if self.status()['phase']==phase:return
            self.step()
        self.fail(self.status())
    # Base HTTP-only cases are exercised by their own class.
    def test_tls_progress_is_not_public_ready(self):
        self.tls_until('origin-verified');self.step();status=self.status()
        self.assertEqual(status['status'],'awaiting-external');self.assertFalse(status['ready'])
        self.assertEqual(status['tls']['status'],'origin-verified');self.assertFalse(status['tls']['live_checked'])
        self.assertEqual(len(self.fixture.writes),1)
    def test_tls_lost_reload_never_repeats_activation(self):
        self.tls_until('activate-tls');self.lose_tls=True;self.step()
        self.assertEqual(self.status()['status'],'reconciliation-required')
        execution.resume(self.db,self.principal,'app','development',self.operation);self.step()
        self.assertEqual(self.status()['phase'],'origin-verified')
        self.assertEqual(sum(n=='activate_tls' for n,_ in self.calls),1)
        self.assertEqual(sum(n=='recover_tls' for n,_ in self.calls),1)
    def test_http_only_operation_resumes_same_identity_when_tls_enabled(self):
        self.tls_enabled=False
        for _ in range(6):self.step()
        self.assertEqual(self.status()['status'],'awaiting-tls')
        self.tls_enabled=True;execution.resume(self.db,self.principal,'app','development',self.operation);self.step()
        self.assertEqual(self.status()['phase'],'certificate');self.assertEqual(len(self.fixture.writes),1)
    def test_certificate_dns_drift_blocks_before_host_effect(self):
        self.tls_until('certificate');self.fixture.records[0]['comment']='foreign'
        self.step();self.assertFalse(self.cert_issued);self.assertEqual(len(self.fixture.writes),1)
    def test_lost_certificate_reply_reenters_idempotent_host_request(self):
        self.tls_until('certificate');self.lose_certificate=True;self.step()
        self.assertEqual(self.status()['status'],'reconciliation-required')
        execution.resume(self.db,self.principal,'app','development',self.operation);self.step()
        self.assertEqual(self.status()['phase'],'stage-tls');self.assertEqual(len(self.fixture.writes),1)


    def test_foreign_certificate_identity_stops_before_stage(self):
        self.tls_until('certificate');original=self.call
        def foreign(method,request):
            result=original(method,request)
            if method=='request_certificate':result['sans']=['other.example.test']
            return result
        self.call=foreign;self.step()
        self.assertEqual(self.status()['status'],'reconciliation-required')
        self.assertFalse(any(name=='stage_tls' for name,_ in self.calls))
    def test_expired_tls_projection_never_claims_current_or_ready(self):
        self.tls_until('origin-verified');self.step()
        journal=json.loads(execution._row(self.db,self.operation)['journal'])
        self.assertNotIn('pending_effect',journal)
        journal['certificate']['not_after']=journal['tls_verified']['not_after']=time.time()-1
        self.db.execute('UPDATE domain_executions SET journal=?',(json.dumps(journal),))
        self.assertEqual(self.status()['tls']['status'],'expired');self.assertFalse(self.status()['ready'])
