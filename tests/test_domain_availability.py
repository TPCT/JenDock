import json
import time
import unittest
from unittest.mock import patch
import ingress
import registry
import domain_execution
import test_ingress as fixtures
from store import snapshot

class DomainAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.IngressTests();self.fixture.setUp();self.db=self.fixture.db
        self.env=patch.dict('os.environ',{'INGRESS_ENABLED':'1'});self.env.start()
    def tearDown(self):self.env.stop();self.fixture.tearDown()
    def observe(self,state='available',configured=True):snapshot(self.db,'ingress-worker',{'state':state,'configured':configured})
    def test_enabled_without_worker_observation_is_not_available(self):
        self.assertFalse(ingress.executor_status(self.db)['executor_available'])
        self.assertEqual(ingress.executor_status(self.db)['executor']['state'],'unobserved')
        with self.assertRaisesRegex(ValueError,'worker'):ingress.accept(self.db,self.fixture.principal,'app','development',self.fixture.accepted_values(),'domain-request-availability',enqueue_execution=True)
    def test_fresh_configured_observation_and_stale_malformed_fail_closed(self):
        self.observe();self.assertTrue(ingress.executor_status(self.db)['executor_available'])
        for updated in (time.time()-100,time.time()+10,'bad'):
            self.db.execute('UPDATE snapshots SET updated=? WHERE name=?',(updated,'ingress-worker'))
            self.assertFalse(ingress.executor_status(self.db)['executor_available'])
        for value in ({'state':'available','configured':1},{'state':'available','configured':False},[],{'state':'bogus','configured':True}):
            snapshot(self.db,'ingress-worker',value);self.assertFalse(ingress.executor_status(self.db)['executor_available'])
        self.db.execute("UPDATE snapshots SET value='not-json'");self.assertFalse(ingress.executor_status(self.db)['executor_available'])
    def test_existing_accepted_replay_survives_stale_worker_and_disablement(self):
        self.observe();values=self.fixture.accepted_values()
        accepted=ingress.accept(self.db,self.fixture.principal,'app','development',values,'domain-request-availability',enqueue_execution=True)
        self.db.execute('UPDATE snapshots SET updated=0')
        with patch.dict('os.environ',{'INGRESS_ENABLED':'0'}):
            replay=ingress.accept(self.db,self.fixture.principal,'app','development',values,'domain-request-availability',enqueue_execution=True)
            self.assertEqual(replay['operation']['id'],accepted['operation']['id']);self.assertFalse(replay['operation']['executor_available'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM domain_executions').fetchone()[0],1)
    def test_unavailable_observation_does_not_rewrite_execution_state(self):
        self.observe();values=self.fixture.accepted_values()
        accepted=ingress.accept(self.db,self.fixture.principal,'app','development',values,'domain-request-availability',enqueue_execution=True)
        self.observe('unavailable',False)
        status=domain_execution.public_status(self.db,accepted['operation']['id'])
        self.assertEqual(status['status'],'queued');self.assertFalse(status['executor_available'])

class DomainWorkerAvailabilityTests(unittest.TestCase):
    setUp=DomainAvailabilityTests.setUp
    tearDown=DomainAvailabilityTests.tearDown
    def test_worker_configuration_failure_and_disabled_are_observed(self):
        import worker
        with patch('domain_worker.configuration',side_effect=ValueError('fixture secret')):
            with self.assertRaisesRegex(ValueError,'configuration'):worker.process_domain_once(self.db)
        status=ingress.executor_status(self.db)
        self.assertFalse(status['executor_available']);self.assertFalse(status['executor']['configured'])
        self.assertNotIn('secret',json.dumps(status))
        with patch('domain_worker.configuration',return_value=None):self.assertFalse(worker.process_domain_once(self.db))
        self.assertEqual(ingress.executor_status(self.db)['executor']['state'],'disabled')
    def test_probe_is_bounded_cached_and_configuration_change_invalidates(self):
        import worker
        from unittest.mock import Mock
        client=Mock();client.call.return_value={'ports':[12001],'observed':time.time()}
        settings={'image':'fixture','socket':'/fixture.sock'}
        worker.observe_domain_executor(self.db,settings,client)
        worker.observe_domain_executor(self.db,settings,client)
        self.assertEqual(client.call.call_count,1);self.assertTrue(ingress.executor_status(self.db)['executor_available'])
        worker.observe_domain_executor(self.db,dict(settings,image='replacement'),client)
        self.assertEqual(client.call.call_count,2)
        self.db.execute('UPDATE snapshots SET updated=0')
        client.call.side_effect=TimeoutError('fixture secret')
        with self.assertRaises(ValueError):worker.observe_domain_executor(self.db,settings,client)
        self.assertFalse(ingress.executor_status(self.db)['executor_available'])
        self.assertTrue(ingress.executor_status(self.db)['executor']['configured'])
        count=client.call.call_count
        with self.assertRaises(ValueError):worker.observe_domain_executor(self.db,settings,client)
        self.assertEqual(client.call.call_count,count)
    def test_invalid_host_observation_cannot_publish_available(self):
        import worker
        from unittest.mock import Mock
        for evidence in ({'ports':[True],'observed':time.time()},{'ports':[],'observed':float('nan')},{'ports':[],'observed':time.time()+20},None):
            with self.subTest(evidence=evidence):
                client=Mock();client.call.return_value=evidence
                with self.assertRaises(ValueError):worker.observe_domain_executor(self.db,{},client)
                self.assertFalse(ingress.executor_status(self.db)['executor_available'])
