"""Independent TLS integration regression checks; no certificate issuance effects."""
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import ingress_host_daemon as daemon
from ingress_host_service import HostService
from ingress_host_transport import HostServer, HostClient, TransportError
import test_ingress_host_tls as tls_fixtures
import test_ingress_host_daemon as daemon_fixtures


class TLSIntegrationReviewTests(unittest.TestCase):
    def setUp(self):
        self.f=tls_fixtures.TLSHostTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.cert.paths={'challenge_root':self.f.challenge}
        self.f.cert.request.return_value={'status':'verified'}
        self.issuer=SimpleNamespace(adapter=self.f.http,authorize_route=lambda *args:None,
                                    authorize_certificate=Mock(),issue=Mock())
        self.service=HostService(self.f.http,self.issuer,tls=self.f.tls)

    def test_challenge_actual_file_bytes_proved_and_removed_before_issuance(self):
        def verify(host,token,expected):
            path=self.f.challenge/'.well-known'/'acme-challenge'/token
            self.assertEqual(host,'dev.example.test');self.assertEqual(path.read_bytes(),expected)
            self.assertGreaterEqual(len(expected),16);return True
        self.f.executor.verify_challenge=Mock(side_effect=verify)
        def issue(*args,**kwargs):
            self.assertEqual(list((self.f.challenge/'.well-known'/'acme-challenge').iterdir()),[])
            kwargs['authorize']();return {'status':'verified'}
        self.f.cert.request.side_effect=issue
        self.assertEqual(self.service.request_certificate(self.f.request),{'status':'verified'})
        self.assertTrue(self.issuer.authorize_certificate.called)

    def test_failed_or_unknown_challenge_never_issues_and_cleans_probe(self):
        for observed in (False,None):
            self.f.executor.verify_challenge=Mock(return_value=observed)
            with self.assertRaisesRegex(ValueError,'challenge routing is not verified'):
                self.service.request_certificate(self.f.request)
            self.f.cert.request.assert_not_called()
            self.assertEqual(list((self.f.challenge/'.well-known'/'acme-challenge').iterdir()),[])

    def test_legacy_http_route_rejected_before_probe_or_issuance(self):
        grant=self.f.http._policy()['routes']['a'*32]
        self.f.http.challenge_root=None;legacy=self.f.http._config(grant,self.f.http._policy()['listener'])
        self.f.http.challenge_root=self.f.challenge
        path,journal=self.f.http._paths(self.f.http._policy(),'a'*32)
        from ingress_host import _digest
        state=self.f.http._state(journal);state.update(candidate=legacy,digest=_digest(legacy))
        self.f.http._write(path,legacy);self.f.http._save(journal,state)
        self.f.executor.verify_challenge=Mock(return_value=True)
        with self.assertRaisesRegex(ValueError,'Upgrade the owned HTTP route'):
            self.service.request_certificate(self.f.request)
        self.f.executor.verify_challenge.assert_not_called();self.f.cert.request.assert_not_called()

    def test_challenge_directory_symlink_escape_is_rejected(self):
        (self.f.challenge/'.well-known').symlink_to(self.f.root,target_is_directory=True)
        self.f.executor.verify_challenge=Mock(return_value=True)
        with self.assertRaises(ValueError):self.service.request_certificate(self.f.request)
        self.f.executor.verify_challenge.assert_not_called();self.f.cert.request.assert_not_called()

    def test_tls_facade_rejects_replaced_authority_hook(self):
        self.f.tls.authorize_certificate=lambda *args:None
        for method in ('request_certificate','stage_tls','activate_tls','inspect_tls','verify_tls','recover_tls'):
            with self.subTest(method=method),self.assertRaises(ValueError):getattr(self.service,method)({})

    def test_tls_facade_rejects_replaced_http_adapter(self):
        self.f.tls.http=Mock()
        with self.assertRaises(ValueError):self.service._tls()

    def test_tls_capability_certificate_manager_and_related_hook_are_pinned(self):
        for owner,attribute in ((self.service,'tls'),(self.f.tls,'certificates'),(self.f.http,'related_configs')):
            with self.subTest(attribute=attribute),patch.object(owner,attribute,Mock()):
                with self.assertRaises(ValueError):self.service._tls()
                with self.assertRaises(ValueError):self.service.capabilities({})

    def test_real_socket_tls_dispatch_and_capabilities(self):
        path=self.f.root/'tls-review.sock'
        server=HostServer(path,self.service,allowed_uids={os.getuid()},owner_uid=os.getuid(),timeout=1).open();self.addCleanup(server.close)
        def call(method,request):
            results=[]
            def client():
                try:results.append(HostClient(path,expected_server_uid=os.getuid(),timeout=2).call(method,request))
                except Exception as error:results.append(error)
            thread=threading.Thread(target=client);thread.start()
            try:server.serve_once()
            finally:thread.join(3)
            self.assertFalse(thread.is_alive())
            if isinstance(results[0],Exception):raise results[0]
            return results[0]
        self.assertEqual(call('capabilities',{}),{'schema':1,'http':True,'tls':True})
        with self.assertRaises(TransportError):call('capabilities',{'tls':True})
        for method in ('request_certificate','stage_tls','activate_tls','inspect_tls','verify_tls','recover_tls'):
            with self.subTest(method=method):
                function=Mock(return_value={'method':method})
                with patch.object(self.f.tls,method,function):
                    self.assertEqual(call(method,self.f.request),{'method':method})
                function.assert_called_once_with(self.f.request)

    def test_daemon_schema2_wires_tls_listener_trust_challenge_and_facade(self):
        f=daemon_fixtures.DaemonTests();f.setUp();self.addCleanup(f.doCleanups)
        challenge=f.root/'challenge';challenge.mkdir(mode=0o700)
        f.config['schema_version']=2
        f.config['tls']={'listener':{'address':'127.0.0.1','port':8443},'observation_listener':{'address':'127.0.0.1','port':8443},'ca_file':None,'certificates':{}}
        f.save();config=daemon.load_config(f.path,owner_uid=os.getuid())
        cert=Mock();cert.paths={'challenge_root':challenge}
        with patch.object(daemon,'ApacheExecutor') as executor,patch('ingress_certificates.CertificateManager',return_value=cert):
            assembly=daemon.Assembly(config,owner_uid=os.getuid())
            try:
                self.assertEqual(executor.call_args.kwargs['tls_listener'],('127.0.0.1',8443))
                self.assertIsNone(executor.call_args.kwargs['tls_ca_file'])
                self.assertEqual(assembly.adapter.challenge_root,challenge)
                self.assertIs(assembly.service.tls.http,assembly.adapter)
                self.assertIs(assembly.service._tls(),assembly.service.tls)
            finally:assembly.close()
