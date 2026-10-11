"""Actual installed Apache executor acceptance on an ephemeral loopback listener."""
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
import urllib.request
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from ingress_host import HostAdapter
from ingress_apache_executor import ApacheExecutor

class Backend(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.end_headers();self.wfile.write(b'executor-fixture')
    def log_message(self,*args):pass

def main():
    binary='/usr/sbin/apache2'
    before=subprocess.run(['pgrep','-x','apache2'],capture_output=True,text=True).stdout.split()
    with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    backend=http.server.ThreadingHTTPServer(('127.0.0.1',0),Backend)
    thread=threading.Thread(target=backend.serve_forever,daemon=True);thread.start()
    process=None
    try:
        with tempfile.TemporaryDirectory(prefix='deploy-monitor-apache-executor-') as directory:
            root=Path(directory)
            for child in ('managed','state'):(root/child).mkdir(mode=0o700)
            modules=['mpm_event','authz_core','authz_host','headers','proxy','proxy_http']
            config=root/'httpd.conf';pid=root/'httpd.pid'
            text=''.join('LoadModule '+m+'_module /usr/lib/apache2/modules/mod_'+m+'.so\n' for m in modules)
            text+=f'ServerRoot {root}\nServerName localhost\nListen 127.0.0.1:{port}\nPidFile {pid}\nErrorLog {root}/error.log\nLogLevel warn\n'
            for child in ('sites-available','sites-enabled','conf'):(root/child).mkdir(mode=0o700)
            alias_config=root/'conf'/'aliases.conf'
            alias_config.write_text('ServerAlias alias.fixture.test *.reserved.fixture.test\n');alias_config.chmod(0o600)
            sentinel=root/'sites-available'/'sentinel.conf'
            sentinel.write_text(f'<VirtualHost 127.0.0.1:{port}>\nServerName sentinel.fixture.test\nInclude {alias_config}\nProxyPass / http://127.0.0.1:{backend.server_port}/\n</VirtualHost>\n')
            sentinel.chmod(0o600)
            enabled=root/'sites-enabled'/'sentinel.conf';enabled.symlink_to(sentinel)
            text+=f'Include {enabled}\nIncludeOptional {root}/managed/*.conf\n';config.write_text(text);config.chmod(0o600)
            initial=hashlib.sha256(sentinel.read_bytes()).hexdigest()
            argv=(binary,'-f',str(config),'-DFOREGROUND')
            executor=ApacheExecutor(executable=binary,config=str(config),pid_file=str(pid),listener=('127.0.0.1',port),expected_argv=argv,owner_uid=os.getuid(),timeout=5)
            assert executor.running() is False
            process=subprocess.Popen(argv,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            deadline=time.monotonic()+8
            while not executor.running():
                if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
                if time.monotonic()>deadline:raise RuntimeError('Apache identity did not become ready')
                time.sleep(.05)
            executor.configtest()
            inventory=executor.inventory()
            names={name for row in inventory for name in row['names']}
            assert {'sentinel.fixture.test','alias.fixture.test','*.reserved.fixture.test'}<=names,inventory
            policy={'managed_dir':str(root/'managed'),'state_dir':str(root/'state'),'listener':{'address':'127.0.0.1','port':port},'routes':{'a'*32:{'hostname':'dev.fixture.test','zone':'fixture.test','port':backend.server_port,'revision':1}}}
            policy_file=root/'policy.json';policy_file.write_text(json.dumps(policy));policy_file.chmod(0o600)
            adapter=HostAdapter(policy_file,executor,owner_uid=os.getuid())
            request={'route_id':'a'*32,'operation_id':'b'*32,'expected_digest':None}
            stage=adapter.stage_http(request)
            try:adapter.activate_http(dict(request,digest=stage['digest']))
            except ValueError:
                deadline=time.monotonic()+5
                while True:
                    try:
                        if adapter.recover({'route_id':'a'*32})['status']=='active':break
                    except ValueError:
                        if time.monotonic()>deadline:raise
                    time.sleep(.05)
            assert adapter.verify({'route_id':'a'*32})['status']=='active'
            assert executor.observe_absence('unassigned.fixture.test') is True
            req=urllib.request.Request(f'http://127.0.0.1:{port}/',headers={'Host':'sentinel.fixture.test'})
            with urllib.request.urlopen(req,timeout=2) as response:assert response.read()==b'executor-fixture'
            assert hashlib.sha256(sentinel.read_bytes()).hexdigest()==initial
            process.terminate();process.wait(timeout=5)
            assert executor.running() is False
            try:executor.reload()
            except Exception:pass
            else:raise AssertionError('Stopped daemon was reloaded/started')
            report={'scope':'local-installed-apache-executor','real_inventory_aliases':True,'included_aliases_and_enabled_symlink':True,'pid_identity_verified':True,'http_activation_verified':True,'sentinel_preserved':True,'stopped_daemon_not_started':True,'production':False,'tls_verified':False}
    finally:
        if process:
            if process.poll() is None:process.terminate();process.wait(timeout=5)
            if process.stderr:process.stderr.close()
        backend.shutdown();backend.server_close();thread.join(timeout=2)
    after=subprocess.run(['pgrep','-x','apache2'],capture_output=True,text=True).stdout.split()
    assert before==after,(before,after)
    report['cleanup_verified']=True
    (ROOT/'.runtime/ingress-executor-acceptance.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
if __name__=='__main__':main()
