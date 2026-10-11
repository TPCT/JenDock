"""Real local Apache configtest/reload with a loopback backend and sentinel site.

This verifies the host adapter, not TLS, Cloudflare or the entire domain worker.
"""
import hashlib
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from ingress_host import HostAdapter
from ingress_host_transport import HostClient, HostServer, TransportError


def command(args):
    p=subprocess.run(args,capture_output=True,text=True,timeout=30)
    if p.returncode:raise RuntimeError(p.stderr[:1500])
    return p.stdout


def free_port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


class Backend(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body=('backend:'+self.headers.get('Host','')).encode()
        self.send_response(200);self.end_headers();self.wfile.write(body)
    def log_message(self,*args):pass


def main():
    baseline=command(['docker','ps','-aq','--filter','label=com.docker.compose.project=deploy-monitor']).split()
    image=command(['docker','image','inspect','httpd:2.4-alpine','--format','{{.Id}}']).strip()
    edge_port=free_port();backend=http.server.ThreadingHTTPServer(('127.0.0.1',0),Backend)
    thread=threading.Thread(target=backend.serve_forever,daemon=True);thread.start()
    container=None
    report={'scope':'local-http-host-adapter','tls_verified':False,'provider_verified':False}
    with tempfile.TemporaryDirectory(prefix='deploy-monitor-ingress-apache-') as directory:
        root=Path(directory)
        for name in ('managed','state'): (root/name).mkdir(mode=0o700)
        # Apache reads configuration as root before dropping worker privileges.
        modules=['mpm_event','unixd','authz_core','authz_host','log_config','headers','proxy','proxy_http']
        conf='ServerRoot /usr/local/apache2\n'+''.join('LoadModule '+m+'_module modules/mod_'+m+'.so\n' for m in modules)
        conf+=f'''ServerName localhost
Listen 127.0.0.1:{edge_port}
PidFile /tmp/httpd.pid
User daemon
Group daemon
ErrorLog /proc/self/fd/2
LogLevel warn
Include /fixture/sentinel.conf
IncludeOptional /fixture/managed/*.conf
'''
        (root/'httpd.conf').write_text(conf)
        sentinel=f'''<VirtualHost 127.0.0.1:{edge_port}>
ServerName sentinel.fixture.test
ProxyPreserveHost On
ProxyPass / http://127.0.0.1:{backend.server_port}/
ProxyPassReverse / http://127.0.0.1:{backend.server_port}/
</VirtualHost>
'''
        (root/'sentinel.conf').write_text(sentinel)
        sentinel_hash=hashlib.sha256(sentinel.encode()).hexdigest()
        route='a'*32
        policy={'managed_dir':str(root/'managed'),'state_dir':str(root/'state'),
                'listener':{'address':'127.0.0.1','port':edge_port},
                'routes':{route:{'hostname':'dev.fixture.test','zone':'fixture.test','port':backend.server_port,'revision':1}}}
        (root/'policy.json').write_text(json.dumps(policy));(root/'policy.json').chmod(0o600)
        def probe(host):
            req=urllib.request.Request(f'http://127.0.0.1:{edge_port}/',headers={'Host':host})
            with urllib.request.urlopen(req,timeout=2) as r:return r.status,dict(r.headers),r.read().decode()
        class Executor:
            reload_count=0
            lose_reply=False
            fail_before_reload=False
            invalid_syntax=False
            def inventory(self):
                output=command(['docker','exec',container,'httpd','-S','-f','/fixture/httpd.conf'])
                # Each file is fixture-owned; actual -S must name every config.
                rows=[{'names':['sentinel.fixture.test'],'path':str(root/'sentinel.conf')}]
                for path in (root/'managed').glob('*.conf'):
                    if '/fixture/managed/'+path.name not in output:raise RuntimeError('Managed vhost missing from Apache inventory')
                    names=[line.split()[1] for line in path.read_text().splitlines() if line.strip().startswith('ServerName ')]
                    rows.append({'names':names,'path':str(path)})
                return rows
            def running(self):
                row=json.loads(command(['docker','inspect',container]))[0]
                return row['State']['Running'] and command(['docker','exec',container,'readlink','/proc/1/exe']).strip()=='/usr/local/apache2/bin/httpd'
            def configtest(self):
                original=(root/'httpd.conf').read_text()
                try:
                    if self.invalid_syntax:(root/'httpd.conf').write_text(original+'\nInvalidFixtureDirective\n')
                    command(['docker','exec',container,'httpd','-t','-f','/fixture/httpd.conf'])
                finally:(root/'httpd.conf').write_text(original)
            def reload(self):
                self.reload_count+=1
                if self.fail_before_reload:raise RuntimeError('Synthetic reload failure before signal')
                command(['docker','exec',container,'httpd','-k','graceful','-f','/fixture/httpd.conf'])
                if self.lose_reply:raise RuntimeError('Synthetic lost reload response')
            def observe_absence(self,hostname):
                try:
                    _,headers,_=probe(hostname)
                    return 'X-Deploy-Monitor-Route' not in headers
                except OSError:return None
            def observe(self,hostname,token):
                try:
                    _,headers,_=probe(hostname)
                    return headers.get('X-Deploy-Monitor-Route')==token
                except OSError:return None
        host_server=None; host_thread=None; host_stop=threading.Event()
        try:
            container=command(['docker','run','-d','--network','host','--read-only','--tmpfs','/tmp:rw,nosuid,size=16m',
                '--label','com.docker.compose.project=deploy-monitor','--label','com.docker.compose.service=ingress-edge-fixture',
                '--label','fixture.operation='+uuid.uuid4().hex,'-v',str(root)+':/fixture:ro',image,'httpd','-DFOREGROUND','-f','/fixture/httpd.conf']).strip()
            deadline=time.monotonic()+15
            while True:
                try:
                    assert probe('sentinel.fixture.test')[2]=='backend:sentinel.fixture.test';break
                except OSError:
                    if time.monotonic()>deadline:raise
                    time.sleep(.1)
            executor=Executor();adapter=HostAdapter(root/'policy.json',executor,owner_uid=os.getuid())
            host_server=HostServer(root/'helper.sock',adapter,allowed_uids={os.getuid()},owner_uid=os.getuid(),timeout=10).open()
            host_server.socket.settimeout(.2)
            def serve():
                while not host_stop.is_set():host_server.serve_once()
            host_thread=threading.Thread(target=serve,daemon=True);host_thread.start()
            client=HostClient(root/'helper.sock',expected_server_uid=os.getuid(),timeout=10)
            class RemoteAdapter:
                def stage_http(self,value):return client.call('stage_http',value)
                def activate_http(self,value):return client.call('activate_http',value)
                def verify(self,value):return client.call('verify',value)
                def recover(self,value):return client.call('recover',value)
            adapter=RemoteAdapter()
            request={'route_id':route,'operation_id':'b'*32,'expected_digest':None}
            staged=adapter.stage_http(request)
            adapter.activate_http(dict(request,digest=staged['digest']))
            adapter.verify({'route_id':route})
            assert probe('dev.fixture.test')[2]=='backend:dev.fixture.test'
            assert probe('sentinel.fixture.test')[2]=='backend:sentinel.fixture.test'
            adapter.activate_http(dict(request,digest=staged['digest']))
            assert executor.reload_count==1
            assert hashlib.sha256((root/'sentinel.conf').read_bytes()).hexdigest()==sentinel_hash
            # A real Apache parser failure must restore the previously active file.
            policy['routes'][route]['revision']=2
            (root/'policy.json').write_text(json.dumps(policy))
            update={'route_id':route,'operation_id':'c'*32,'expected_digest':staged['digest']}
            changed=adapter.stage_http(update);executor.invalid_syntax=True
            try:adapter.activate_http(dict(update,digest=changed['digest']))
            except RuntimeError:pass
            else:raise AssertionError('Apache syntax failure was not rejected')
            finally:executor.invalid_syntax=False
            assert executor.reload_count==1
            assert probe('dev.fixture.test')[2]=='backend:dev.fixture.test'
            # The graceful reload succeeds but its caller loses the response.
            executor.lose_reply=True
            try:adapter.activate_http(dict(update,digest=changed['digest']))
            except RuntimeError:pass
            else:raise AssertionError('Lost response did not preserve uncertainty')
            executor.lose_reply=False
            deadline=time.monotonic()+5
            while True:
                try:
                    recovered=adapter.recover({'route_id':route})
                    if recovered['status']=='active':break
                except (ValueError,TransportError):
                    if time.monotonic()>deadline:raise
                time.sleep(.1)
            assert executor.reload_count==2
            assert adapter.verify({'route_id':route})['status']=='active'
            assert probe('sentinel.fixture.test')[2]=='backend:sentinel.fixture.test'
            # A failed candidate reload is restored with observed prior identity.
            # Losing the restoration reply must not trigger a second restoration.
            policy['routes'][route]['revision']=3
            (root/'policy.json').write_text(json.dumps(policy))
            final_request={'route_id':route,'operation_id':'d'*32,'expected_digest':changed['digest']}
            final_stage=adapter.stage_http(final_request)
            executor.fail_before_reload=True
            try:adapter.activate_http(dict(final_request,digest=final_stage['digest']))
            except TransportError:pass
            else:raise AssertionError('Missing candidate reload was not rejected')
            executor.fail_before_reload=False;executor.lose_reply=True
            try:adapter.recover({'route_id':route})
            except TransportError:pass
            else:raise AssertionError('Lost restoration reply did not preserve uncertainty')
            executor.lose_reply=False
            assert executor.reload_count==4
            assert adapter.recover({'route_id':route})['status']=='staged'
            assert executor.reload_count==4
            assert probe('dev.fixture.test')[2]=='backend:dev.fixture.test'
            assert probe('sentinel.fixture.test')[2]=='backend:sentinel.fixture.test'
            assert hashlib.sha256((root/'sentinel.conf').read_bytes()).hexdigest()==sentinel_hash
            report.update(authenticated_unix_transport=True,http_verified=True,replay_without_reload=True,sentinel_preserved=True,
                          syntax_failure_preserved_runtime=True,lost_reply_reconciled_without_reload=True,
                          restored_runtime_verified=True,restoration_reply_reconciled_without_reload=True)
        finally:
            host_stop.set()
            if host_thread:host_thread.join(timeout=2)
            if host_server:host_server.close()
            if container:command(['docker','rm','-f',container])
            backend.shutdown();backend.server_close();thread.join(timeout=3)
    assert command(['docker','ps','-aq','--filter','label=com.docker.compose.project=deploy-monitor']).split()==baseline
    report['cleanup_verified']=True
    (ROOT/'.runtime/ingress-apache-acceptance.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
