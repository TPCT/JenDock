import unittest
import ingress_external as external

class ExternalVerificationTests(unittest.TestCase):
    def test_hostname_and_token_closed_inputs(self):
        for host in ('https://app.example.com','localhost','127.0.0.1','app.example.com:443','APP.example.com','app.example.com.','*.example.com'):
            with self.subTest(host=host),self.assertRaises(ValueError):external.verify(host,'a'*64,transport=lambda *args:None)
    def test_private_destination_denied_before_transport(self):
        for address in ('127.0.0.1','10.0.0.1','169.254.169.254','::1','fc00::1','192.0.2.1'):
            with self.subTest(address=address),self.assertRaises(ValueError):external.public_addresses([address])

    def test_exact_evidence_and_normal_tls_policy_with_trusted_fixture_transport(self):
        import hashlib,ssl
        def transport(host,addresses,deadline,context):
            self.assertEqual(host,'app.example.com');self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED);self.assertTrue(context.check_hostname)
            return {'status':200,'body':b'a'*64,'peer_address':addresses[0],'certificate_fingerprint':'b'*64}
        evidence=external.verify('app.example.com','a'*64,resolver=lambda *args:['127.0.0.1'],transport=transport)
        self.assertEqual(evidence['proof_digest'],hashlib.sha256(b'a'*64).hexdigest());self.assertFalse(evidence['ready'])

    def test_private_resolver_cannot_bypass_production_transport(self):
        from unittest.mock import patch
        with patch.object(external,'_transport') as transport,self.assertRaises(ValueError):
            external.verify('app.example.com','a'*64,resolver=lambda *args:['127.0.0.1'])
        transport.assert_not_called()
        with self.assertRaises(ValueError):external.public_addresses(['8.8.8.8','10.0.0.1'])

    def test_redirect_wrong_body_peer_and_malformed_fingerprint_fail(self):
        baseline={'status':200,'body':b'a'*64,'peer_address':'8.8.8.8','certificate_fingerprint':'b'*64}
        for change in ({'status':302},{'body':b'a'*64+b'\n'},{'peer_address':'1.1.1.1'},{'certificate_fingerprint':'bad'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                external.verify('app.example.com','a'*64,resolver=lambda *args:['8.8.8.8'],transport=lambda *args:dict(baseline,**change))

    def test_raw_http_framing_limits_and_chunked_proof(self):
        import time
        class Stream:
            def __init__(self,data):self.data=data
            def settimeout(self,value):pass
            def recv(self,length):value=self.data[:length];self.data=self.data[length:];return value
        valid=b'HTTP/1.1 200 OK\r\nContent-Length: 64\r\n\r\n'+b'a'*64
        self.assertEqual(external._response(Stream(valid),time.monotonic()+1),b'a'*64)
        chunked=b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n40\r\n'+b'a'*64+b'\r\n0\r\n\r\n'
        self.assertEqual(external._response(Stream(chunked),time.monotonic()+1),b'a'*64)
        for invalid in (valid.replace(b'200 OK',b'302 Found'),valid.replace(b'64',b'999',1),
                        valid.replace(b'Content-Length: 64',b'Content-Length: 64\r\nContent-Length: 64'),
                        chunked.replace(b'Transfer-Encoding: chunked',b'Transfer-Encoding: chunked\r\nContent-Length: 64'),
                        valid.replace(b'Content-Length: 64',b'X-Long: '+b'x'*17000),
                        valid.replace(b'Content-Length: 64',b'Content-Encoding: gzip\r\nContent-Length: 64')):
            with self.subTest(size=len(invalid)),self.assertRaises(ValueError):external._response(Stream(invalid),time.monotonic()+1)

    def test_pinned_numeric_connection_preserves_sni_and_sends_no_credentials(self):
        import time
        from unittest.mock import MagicMock,patch
        raw=MagicMock();raw.__enter__.return_value=raw
        secured=MagicMock();secured.__enter__.return_value=secured;secured.getpeercert.return_value=b'fixture-der'
        secured.recv.return_value=b'HTTP/1.1 200 OK\r\nContent-Length: 64\r\n\r\n'+b'a'*64
        context=MagicMock();context.wrap_socket.return_value=secured
        with patch.object(external.socket,'socket',return_value=raw):
            external._transport('app.example.com',['8.8.8.8'],time.monotonic()+1,context)
        raw.connect.assert_called_once_with(('8.8.8.8',443));context.wrap_socket.assert_called_once_with(raw,server_hostname='app.example.com')
        request=secured.sendall.call_args.args[0]
        self.assertIn(b'GET /.well-known/deploy-monitor/route-proof HTTP/1.1',request)
        self.assertIn(b'Host: app.example.com',request);self.assertNotIn(b'Cookie:',request);self.assertNotIn(b'Authorization:',request)

    def test_resolution_timeout_and_transport_deadline_are_bounded(self):
        import threading,time
        from unittest.mock import patch
        release=threading.Event()
        def stuck(*args,**kwargs):release.wait(1);return []
        try:
            with patch.object(external.socket,'getaddrinfo',side_effect=stuck),self.assertRaises(ValueError):
                external._resolve('app.example.com',time.monotonic()+.02)
        finally:release.set()
        def slow(*args):
            time.sleep(.02)
            return {'status':200,'body':b'a'*64,'peer_address':'8.8.8.8','certificate_fingerprint':'b'*64}
        with self.assertRaises(ValueError):external.verify('app.example.com','a'*64,timeout=.01,resolver=lambda *args:['8.8.8.8'],transport=slow)

    def test_errors_never_echo_transport_details(self):
        def failed(*args):raise RuntimeError('private-fixture-secret')
        with self.assertRaises(ValueError) as error:external.verify('app.example.com','a'*64,resolver=lambda *args:['8.8.8.8'],transport=failed)
        self.assertNotIn('private-fixture-secret',str(error.exception))
