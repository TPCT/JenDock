"""Authenticated domain orchestration through real Docker, Unix RPC and Apache.

Provider metadata is synthetic. No live DNS, certificates or public routing.
"""
import copy
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request,urlopen
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cloudflare_dns
import domain_execution
import domain_worker
import worker
import ingress_runtime
from ingress_host_service import HostService
from ingress_host_transport import HostServer,HostClient
from server import Application,handler
from store import connect
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import application_operations as ops
import ingress
import registry
from ingress_host import HostAdapter
from ingress_host_grants import HostGrantIssuer,DockerGatewayVerifier
from ingress_apache_executor import ApacheExecutor
from live_ingress_runtime import main,free_port,request,PROJECT


def edge_acceptance(db,docker,apps,ports):
    before=subprocess.run(['pgrep','-x','apache2'],capture_output=True,text=True).stdout.split()
    process=None
    with tempfile.TemporaryDirectory(prefix='deploy-monitor-domain-edge-') as directory:
        root=Path(directory);(root/'managed').mkdir(mode=0o700);(root/'state').mkdir(mode=0o700)
        port=free_port();binary='/usr/sbin/apache2';config=root/'httpd.conf';pid=root/'httpd.pid'
        modules=['mpm_event','authz_core','authz_host','headers','proxy','proxy_http']
        text=''.join('LoadModule '+m+'_module /usr/lib/apache2/modules/mod_'+m+'.so\n' for m in modules)
        text+=f'ServerRoot {root}\nServerName localhost\nListen 127.0.0.1:{port}\nPidFile {pid}\nErrorLog {root}/error.log\n'
        sentinel=root/'sentinel.conf'
        sentinel.write_text(f'<VirtualHost 127.0.0.1:{port}>\nServerName sentinel.fixture.test\nServerAlias *.reserved.fixture.test\nProxyPass / http://127.0.0.1:{ports["development"]}/\n</VirtualHost>\n');sentinel.chmod(0o600)
        text+=f'Include {sentinel}\nIncludeOptional {root}/managed/*.conf\n';config.write_text(text);config.chmod(0o600)
        sentinel_hash=hashlib.sha256(sentinel.read_bytes()).hexdigest()
        argv=(binary,'-f',str(config),'-DFOREGROUND')
        executor=ApacheExecutor(executable=binary,config=str(config),pid_file=str(pid),listener=('127.0.0.1',port),expected_argv=argv,owner_uid=os.getuid(),timeout=5)
        policy={'managed_dir':str(root/'managed'),'state_dir':str(root/'state'),'listener':{'address':'127.0.0.1','port':port},'routes':{}}
        policy_file=root/'policy.json';policy_file.write_text(json.dumps(policy));policy_file.chmod(0o600)
        database=Path(db.execute('PRAGMA database_list').fetchone()[2])
        credentials=root/'console.json'
        credentials.write_text(json.dumps({'username':'domain-fixture-owner','salt':'ab'*16,'password_hash':hashlib.pbkdf2_hmac('sha256',b'local-test-only',bytes.fromhex('ab'*16),260000).hex()}));credentials.chmod(0o600)
        console_config=root/'console-config.json';console_config.write_text('{}')
        socket_path=root/'host.sock';stop=threading.Event();ready=threading.Event();errors=[];reloads=[]
        real_reload=executor.reload
        def counted_reload():reloads.append(time.time());return real_reload()
        executor.reload=counted_reload
        def host_thread():
            connection=connect(database);server=None
            try:
                adapter=HostAdapter(policy_file,executor,owner_uid=os.getuid())
                issuer=HostGrantIssuer(adapter,connection,verify_gateway=DockerGatewayVerifier(docker),resolve_principal=domain_execution.resolve_authority)
                server=HostServer(socket_path,HostService(adapter,issuer,available_ports=lambda:{'ports':[free_port()],'observed':time.time()}),allowed_uids={os.getuid()},owner_uid=os.getuid(),timeout=30).open()
                ready.set()
                while not stop.is_set():server.serve_once()
            except Exception as error:errors.append(repr(error));ready.set()
            finally:
                if server:server.close()
                connection.close()
        thread=threading.Thread(target=host_thread,daemon=True);http=None;http_thread=None
        records={};writes=[]
        def provider(method,path,token,body=None):
            assert not db.in_transaction
            if method=='GET' and '/dns_records' not in path:return {'success':True,'result':{'id':'b'*32,'name':'fixture.test','status':'active','account':{'id':'a'*32}}}
            if method=='GET':
                from urllib.parse import parse_qs,urlsplit
                hostname=parse_qs(urlsplit(path).query)['name.exact'][0]
                return {'success':True,'result':copy.deepcopy(records.get(hostname,[])),'result_info':{'page':1,'total_pages':1}}
            assert method=='POST';writes.append(path);record=dict(body,id=uuid.uuid4().hex);records[body['name']]=[record]
            return {'success':True,'result':copy.deepcopy(record)}
        try:
            process=subprocess.Popen(argv,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            deadline=time.monotonic()+8
            while not executor.running():
                if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
                if time.monotonic()>deadline:raise RuntimeError('Apache did not become ready')
                time.sleep(.05)
            sentinel_before=request(port,'sentinel.fixture.test')
            ingress.initialize(db)
            metadata={'api_access':'verified','zone_read':'verified','token_kind':'account','account_id':'a'*32,'zones':[{'id':'b'*32,'name':'fixture.test','status':'active','account_id':'a'*32}]}
            now=time.time();db.execute('INSERT INTO integration_connections VALUES(?,?,?,?,?,?,?,?,?)',('cloudflare',1,b'synthetic-unused',1,registry.canonical(metadata),'{}',now,now,'fixture'))
            environment={'INGRESS_ORIGIN_IPV4':'192.0.2.10','INGRESS_ENABLED':'1','CREDENTIALS_FILE':str(credentials),'INGRESS_HOST_SOCKET':str(socket_path),'INGRESS_GATEWAY_IMAGE':'nginxinc/nginx-unprivileged@sha256:15c994d10d6d78658721c3bcafff14cb281fba2a4bdf9d5ba92c416a472516e3'}
            with patch.dict(os.environ,environment),patch.object(cloudflare_dns.integrations,'credential_for_use',return_value='synthetic-provider-only'):
                thread.start();assert ready.wait(10) and not errors,errors
                app=Application(console_config,database,credentials);app.secure=False;app.cookie_name='monitor_session'
                http=ThreadingHTTPServer(('127.0.0.1',0),handler(app));origin=f'http://127.0.0.1:{http.server_port}';app.origin=origin
                http_thread=threading.Thread(target=http.serve_forever,daemon=True);http_thread.start()
                cookie=None;csrf=None
                def post(path,body):
                    headers={'Origin':origin,'Content-Type':'application/json'}
                    if cookie:headers.update(Cookie=cookie,**{'X-CSRF-Token':csrf})
                    with urlopen(Request(origin+path,data=json.dumps(body).encode(),headers=headers),timeout=20) as response:return json.load(response),response.headers
                logged,headers=post('/_deploy/api/login',{'username':'domain-fixture-owner','password':'local-test-only'})
                cookie=headers['Set-Cookie'].split(';',1)[0];csrf=logged['csrf']
                def action(env,name,**values):return post('/_deploy/api/action',dict(action=name,project=PROJECT,environment=env,**values))[0]
                client=HostClient(socket_path,expected_server_uid=os.getuid(),timeout=30)
                runtime=db.execute('SELECT root FROM ingress_runtime_intents LIMIT 1').fetchone()[0]
                compose=next(item['Path'] for item in json.loads(subprocess.check_output([docker,'info','--format','{{json .ClientInfo.Plugins}}'],text=True)) if item['Name']=='compose')
                gateway=domain_worker.Gateway(root=runtime,docker=docker,compose=compose,host_client=client,image='unused-existing-gateway',resources={'memory_mib':64,'cpu':.25})
                class LostReply:
                    armed=True
                    def call(self,method,request):
                        result=client.call(method,request)
                        if method=='activate_http' and self.armed:
                            self.armed=False;raise TimeoutError('Synthetic lost response after actual host operation')
                        return result
                lossy=LostReply();routes=[]
                gateway_reloads=[];gateway_lost=[False];real_command=ingress_runtime._command
                def interrupted_command(command,*args,**kwargs):
                    result=real_command(command,*args,**kwargs)
                    if 'nginx' in command and '-s' in command and 'reload' in command:
                        gateway_reloads.append(command)
                        if not gateway_lost[0]:
                            gateway_lost[0]=True
                            raise RuntimeError('Synthetic lost gateway reload response')
                    return result
                class InterruptedGateway:
                    def __call__(self,*args,**kwargs):
                        with patch.object(ingress_runtime,'_command',side_effect=interrupted_command):return gateway(*args,**kwargs)
                interrupted_gateway=InterruptedGateway()
                for env in registry.ENVIRONMENTS:
                    worker.observe_domain_executor(db,domain_worker.configuration(),client)
                    hostname='second-'+env+'.fixture.test'
                    values={'service_id':apps[env]['id'],'hostname':hostname,'zone_id':'b'*32,'dns_type':'A','target':'192.0.2.10','proxied':False,'ttl':300}
                    preview=action(env,'domain-preview',values=values)
                    submitted=dict(values,**{key:preview[key] for key in ingress.PINS});request_id=str(uuid.uuid4())
                    accepted=action(env,'domain-create',values=submitted,request_id=request_id);operation=accepted['operation']['id']
                    assert action(env,'domain-create',values=submitted,request_id=request_id)['operation']['id']==operation
                    resumed=False
                    for _ in range(12):
                        domain_execution.process_once(db,worker_id='live-domain-worker',gateway=interrupted_gateway,host_client=lossy,key_path='unused',provider_transport=provider)
                        observed=action(env,'domain-operation',operation_id=operation)['operation']
                        if observed['status']=='reconciliation-required':
                            count=len(reloads);action(env,'domain-resume',operation_id=operation);resumed=True
                            domain_execution.process_once(db,worker_id='live-domain-worker',gateway=interrupted_gateway,host_client=client,key_path='unused',provider_transport=provider)
                            assert len(reloads)==count,'Reconciliation repeated Apache reload'
                        elif observed['status']=='awaiting-tls':break
                        elif observed['status']=='failed':raise AssertionError(observed)
                    final=action(env,'domain-operation',operation_id=operation)['operation']
                    assert final['status']=='awaiting-tls' and final['dns']['status']=='verified' and not final['ready'],final
                    expected=PROJECT+':'+env+':v1';assert request(port,hostname)==(200,expected)
                    routes.append({'environment':env,'hostname':hostname,'operation_id':operation,'status':final['status'],'lost_reply_resumed':resumed})
                assert any(row['lost_reply_resumed'] for row in routes)
                assert len(gateway_reloads)==2 and gateway_lost[0],'Gateway reconciliation repeated reload'
                assert len(writes)==2;assert request(port,'sentinel.fixture.test')==sentinel_before
                assert hashlib.sha256(sentinel.read_bytes()).hexdigest()==sentinel_hash
                report={'routes':routes,'authenticated_http':True,'actual_unix_rpc':True,'actual_apache':True,'actual_gateway_additions':True,'synthetic_dns_writes':len(writes),'dns_live':False,'tls_verified':False,'ready':False,'reloads':len(reloads),'gateway_reloads':len(gateway_reloads),'gateway_lost_reply_reconciled':gateway_lost[0]}
        finally:
            if http:http.shutdown();http.server_close();http_thread.join(timeout=5)
            stop.set()
            if thread.is_alive():
                try:
                    import socket
                    wake=socket.socket(socket.AF_UNIX);wake.connect(str(socket_path));wake.close()
                except OSError:pass
                thread.join(timeout=35)
            if process:
                if process.poll() is None:process.terminate();process.wait(timeout=5)
                if process.stderr:process.stderr.close()
            assert not thread.is_alive(),'Host fixture thread did not stop'
    after=subprocess.run(['pgrep','-x','apache2'],capture_output=True,text=True).stdout.split();assert after==before
    report['apache_cleanup_verified']=True
    (ROOT/'.runtime/domain-execution-acceptance.json').write_text(json.dumps(report,indent=2)+'\n')
    return report

if __name__=='__main__':main(edge_acceptance=edge_acceptance)
