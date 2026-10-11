import hashlib
import json
import time
import unittest
from types import SimpleNamespace
import domain_execution as execution
import test_domain_execution_tls as tls


class ExternalFlowTests(unittest.TestCase):
    setUp=tls.DomainTLSTests.setUp
    tearDown=tls.DomainTLSTests.tearDown
    resolver=tls.DomainTLSTests.resolver
    call=tls.DomainTLSTests.call
    status=tls.DomainTLSTests.status
    gateway=tls.DomainTLSTests.gateway
    step=tls.DomainTLSTests.step
    tls_until=tls.DomainTLSTests.tls_until

    def prepare(self):
        self.tls_until('origin-verified');self.step()
        self.probes=0;self.change_backend=False;self.fail_probe=False
        def readiness(db,definition,*,authorize):
            self.assertFalse(db.in_transaction);authorize();self.probes+=1
            return {'gateway_id':'e'*64,'backend_id':('a' if self.change_backend and self.probes%2==0 else 'b')*64,'proof_token':'c'*64,'observed_at':time.time()}
        self.observer=SimpleNamespace(readiness=readiness)
    def verify(self,host,token):
        self.assertFalse(self.db.in_transaction)
        if self.fail_probe:raise TimeoutError('private connection detail')
        return {'hostname':host,'status':'verified','observed_at':time.time(),'proof_digest':hashlib.sha256(token.encode()).hexdigest()}
    def external_step(self):
        return execution.process_once(self.db,worker_id='external-worker',gateway=self.observer,host_client=self,
            key_path='unused',resolve_principal=self.resolver,provider_transport=self.fixture.transport,external_verifier=self.verify)
    def test_automatic_read_only_external_check_records_bundle(self):
        self.prepare();before=len(self.fixture.writes);commands=len(self.calls)
        self.external_step()
        row=execution._row(self.db,self.operation);journal=json.loads(row['journal'])
        self.assertEqual(row['state'],'ready');self.assertEqual(self.probes,2)
        self.assertEqual(len(self.fixture.writes),before)
        self.assertEqual([name for name,_ in self.calls[commands:]],['verify_tls','verify_tls'])
        self.assertNotIn('pending_effect',journal);self.assertLessEqual(journal['external_verified']['expires_at']-time.time(),60)
        self.assertFalse(self.external_step())
    def test_backend_change_during_probe_does_not_become_ready(self):
        self.prepare();self.change_backend=True;self.external_step()
        self.assertEqual(self.status()['status'],'awaiting-external');self.assertFalse(self.status()['ready'])
        self.assertNotIn('external_verified',json.loads(execution._row(self.db,self.operation)['journal']))
    def test_lost_probe_can_retry_without_repeating_writes_or_reload(self):
        self.prepare();self.fail_probe=True;self.external_step()
        status=self.status();self.assertEqual(status['error_code'],'external_unverified');self.assertNotIn('private',json.dumps(status))
        self.assertFalse(self.external_step())
        self.fail_probe=False
        execution.resume(self.db,self.principal,'app','development',self.operation);self.external_step()
        self.assertEqual(execution._row(self.db,self.operation)['state'],'ready')
        self.assertEqual(len(self.fixture.writes),1)
        self.assertEqual(sum(name=='activate_tls' for name,_ in self.calls),1)
    def test_existing_dns_ownership_required_before_public_request(self):
        self.prepare();self.fixture.records[0]['comment']='foreign';self.external_step()
        self.assertEqual(self.probes,0);self.assertEqual(self.status()['status'],'awaiting-external')

    def test_missing_dns_intent_never_recreates_or_updates_record(self):
        self.prepare();self.db.execute('DELETE FROM ingress_dns_intents');before=len(self.fixture.writes)
        self.external_step()
        self.assertEqual(self.probes,0);self.assertEqual(len(self.fixture.writes),before)
        self.assertEqual(execution._row(self.db,self.operation)['state'],'awaiting-external')
