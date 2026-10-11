"""Changed generation promotion against actual host TLS state transitions.

Certificate material/probes are fixture capabilities; no Apache/ACME effects.
"""
import json
import time
import unittest
import domain_execution
import domain_renewal
import test_domain_execution_tls_review as fixtures


class RenewalHostContractTests(unittest.TestCase):
    def setUp(self):
        self.host=fixtures.DomainTLSContractReviewTests();self.host.setUp();self.addCleanup(self.host.doCleanups)
        self.host.until('origin-verified');self.host.f.step();self.f=self.host.f;self.db=self.f.db
        self.old=json.loads(domain_execution._row(self.db,self.f.operation)['journal'])
        original_files=dict(self.host.cert.generation_files.return_value)
        self.next=dict(self.old['certificate'],generation='4'*64,fingerprint='5'*64,not_after=time.time()+90*86400)
        def files(route,generation):
            if generation==self.next['generation']:return dict(original_files,**self.next)
            return dict(original_files)
        self.host.cert.generation_files.side_effect=files
        self.methods=[];self.lost=False
        def call(method,request):
            self.assertFalse(self.db.in_transaction);self.methods.append(method)
            if method=='renew_certificate':return dict(self.next,renewal_id=request['renewal_id'])
            result=self.f.call(method,request)
            if method=='activate_tls' and self.lost:self.lost=False;raise TimeoutError('lost actual activation result')
            return result
        self.call=call
    def step(self):
        return domain_renewal.process_once(self.db,worker_id='renew-contract',host_client=self,key_path='unused',
            resolve_principal=self.f.resolver,provider_transport=self.f.fixture.transport)
    def test_actual_host_changed_generation_promotes_same_owner(self):
        for _ in range(3):self.step()
        journal=json.loads(domain_execution._row(self.db,self.f.operation)['journal'])
        self.assertEqual(journal['certificate']['generation'],self.next['generation'])
        self.assertEqual(journal['certificate']['operation_id'],self.f.operation)
        self.assertEqual(journal['tls_stage']['status'],'staged')
        self.assertEqual(self.host.executor.calls.count('reload'),3)
        self.assertEqual(domain_renewal._row(self.db,self.f.operation)['state'],'complete')
        actual=self.host.tls.verify_tls({'route_id':self.f.fixture.route,'operation_id':self.f.operation})
        self.assertEqual(actual['generation'],self.next['generation']);self.assertEqual(actual['digest'],journal['tls_stage']['digest'])
    def test_actual_host_lost_activation_reply_reconciles_without_second_reload(self):
        self.step();self.step();self.lost=True;self.step()
        self.assertEqual(self.host.executor.calls.count('reload'),3)
        self.db.execute('UPDATE domain_renewals SET next_at=0');self.step()
        self.assertEqual(self.host.executor.calls.count('reload'),3)
        self.assertEqual(self.methods.count('activate_tls'),1);self.assertEqual(self.methods.count('inspect_tls'),1)
        self.assertEqual(domain_renewal._row(self.db,self.f.operation)['state'],'complete')
