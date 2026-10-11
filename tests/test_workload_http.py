"""Real authenticated HTTP -> peer checked Unix RPC -> workload controller.

Container observations are explicit test doubles; this does not prove Docker exec.
"""
import os
from unittest.mock import patch
from urllib.error import HTTPError
import unittest
import workload_rpc
import test_database_control_flow as http_fixture
from test_workload_control import TARGET, Transport


class HTTPWorkloadTests(unittest.TestCase):
    def setUp(self):
        self.f=http_fixture.DatabaseControlFlowTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.target=dict(TARGET,project='app')
        self.transport=Transport()
        original=workload_rpc.request
        for item in (
            patch.dict(os.environ,{'WORKLOAD_RECEIVER_UID':str(os.getuid()),'WORKLOAD_WORKER_UID':str(os.getuid())}),
            patch('workload_rpc.request',side_effect=lambda payload,path:original(payload,self.f.socket)),
            patch('workload_transport.WorkloadTransport',return_value=self.transport),
            patch('workload_control._target',return_value={'role':'queue','configuration':{'connection':'pgsql','queues':['default']}}),
        ):
            item.start();self.addCleanup(item.stop)
    def test_session_csrf_and_worker_peer_checked_read(self):
        body=dict(self.target,action='workload-role-details')
        result,_=self.f.post('/_deploy/api/action',body)
        self.assertEqual(result['workload']['project'],'app')
        self.assertEqual(self.transport.calls,['queue.observe'])
        for extra in ({'authentication':{'kind':'console','session_digest':'a'*64}},{'actor':'owner'}):
            with self.assertRaises(HTTPError) as denied:self.f.post('/_deploy/api/action',dict(body,**extra))
            self.assertEqual(denied.exception.code,400);denied.exception.close()
        for authenticated,csrf in ((False,None),(True,'wrong')):
            with self.assertRaises(HTTPError) as denied:self.f.post('/_deploy/api/action',body,authenticated=authenticated,csrf=csrf)
            self.assertIn(denied.exception.code,(401,403));denied.exception.close()
        with patch.dict(os.environ,{'WORKLOAD_RECEIVER_UID':str(os.getuid()+1)}):
            with self.assertRaises(HTTPError) as denied:self.f.post('/_deploy/api/action',body)
            self.assertEqual(denied.exception.code,403);denied.exception.close()
        self.assertEqual(self.transport.calls,['queue.observe'])
