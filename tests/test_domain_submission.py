import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import ingress
import domain_execution
import test_domain_transports as transport


class DomainSubmissionTests(unittest.TestCase):
    def setUp(self):
        transport.DomainHTTPTests.setUp(self)
        from store import snapshot
        snapshot(self.fixture.db,'ingress-worker',{'state':'available','configured':True})
    tearDown=transport.DomainHTTPTests.tearDown
    post=transport.DomainHTTPTests.post
    action=transport.DomainHTTPTests.action

    def values(self):
        values=dict(self.fixture.values,hostname='second.example.test')
        preview=self.action('domain-preview',values=values)
        return dict(values,**{key:preview[key] for key in ingress.PINS})

    def test_authenticated_submission_persists_queue_and_replays_same_operation(self):
        with patch.dict(os.environ,{'INGRESS_ENABLED':'1'}):
            values=self.values()
            accepted=self.action('domain-create',values=values,request_id='domain-http-request-0001')
            operation=accepted['operation']
            self.assertEqual(operation['status'],'queued')
            self.assertFalse(operation['ready'])
            again=self.action('domain-create',values=values,request_id='domain-http-request-0001')
            self.assertEqual(again['operation']['id'],operation['id'])
            loaded=self.action('domain-operation',operation_id=operation['id'])['operation']
            self.assertEqual(loaded['phase'],'dns-preview')
            route=self.action('domain-get',route_id=accepted['route']['id'])['route']
            self.assertEqual(route['operation_id'],operation['id'])
            self.assertEqual(route['status'],'queued')
            self.assertEqual(self.fixture.db.execute('SELECT count(*) FROM domain_executions').fetchone()[0],1)

    def test_queue_failure_rolls_back_route_acceptance(self):
        with patch.dict(os.environ,{'INGRESS_ENABLED':'1'}):
            values=self.values()
            with patch.object(domain_execution,'enqueue',side_effect=ValueError('unavailable')):
                with self.assertRaises(HTTPError) as error:self.action('domain-create',values=values,request_id='domain-http-request-0002')
                self.assertEqual(error.exception.code,409);error.exception.close()
            self.assertEqual(self.fixture.db.execute('SELECT count(*) FROM ingress_routes').fetchone()[0],1)

    def test_disabled_execution_does_not_accept_or_queue(self):
        with patch.dict(os.environ,{'INGRESS_ENABLED':'0'}):
            values=self.values()
            with self.assertRaises(HTTPError) as error:self.action('domain-create',values=values,request_id='domain-http-request-0003')
            self.assertEqual(error.exception.code,409);error.exception.close()
            self.assertEqual(self.fixture.db.execute('SELECT count(*) FROM ingress_routes').fetchone()[0],1)

    def test_disabled_worker_preserves_existing_operation_recovery(self):
        with patch.dict(os.environ,{'INGRESS_ENABLED':'1'}):
            values=self.values()
            accepted=self.action('domain-create',values=values,request_id='domain-http-recovery-0001')
        operation_id=accepted['operation']['id']
        self.fixture.db.execute("UPDATE domain_executions SET state='failed' WHERE operation_id=?",(operation_id,))
        with patch.dict(os.environ,{'INGRESS_ENABLED':'0'}):
            resumed=self.action('domain-resume',operation_id=operation_id)['operation']
            self.assertEqual(resumed['status'],'queued')
            self.assertFalse(resumed['executor_available'])
            self.assertEqual(resumed['executor']['state'],'disabled')
            self.assertEqual(resumed['id'],operation_id)
