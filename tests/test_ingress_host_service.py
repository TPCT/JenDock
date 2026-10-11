import dataclasses
import os
import threading
import unittest
from ingress_host_transport import HostServer, HostClient, TransportError
from ingress_host_service import HostService
import test_ingress_host_grants as fixtures


class HostServiceTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.HostGrantTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.service=HostService(self.f.adapter,self.f.issuer)
        self.path=self.f.root/'helper.sock'
        self.server=HostServer(self.path,self.service,allowed_uids={os.getuid()},owner_uid=os.getuid(),timeout=1).open()
        self.addCleanup(self.server.close)

    def call(self,method,request):
        # Keep issuer SQLite use on its owning thread; socket client runs separately.
        results=[]
        def client():
            try:results.append(HostClient(self.path,expected_server_uid=os.getuid(),timeout=2).call(method,request))
            except Exception as error:results.append(error)
        thread=threading.Thread(target=client);thread.start()
        try:self.server.serve_once()
        finally:thread.join(3)
        self.assertFalse(thread.is_alive());self.assertEqual(len(results),1)
        if isinstance(results[0],Exception):raise results[0]
        return results[0]

    def test_real_socket_grant_stage_activate_revalidates(self):
        self.assertEqual(self.call('issue_grant',self.f.request)['status'],'granted')
        before=self.f.observations
        request={'route_id':self.f.route,'operation_id':'b'*32,'expected_digest':None}
        staged=self.call('stage_http',request)
        self.assertEqual(self.call('activate_http',dict(request,digest=staged['digest']))['status'],'active')
        self.assertGreater(self.f.observations,before)
        self.assertEqual(self.f.executor.calls,['test','reload'])

    def test_revoked_grant_and_extra_fields_are_redacted(self):
        for request in (dict(self.f.request,port=31002),dict(self.f.request,secret='private')):
            with self.assertRaisesRegex(TransportError,'^operation_failed$'):self.call('issue_grant',request)
        self.f.principal=dataclasses.replace(self.f.principal,scopes=frozenset())
        with self.assertRaisesRegex(TransportError,'^operation_failed$'):self.call('issue_grant',self.f.request)
        self.assertEqual(self.f.policy()['routes'],{});self.assertEqual(self.f.executor.calls,[])

    def test_denied_socket_peer_cannot_issue(self):
        self.server.allowed_uids=frozenset({os.getuid()+1})
        with self.assertRaisesRegex(TransportError,'^unauthorized$'):self.call('issue_grant',self.f.request)
        self.assertEqual(self.f.observations,0)

    def test_issuer_adapter_must_be_identical(self):
        with self.assertRaises(ValueError):HostService(object(),self.f.issuer)

    def test_bare_adapter_cannot_expose_issue_method(self):
        self.server.adapter=self.f.adapter
        # Reconstruct because exposed capabilities are selected at construction.
        self.server.close()
        self.server=HostServer(self.path,self.f.adapter,allowed_uids={os.getuid()},owner_uid=os.getuid(),timeout=1).open()
        self.addCleanup(self.server.close)
        with self.assertRaisesRegex(TransportError,'^invalid_request$'):self.call('issue_grant',self.f.request)

    def test_revocation_after_socket_stage_blocks_activation(self):
        self.call('issue_grant',self.f.request)
        request={'route_id':self.f.route,'operation_id':'b'*32,'expected_digest':None}
        staged=self.call('stage_http',request)
        self.f.principal=dataclasses.replace(self.f.principal,scopes=frozenset())
        with self.assertRaisesRegex(TransportError,'^operation_failed$'):
            self.call('activate_http',dict(request,digest=staged['digest']))
        self.assertEqual(self.f.executor.calls,[])
        self.assertFalse(list((self.f.root/'managed').glob('*.conf')))

    def test_issuer_private_error_never_crosses_socket(self):
        def fail(request):raise RuntimeError('/private/credentials token=private-secret')
        self.f.issuer.issue=fail
        with self.assertRaisesRegex(TransportError,'^operation_failed$') as caught:
            self.call('issue_grant',self.f.request)
        self.assertNotIn('private',str(caught.exception))

    def test_missing_issuer_capabilities_rejected_before_wiring(self):
        from types import SimpleNamespace
        for callback,issue in ((None,lambda request:None),(lambda route,grant:None,None)):
            issuer=SimpleNamespace(adapter=self.f.adapter,authorize_route=callback,issue=issue)
            previous=self.f.adapter.authorize_route
            with self.assertRaises(ValueError):HostService(self.f.adapter,issuer)
            self.assertIs(self.f.adapter.authorize_route,previous)

    def test_changed_adapter_callback_blocks_every_dispatch(self):
        from unittest.mock import Mock
        forwarded=Mock(return_value={'unexpected':'dispatch'})
        self.f.issuer.issue=forwarded
        for name in ('stage_http','activate_http','inspect','verify','recover'):
            setattr(self.f.adapter,name,forwarded)
        for replacement in (None,lambda route,grant:None):
            self.f.adapter.authorize_route=replacement
            for method in ('issue_grant','stage_http','activate_http','inspect','verify','recover'):
                with self.subTest(method=method,replacement=replacement):
                    with self.assertRaisesRegex(TransportError,'^operation_failed$'):self.call(method,{})
        forwarded.assert_not_called()
        self.assertEqual(self.f.observations,0)
        self.assertEqual(self.f.executor.calls,[])
        self.assertEqual(self.f.policy()['routes'],{})
