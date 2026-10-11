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
