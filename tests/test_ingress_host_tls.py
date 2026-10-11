import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
from ingress_host_tls import HostTLSAdapter
import test_ingress_host as fixtures


class TLSHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.http, self.executor, self.root = fixtures.make_host(Path(self.temp.name))
        self.challenge = self.root/'challenge'; self.challenge.mkdir(mode=0o700)
        self.http.challenge_root = self.challenge
        self.request = {'route_id': 'a'*32, 'operation_id': 'b'*32}
        http = self.http.stage_http(dict(self.request, expected_digest=None))
        self.http.activate_http(dict(self.request, expected_digest=None, digest=http['digest']))
        self.http_bytes = (self.root/'managed'/('a'*32+'.conf')).read_bytes()
        self.executor.calls.clear(); self.executor.observe_tls = Mock(return_value=True)
        self.cert = Mock()
        files={}
        for role in ('fullchain','privkey'):
            path=self.root/(role+'.pem');path.write_text('fixture');path.chmod(0o600);files[role]=str(path)
        self.metadata=dict(self.request, hostname='dev.example.test', generation='c'*64,fingerprint='d'*64,not_after=time.time()+3600,status='verified')
        self.cert.generation_files.return_value=dict(self.metadata, **files)
        self.cert.inspect.return_value=self.metadata
        self.tls = HostTLSAdapter(self.http,self.cert,listener={'address':'127.0.0.1','port':8443})
        self.stage_request=dict(self.request, expected_digest=None,generation='c'*64,fingerprint='d'*64)
    def stage(self): return self.tls.stage_tls(self.stage_request)
    def activate(self):
        staged=self.stage()
        return self.tls.activate_tls(dict(self.request,digest=staged['digest']))
    def test_http_challenge_exclusion_precedes_proxy(self):
        config=self.http_bytes.decode()
        self.assertLess(config.index('ProxyPass /.well-known/acme-challenge/ !'),config.index('ProxyPass / http'))
        self.assertIn('SetHandler none',config)
    def test_activation_preserves_http_and_replay_no_repeat(self):
        active=self.activate();self.assertEqual(active['status'],'active')
        self.assertEqual(self.executor.calls,['test','reload'])
        self.tls.activate_tls(dict(self.request,digest=active['digest']))
        self.assertEqual(self.executor.calls,['test','reload'])
        self.assertEqual((self.root/'managed'/('a'*32+'.conf')).read_bytes(),self.http_bytes)
        tls_path=self.root/'managed'/('a'*32+'-tls.conf')
        self.executor.names=[{'names':['dev.example.test'],'path':str(tls_path)}]
        self.assertEqual(self.http.verify({'route_id':'a'*32})['status'],'active')
    def test_failed_config_restores_without_signal(self):
        self.executor.fail='test'
        with self.assertRaises(RuntimeError): self.activate()
        self.assertEqual(self.executor.calls,['test'])
        self.assertFalse((self.root/'managed'/('a'*32+'-tls.conf')).exists())
        self.assertEqual(self.tls.inspect_tls(self.request)['status'],'staged')
    def test_lost_reload_reply_recovers_by_observation(self):
        self.executor.fail='reload'
        with self.assertRaises(RuntimeError):self.activate()
        self.executor.fail=None;self.executor.observe_tls.return_value=None
        with self.assertRaises(ValueError):self.tls.recover_tls(self.request)
        self.assertEqual(self.executor.calls,['test','reload'])
        self.executor.observe_tls.return_value=True
        self.assertEqual(self.tls.recover_tls(self.request)['status'],'active')
        self.assertEqual(self.executor.calls,['test','reload'])
    def test_foreign_tls_collision_and_stale_certificate(self):
        self.executor.names=[{'names':['*.example.test'],'path':'/foreign.conf'}]
        with self.assertRaises(ValueError):self.stage()
        self.executor.names=[];self.cert.generation_files.return_value['fingerprint']='e'*64
        with self.assertRaises(ValueError):self.stage()
        self.assertEqual(self.executor.calls,[])
    def test_cross_operation_and_injected_paths_rejected(self):
        with self.assertRaises(ValueError):self.tls.stage_tls(dict(self.stage_request,operation_id='f'*32))
        with self.assertRaises(ValueError):self.tls.stage_tls(dict(self.stage_request,certificate_path='/tmp/key'))
        self.assertEqual(self.executor.calls,[])
    def test_changed_tls_file_is_not_exempt_from_collision(self):
        self.activate();path=self.root/'managed'/('a'*32+'-tls.conf');path.write_text('foreign')
        self.executor.names=[{'names':['dev.example.test'],'path':str(path)}]
        with self.assertRaises(ValueError):self.http.verify({'route_id':'a'*32})
    def test_revocation_before_signal_compensates(self):
        staged=self.stage()
        def reject():
            self.http.authorize_route=lambda *a: (_ for _ in ()).throw(ValueError('revoked'))
        self.executor.configtest=reject
        with self.assertRaises(ValueError):self.tls.activate_tls(dict(self.request,digest=staged['digest']))
        self.assertNotIn('reload',self.executor.calls)
        self.assertFalse((self.root/'managed'/('a'*32+'-tls.conf')).exists())
