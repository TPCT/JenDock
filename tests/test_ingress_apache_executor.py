import unittest
from ingress_apache_executor import parse_inventory

class ExecutorTests(unittest.TestCase):
    def test_real_apache_shapes(self):
        rows=parse_inventory('''VirtualHost configuration:
*:80 is a NameVirtualHost
         default server sentinel.test (/etc/apache2/sentinel.conf:1)
         port 80 namevhost sentinel.test (/etc/apache2/sentinel.conf:1)
         port 80 namevhost app.example.test (/etc/apache2/app.conf:1)
                 alias other.example.test
                 wild alias *.example.test
127.0.0.1:81 solo.test (/etc/apache2/solo.conf:3)
ServerRoot: "/etc/apache2"
Main DocumentRoot: "/var/www"
PidFile: "/run/apache2.pid"
Define: DUMP_VHOSTS
User: name="www-data" id=33
Group: name="www-data" id=33
''')
        self.assertEqual(rows[1],{'names':['app.example.test','other.example.test','*.example.test'],'path':'/etc/apache2/app.conf'})
        self.assertEqual(len(rows),3)
    def test_unparseable_inventory_rejected(self):
        for text in ('garbage','VirtualHost configuration:\n unknown vhost syntax','VirtualHost configuration:\n alias detached.test'):
            with self.assertRaises(ValueError):parse_inventory(text)

    def test_bounded_subprocess_output_and_timeout(self):
        import sys
        from ingress_apache_executor import bounded_run
        self.assertEqual(bounded_run([sys.executable,'-c','print("ok")'],1),'ok\n')
        for script in ('print("x"*70000)','import time;time.sleep(2)'):
            with self.assertRaises(ValueError):bounded_run([sys.executable,'-c',script],.1)

    def http_executor(self, reply):
        import socket
        import threading
        from ingress_apache_executor import ApacheExecutor
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
        executor=object.__new__(ApacheExecutor);executor.listener=listener.getsockname();executor.timeout=.2
        requests=[]
        def serve():
            try:
                connection,_=listener.accept()
                with connection:
                    requests.append(connection.recv(4096));connection.sendall(reply)
            finally:listener.close()
        thread=threading.Thread(target=serve);thread.start();self.addCleanup(thread.join,1)
        return executor,requests

    def test_real_http_identity_and_host(self):
        e,requests=self.http_executor(b'HTTP/1.1 200 OK\r\nX-Deploy-Monitor-Route: '+b'a'*64+b'\r\nContent-Length: 0\r\n\r\n')
        self.assertTrue(e.observe('app.example.test','a'*64))
        self.assertIn(b'Host: app.example.test\r\n',requests[0])

    def test_http_missing_duplicate_truncated_and_redirect(self):
        replies=[(b'HTTP/1.1 200 OK\r\n\r\n',False),
            (b'HTTP/1.1 200 OK\r\nX-Deploy-Monitor-Route: '+b'a'*64+b'\r\nX-Deploy-Monitor-Route: '+b'a'*64+b'\r\n\r\n',None),
            (b'HTTP/1.1 200 OK\r\n',None),
            (b'HTTP/1.1 302 Found\r\nLocation: http://evil.test/\r\n\r\n',False)]
        for response,wanted in replies:
            with self.subTest(response=response):
                e,_=self.http_executor(response);self.assertIs(e.observe('app.example.test','a'*64),wanted)

    def test_absence_requires_inventory_no_alias(self):
        from ingress_apache_executor import ApacheExecutor
        e=object.__new__(ApacheExecutor);e._headers=lambda host:None
        e.inventory=lambda:[{'names':['*.example.test'],'path':'/foreign.conf'}]
        self.assertFalse(e.observe_absence('app.example.test'))
        e.inventory=lambda:[];self.assertTrue(e.observe_absence('app.example.test'))
        def fail():raise ValueError('unknown')
        e.inventory=fail;self.assertIsNone(e.observe_absence('app.example.test'))

    def test_wrong_process_cannot_be_signalled(self):
        from unittest.mock import patch
        from ingress_apache_executor import ApacheExecutor
        e=object.__new__(ApacheExecutor);e._pid=lambda:321
        def wrong(pid):raise ValueError('wrong executable')
        e._verify_pid=wrong
        with patch('ingress_apache_executor.os.pidfd_open',return_value=99),patch('ingress_apache_executor.os.close') as close,patch('ingress_apache_executor.signal.pidfd_send_signal') as send:
            with self.assertRaises(ValueError):e.reload()
            send.assert_not_called();close.assert_called_once_with(99)

    def test_reload_pins_existing_process_only(self):
        import signal
        from unittest.mock import patch
        from ingress_apache_executor import ApacheExecutor
        e=object.__new__(ApacheExecutor);e._pid=lambda:321;e._verify_pid=lambda pid:None
        with patch('ingress_apache_executor.os.pidfd_open',return_value=99),patch('ingress_apache_executor.os.close'),patch('ingress_apache_executor.signal.pidfd_send_signal') as send:
            e.reload();send.assert_called_once_with(99,signal.SIGUSR1)

    def test_single_vhost_inventory_recovers_real_source_aliases(self):
        import os
        from pathlib import Path
        import tempfile
        from ingress_apache_executor import ApacheExecutor
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'site.conf'
            path.write_text('<VirtualHost 127.0.0.1:38255>\nServerName sentinel.fixture.test\nServerAlias alias.fixture.test *.reserved.fixture.test\n</VirtualHost>\n');path.chmod(0o600)
            e=object.__new__(ApacheExecutor);e.owner_uid=os.getuid()
            e._command=lambda flag:f'VirtualHost configuration:\n127.0.0.1:38255        sentinel.fixture.test ({path}:1)\n'
            self.assertEqual(e.inventory(),[{'names':['sentinel.fixture.test','alias.fixture.test','*.reserved.fixture.test'],'path':str(path)}])

    def test_unprovable_alias_sources_fail_closed(self):
        import os
        from pathlib import Path
        import tempfile
        from ingress_apache_executor import ApacheExecutor
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'site.conf'
            e=object.__new__(ApacheExecutor);e.owner_uid=os.getuid()
            e._command=lambda flag:f'VirtualHost configuration:\n127.0.0.1:80 app.test ({path}:1)\n'
            for directive in ('Include /tmp/other','IncludeOptional /tmp/*','Use Site name','<IfDefine TEST>','ServerAlias ${ALIAS}','ServerAlias next.test \\\\'):
                with self.subTest(directive=directive):
                    path.write_text('<VirtualHost 127.0.0.1:80>\nServerName app.test\n'+directive+'\n</VirtualHost>\n');path.chmod(0o600)
                    with self.assertRaises(ValueError):e.inventory()

    def test_actual_debian_multi_vhost_spacing(self):
        output='VirtualHost configuration:\n127.0.0.1:56839        is a NameVirtualHost\n         default server sentinel.fixture.test (/tmp/deploy-monitor-apache-executor-w5pljdpm/sentinel.conf:1)\n         port 56839 namevhost sentinel.fixture.test (/tmp/deploy-monitor-apache-executor-w5pljdpm/sentinel.conf:1)\n                 alias alias.fixture.test\n                 wild alias *.reserved.fixture.test\n         port 56839 namevhost dev.fixture.test (/tmp/deploy-monitor-apache-executor-w5pljdpm/managed/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.conf:2)\nServerRoot: "/tmp/deploy-monitor-apache-executor-w5pljdpm"\nMain DocumentRoot: "/var/www/html"\nMain ErrorLog: "/tmp/deploy-monitor-apache-executor-w5pljdpm/error.log"\nMutex watchdog-callback: using_defaults\nMutex proxy: using_defaults\nMutex default: dir="/var/run/apache2/" mechanism=default \nPidFile: "/tmp/deploy-monitor-apache-executor-w5pljdpm/httpd.pid"\nDefine: FOREGROUND\nDefine: DUMP_VHOSTS\nDefine: DUMP_RUN_CFG\nUser: name="#-1" id=4294967295 not_used\nGroup: name="#-1" id=4294967295 not_used\n'
        rows=parse_inventory(output)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]["names"],["sentinel.fixture.test","alias.fixture.test","*.reserved.fixture.test"])
        self.assertEqual(rows[1]["names"],["dev.fixture.test"])
        self.assertEqual(parse_inventory(output.replace(" ","\t")),rows)

    def source_executor(self, root):
        import os
        from ingress_apache_executor import ApacheExecutor
        e=object.__new__(ApacheExecutor);e.owner_uid=os.getuid()
        e.source_roots=(root,);e.server_root=root
        return e

    def test_trusted_nested_includes_and_enabled_symlink_aliases(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'available').mkdir(mode=0o700);(root/'enabled').mkdir(mode=0o700);(root/'parts').mkdir(mode=0o700)
            site=root/'available/site.conf';site.write_text('<VirtualHost *:80>\nServerName app.test\nInclude "parts/*.conf"\nIncludeOptional parts/missing.conf\n</VirtualHost>\n')
            (root/'parts/one.conf').write_text('ServerAlias alias.test *.reserved.test\nInclude parts/nested.inc\n')
            (root/'parts/nested.inc').write_text('ServerAlias nested.test\n')
            for file in (site, root/'parts/one.conf', root/'parts/nested.inc'):file.chmod(0o600)
            link=root/'enabled/site.conf';link.symlink_to(site)
            e=self.source_executor(root)
            self.assertEqual(e._aliases(str(link),1,'app.test'),['app.test','alias.test','*.reserved.test','nested.test'])

    def test_includes_reject_escape_untrusted_cycle_and_aggregate_bound(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root=Path(directory);site=root/'site.conf';part=root/'part.conf';e=self.source_executor(root)
            foreign=Path(outside)/'foreign.conf';foreign.write_text('ServerAlias stolen.test\n')
            for content in (f'Include {foreign}', 'Include ../foreign.conf', 'Include part.conf', 'ServerAlias '+('x'*65536)):
                with self.subTest(content=content[:60]):
                    part.write_text(content+'\n');site.write_text('<VirtualHost *:80>\nServerName app.test\nInclude part.conf\n</VirtualHost>\n')
                    part.chmod(0o600);site.chmod(0o600)
                    with self.assertRaises(ValueError):e._aliases(str(site),1,'app.test')
            part.unlink();part.symlink_to(foreign)
            with self.assertRaises(ValueError):e._aliases(str(site),1,'app.test')
            part.unlink();part.write_text('ServerAlias bad.test\n');part.chmod(0o666)
            with self.assertRaises(ValueError):e._aliases(str(site),1,'app.test')

    def test_known_external_includes_require_explicit_read_roots(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as certificates:
            root=Path(directory);certs=Path(certificates);site=root/'site.conf';include=certs/'acme-vhost.conf'
            include.write_text('Alias /.well-known/acme-challenge/ /var/www/acme/\nServerAlias challenge.test\n');include.chmod(0o600)
            site.write_text(f'<VirtualHost *:443>\nServerName app.test\nInclude {include}\nErrorLog ${{APACHE_LOG_DIR}}/error.log\n</VirtualHost>\n');site.chmod(0o600)
            e=self.source_executor(root)
            with self.assertRaises(ValueError):e._aliases(str(site),1,'app.test')
            e.source_roots=(root,certs)
            self.assertEqual(e._aliases(str(site),1,'app.test'),['app.test','challenge.test'])

    def test_shared_include_budget_and_conditional_alias_rejected(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);site=root/'site.conf';part=root/'part.conf';e=self.source_executor(root)
            for body in ('#'+('x'*40000)+'\n', '<IfDefine TEST>\nServerAlias hidden.test\n</IfDefine>\n'):
                part.write_text(body);part.chmod(0o600)
                site.write_text('<VirtualHost *:80>\nServerName app.test\nInclude part.conf\nInclude part.conf\n</VirtualHost>\n');site.chmod(0o600)
                with self.assertRaises(ValueError):e._aliases(str(site),1,'app.test')

    def tls_executor(self, reply, *, trusted=True, hostname='app.example.test'):
        import datetime
        import hashlib
        import os
        from pathlib import Path
        import socket
        import ssl
        import tempfile
        import threading
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        from ingress_apache_executor import ApacheExecutor
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);root=Path(temporary.name)
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,hostname)])
        now=datetime.datetime.now(datetime.timezone.utc)
        cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(hours=1))
              .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]),critical=False)
              .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
        certificate=root/'ca.pem';certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM));certificate.chmod(0o600)
        private=root/'key.pem';private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));private.chmod(0o600)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(certificate,private)
        sni=[];context.set_servername_callback(lambda connection,name,ctx:sni.append(name))
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(2)
        e=object.__new__(ApacheExecutor);e.owner_uid=os.getuid();e.timeout=.5
        e._configure_tls(listener.getsockname(),certificate if trusted else None)
        requests=[]
        def serve():
            try:
                connection,_=listener.accept()
                with connection:
                    try:
                        with context.wrap_socket(connection,server_side=True) as secured:
                            requests.append(secured.recv(4096));secured.sendall(reply)
                    except (OSError,ssl.SSLError):pass
            finally:listener.close()
        thread=threading.Thread(target=serve);thread.start();self.addCleanup(thread.join,3)
        return e,hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest(),sni,requests

    def test_tls_verified_sni_fingerprint_and_status(self):
        reply=b'HTTP/1.1 503 Unavailable\r\nX-Deploy-Monitor-Route: '+b'a'*64+b'\r\nContent-Length: 0\r\n\r\n'
        e,fingerprint,sni,requests=self.tls_executor(reply)
        proof=e.tls_evidence('app.example.test')
        self.assertEqual(proof['status'],503);self.assertEqual(proof['certificate_fingerprint'],fingerprint)
        self.assertEqual(proof['route_token'],'a'*64);self.assertEqual(sni,['app.example.test'])
        self.assertIn(b'Host: app.example.test',requests[0])

    def test_tls_observation_true_different_identity_and_untrusted_unknown(self):
        reply=b'HTTP/1.1 200 OK\r\nX-Deploy-Monitor-Route: '+b'a'*64+b'\r\n\r\n'
        for trusted,wrong,wanted in ((True,False,True),(True,True,False),(False,False,None)):
            with self.subTest(trusted=trusted,wrong=wrong):
                e,fingerprint,_,_=self.tls_executor(reply,trusted=trusted)
                self.assertIs(e.observe_tls('app.example.test','a'*64,'b'*64 if wrong else fingerprint),wanted)
        e,fingerprint,_,_=self.tls_executor(reply,hostname='other.example.test')
        self.assertIsNone(e.observe_tls('app.example.test','a'*64,fingerprint))

    def test_tls_duplicate_identity_and_oversized_headers_unknown(self):
        for reply in (b'HTTP/1.1 200 OK\r\nX-Deploy-Monitor-Route: '+b'a'*64+b'\r\nX-Deploy-Monitor-Route: '+b'a'*64+b'\r\n\r\n', b'HTTP/1.1 200 OK\r\nLarge: '+b'x'*70000):
            e,fingerprint,_,_=self.tls_executor(reply)
            self.assertIsNone(e.observe_tls('app.example.test','a'*64,fingerprint))

    def test_tls_listener_is_optional_and_rejects_remote_address(self):
        from ingress_apache_executor import ApacheExecutor
        e=object.__new__(ApacheExecutor);e._configure_tls(None,None)
        self.assertIsNone(e.observe_tls('app.example.test','a'*64,'b'*64))
        with self.assertRaises(ValueError):e._configure_tls(('192.0.2.1',443),None)

    def test_tls_wrong_route_and_duplicate_gateway_marker(self):
        reply=b'HTTP/1.1 200 OK\r\nX-Deploy-Monitor-Route: '+b'a'*64+b'\r\n\r\n'
        e,fingerprint,_,_=self.tls_executor(reply)
        self.assertFalse(e.observe_tls('app.example.test','b'*64,fingerprint))
        reply=b'HTTP/1.1 200 OK\r\nX-Deploy-Monitor-Route: '+b'a'*64+b'\r\nX-Deploy-Monitor-Gateway: '+b'b'*64+b'\r\nX-Deploy-Monitor-Gateway: '+b'b'*64+b'\r\n\r\n'
        e,fingerprint,_,_=self.tls_executor(reply)
        self.assertIsNone(e.observe_tls('app.example.test','a'*64,fingerprint))

    def test_tls_handshake_stall_is_bounded(self):
        import socket
        import threading
        import time
        from ingress_apache_executor import ApacheExecutor
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
        e=object.__new__(ApacheExecutor);e.timeout=.05;e._configure_tls(listener.getsockname(),None)
        def stall():
            connection,_=listener.accept()
            with connection:time.sleep(.2)
            listener.close()
        thread=threading.Thread(target=stall);thread.start()
        try:
            started=time.monotonic()
            self.assertIsNone(e.observe_tls('app.example.test','a'*64,'b'*64))
            self.assertLess(time.monotonic()-started,.18)
        finally:thread.join(1)

    def test_challenge_exact_path_host_and_body(self):
        body=b'bounded-challenge-proof'
        e,requests=self.http_executor(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
        self.assertTrue(e.verify_challenge('app.example.test','A_-0123456789012345',body))
        self.assertIn(b'GET /.well-known/acme-challenge/A_-0123456789012345 HTTP/1.1',requests[0])
        self.assertIn(b'Host: app.example.test\r\n',requests[0])

    def test_challenge_redirect_wrong_body_truncation_and_bounds(self):
        body=b'bounded-challenge-proof'
        for response,wanted in ((b'HTTP/1.1 302 Found\r\nLocation: https://evil.test/\r\n\r\n',False),
                                (b'HTTP/1.1 200 OK\r\nContent-Length: 23\r\n\r\n'+b'x'*23,False),
                                (b'HTTP/1.1 200 OK\r\nContent-Length: 23\r\n\r\nshort',None),
                                (b'HTTP/1.1 200 OK\r\nContent-Length: 5000\r\n\r\n',None),
                                (b'HTTP/1.1 200 OK\r\nContent-Length: 23\r\nContent-Length: 23\r\n\r\n'+body,None),
                                (b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n',None)):
            with self.subTest(response=response):
                e,_=self.http_executor(response)
                self.assertIs(e.verify_challenge('app.example.test','A'*16,body),wanted)

    def test_challenge_rejects_input_before_connecting(self):
        from ingress_apache_executor import ApacheExecutor
        e=object.__new__(ApacheExecutor)
        for token,body in (('../'+'a'*20,b'x'*16),('a'*15,b'x'*16),('a'*129,b'x'*16),('a'*16,b'x'*4097),('a'*16,'x'*16)):
            with self.assertRaises(ValueError):e.verify_challenge('app.example.test',token,body)

    def test_challenge_read_timeout_is_bounded(self):
        import socket
        import threading
        import time
        from ingress_apache_executor import ApacheExecutor
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
        e=object.__new__(ApacheExecutor);e.timeout=.05;e.listener=listener.getsockname()
        def stall():
            connection,_=listener.accept()
            with connection:
                connection.recv(4096);connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 16\r\n\r\n');time.sleep(.2)
            listener.close()
        thread=threading.Thread(target=stall);thread.start()
        try:
            started=time.monotonic();self.assertIsNone(e.verify_challenge('app.example.test','a'*16,b'x'*16))
            self.assertLess(time.monotonic()-started,.18)
        finally:thread.join(1)
