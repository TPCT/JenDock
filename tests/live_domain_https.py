"""Local domain Ready acceptance through actual Nginx, Apache HTTPS and ACME.

Provider metadata is synthetic. Public verifier uses a trusted loopback transport
with normal CA and hostname validation. This is not public Internet acceptance.
"""
import copy
import socket
import ssl
import live_ingress_tls as acme
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request,urlopen
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import ingress_external
import domain_renewal
from ingress_certificates import CertificateManager,run_bounded,CommandError
from ingress_host_tls import HostTLSAdapter
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

CHANGED_RENEWAL='--changed-renewal' in sys.argv
FORCE_RENEWAL=False  # Trusted fixture executor only; never a production request/config field.


def certificates(root,metadata):
    counter={'calls':0,'certonly':0,'renew':0,'renew_commands':[]}
    def executor(argv,*,timeout,env):
        assert '--force-renewal' not in argv,'Production certificate manager must never force renewal'
        if 'renew' in argv and FORCE_RENEWAL:
            assert CHANGED_RENEWAL and metadata['directory_url'].startswith('https://localhost:')
            argv=[*argv,'--force-renewal']
        counter['calls']+=1
        if 'certonly' in argv:counter['certonly']+=1
        if 'renew' in argv:counter['renew']+=1
        name='deploy-monitor-tls-certbot-'+uuid.uuid4().hex
        docker=['docker','create','--entrypoint','/usr/local/bin/certbot','--name',name,'--network','host','--user',f'{os.getuid()}:{os.getgid()}',
            '--label',f'{acme.LABEL}={metadata["run_id"]}','--label',f'com.docker.compose.project={PROJECT}',
            '--cap-drop','ALL','--security-opt','no-new-privileges','--memory','256m','--cpus','1',
            '--mount',f'type=bind,src={root},dst={root}']
        for key,value in env.items():docker+=['--env',key+'='+value]
        cid=acme.command(docker+[acme.IMAGES['certbot']]+argv[1:]).decode().strip()
        metadata['container_ids'].append(cid);acme.write(root/'fixture.json',json.dumps(metadata,indent=2))
        try:
            try:result=run_bounded(['docker','start','-a',cid],timeout=timeout,env={'PATH':os.environ['PATH']})
            except Exception:
                print(acme.command(['docker','logs',cid]).decode()[-2000:],file=sys.stderr)
                raise
            code=json.loads(acme.command(['docker','inspect',cid]))[0]['State']['ExitCode']
            if 'renew' in argv:
                output=result.decode('utf-8','replace').lower()
                counter['renew_commands'].append({'exit_code':code,'classifications':[v for v in ('not yet due','no renewals were attempted','unrecognized arguments','error','failed','permission denied') if v in output]})
            if code:raise CommandError()
            return result
        finally:
            acme.command(['docker','rm','-f',cid]);metadata['container_ids'].remove(cid)
    env={'REQUESTS_CA_BUNDLE':str(root/'trust.pem'),'HOME':str(root/'certbot-work')}
    args=['certbot','register','--non-interactive','--agree-tos','--email','fixture@example.test','--server',metadata['directory_url']]
    for flag,key in (('--config-dir','certbot-config'),('--work-dir','certbot-work'),('--logs-dir','certbot-logs')):args += [flag,str(root/key)]
    executor(args,timeout=60,env=env)
    accounts=list((root/'certbot-config'/'accounts').rglob('regr.json'))
    assert len(accounts)==1
    for name in ('certificate-state','generations'):(root/name).mkdir(mode=0o700)
    executable=root/'certbot-executable';acme.write(executable,'fixture injected executor identity\n')
    config=dict(executable=str(executable),expected_version='5.8.0',server=metadata['directory_url'],account=accounts[0].parent.name,email='fixture@example.test',agree_tos=True,
        config_dir=str(root/'certbot-config'),work_dir=str(root/'certbot-work'),logs_dir=str(root/'certbot-logs'),challenge_root=str(root/'challenge'),
        state_dir=str(root/'certificate-state'),generations_dir=str(root/'generations'),ca_bundle=str(root/'trust.pem'),timeout=90)
    manager=CertificateManager(config,owner_uid=os.getuid(),executor=executor)
    return manager,counter



def changed_generation_acceptance(db,client,manager,root,routes,reloads,provider,gateway,external_verifier,public_transport,certificate_calls):
    global FORCE_RENEWAL
    baseline={};events=[]
    def serving(hostname):
        context=ssl.create_default_context(cafile=str(root/'trust.pem'))
        return public_transport(hostname,['127.0.0.1'],time.monotonic()+10,context)['certificate_fingerprint']
    for route in routes:
        op=domain_execution._operation(db,route['operation_id'])
        journal=json.loads(domain_execution._row(db,route['operation_id'])['journal'])
        certificate=journal['certificate']
        assert certificate['not_after']-time.time()<3*86400
        files=manager.generation_files(op['route_id'],certificate['generation'])
        baseline[route['operation_id']]={'identity':{'route_id':op['route_id'],'operation_id':op['id']},'hostname':route['hostname'],
            'certificate':certificate,'files':{role:(files[role],hashlib.sha256(Path(files[role]).read_bytes()).hexdigest()) for role in ('cert','chain','fullchain','privkey')}}
        assert serving(route['hostname'])==certificate['fingerprint']
    command_before=certificate_calls['renew'];reload_before=len(reloads)
    class ObservePromotion:
        def call(self,method,request):
            result=client.call(method,request)
            if method=='activate_tls':
                saved=json.loads(domain_execution._row(db,request['operation_id'])['journal'])
                assert saved['certificate']['generation']==baseline[request['operation_id']]['certificate']['generation']
                assert not domain_execution.public_status(db,request['operation_id'])['ready']
                events.append({'operation_id':request['operation_id'],'event':'actual-activation-before-metadata-promotion'})
            return result
    FORCE_RENEWAL=True
    try:
        for _ in range(12):
            domain_renewal.process_once(db,worker_id='real-renewal-worker',host_client=ObservePromotion(),key_path='unused',provider_transport=provider)
            rows=[domain_renewal._row(db,op) for op in baseline]
            for row in rows:
                assert row['error_code'] is None,{'phase':row['phase'],'state':row['state'],'error_code':row['error_code']}
                journal=json.loads(domain_execution._row(db,row['operation_id'])['journal'])
                cert=journal['certificate'];old=baseline[row['operation_id']]
                assert serving(old['hostname'])==cert['fingerprint'],'Serving generation diverged from verified committed state'
                if row['state']=='complete':
                    assert cert['generation']!=old['certificate']['generation'] and cert['fingerprint']!=old['certificate']['fingerprint']
                    assert domain_execution._row(db,row['operation_id'])['state']=='awaiting-external'
                    assert not domain_execution.public_status(db,row['operation_id'])['ready']
                else:
                    assert cert==old['certificate'],'Candidate promoted before activation verification'
                for path,digest in old['files'].values():assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest,'Old immutable generation was removed or changed'
            if all(row['state']=='complete' for row in rows):break
        else:raise AssertionError('Renewal scheduler did not complete bounded phase sequence')
    finally:FORCE_RENEWAL=False
    assert certificate_calls['renew']==command_before+2 and len(reloads)==reload_before+2
    assert len(events)==2
    for _ in range(5):
        domain_execution.process_once(db,worker_id='real-renewal-public-observer',gateway=gateway,host_client=client,key_path='unused',provider_transport=provider,external_verifier=external_verifier)
        if all(domain_execution.public_status(db,op)['ready'] for op in baseline):break
    else:raise AssertionError('New generations did not regain Ready through actual external verification')
    result=[]
    for op,old in baseline.items():
        current=json.loads(domain_execution._row(db,op)['journal'])['certificate']
        assert serving(old['hostname'])==current['fingerprint']
        result.append({'hostname':old['hostname'],'old_generation':old['certificate']['generation'],'new_generation':current['generation'],
            'old_fingerprint':old['certificate']['fingerprint'],'new_fingerprint':current['fingerprint'],'old_generation_retained':True,'ready_reverified':True})
    return {'fixture_only_force':True,'pebble_validity_seconds':172800,'scheduler_real_phases':True,'atomic_promotion_observations':events,'routes':result}

SOURCE_NAMES=('domain_execution.py','domain_external_flow.py','ingress_external.py','ingress_host_tls.py','ingress_certificates.py','ingress_runtime.py','ingress_host.py','ingress_host_service.py','ingress_host_transport.py','ingress_host_grants.py','domain_worker.py','domain_renewal.py','tests/live_domain_https.py')
SOURCE_HASHES={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCE_NAMES}

def edge_acceptance(db,docker,apps,ports):
    source_names=SOURCE_NAMES;source_hashes=SOURCE_HASHES
    before=subprocess.run(['pgrep','-x','apache2'],capture_output=True,text=True).stdout.split()
    process=None
    with tempfile.TemporaryDirectory(prefix='deploy-monitor-domain-edge-') as directory:
        root=Path(directory)/'fixture';metadata=acme.prepare(root);(root/'managed').mkdir(mode=0o700);(root/'state').mkdir(mode=0o700)
        if CHANGED_RENEWAL:
            ca_config=json.loads((root/'pebble.json').read_text())
            ca_config['pebble']['profiles']={'default':{'description':'Local scheduler acceptance only','validityPeriod':172800}}
            acme.write(root/'pebble.json',json.dumps(ca_config))
        port=metadata['ports']['http'];binary='/usr/sbin/apache2';config=root/'httpd.conf';pid=root/'httpd.pid'
        modules=['mpm_event','authz_core','authz_host','headers','proxy','proxy_http','alias','mime','ssl','socache_shmcb']
        text=''.join('LoadModule '+m+'_module /usr/lib/apache2/modules/mod_'+m+'.so\n' for m in modules)
        text+=f'ServerRoot {root}\nServerName localhost\nListen 127.0.0.1:{port}\nPidFile {pid}\nErrorLog {root}/error.log\n'
        sentinel=root/'sentinel.conf'
        sentinel.write_text(f'<VirtualHost 127.0.0.1:{port}>\nServerName sentinel.fixture.test\nServerAlias *.reserved.fixture.test\nProxyPass / http://127.0.0.1:{ports["development"]}/\n</VirtualHost>\n');sentinel.chmod(0o600)
        text+=f'TypesConfig /etc/mime.types\nListen 127.0.0.1:{metadata["ports"]["https"]}\nInclude {sentinel}\nIncludeOptional {root}/managed/*.conf\n';config.write_text(text);config.chmod(0o600)
        sentinel_hash=hashlib.sha256(sentinel.read_bytes()).hexdigest()
        argv=(binary,'-f',str(config),'-DFOREGROUND')
        policy={'managed_dir':str(root/'managed'),'state_dir':str(root/'state'),'listener':{'address':'127.0.0.1','port':port},'routes':{}}
        policy_file=root/'policy.json';policy_file.write_text(json.dumps(policy));policy_file.chmod(0o600)
        database=Path(db.execute('PRAGMA database_list').fetchone()[2])
        credentials=root/'console.json'
        credentials.write_text(json.dumps({'username':'domain-fixture-owner','salt':'ab'*16,'password_hash':hashlib.pbkdf2_hmac('sha256',b'local-test-only',bytes.fromhex('ab'*16),260000).hex()}));credentials.chmod(0o600)
        console_config=root/'console-config.json';console_config.write_text('{}')
        socket_path=root/'host.sock';stop=threading.Event();ready=threading.Event();errors=[];reloads=[]
        def counted_reload():reloads.append(time.time());return real_reload()
        def host_thread():
            connection=connect(database);server=None
            try:
                adapter=HostAdapter(policy_file,executor,owner_uid=os.getuid(),challenge_root=root/'challenge')
                issuer=HostGrantIssuer(adapter,connection,verify_gateway=DockerGatewayVerifier(docker),resolve_principal=domain_execution.resolve_authority)
                tls=HostTLSAdapter(adapter,manager,listener={'address':'127.0.0.1','port':metadata['ports']['https']})
                server=HostServer(socket_path,HostService(adapter,issuer,tls=tls,available_ports=lambda:{'ports':[free_port()],'observed':time.time()}),allowed_uids={os.getuid()},owner_uid=os.getuid(),timeout=30).open()
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
            compose_ca=['docker','compose','-p',PROJECT,'-f',str(root/'compose.json')]
            acme.command(compose_ca+['up','-d'])
            deadline=time.monotonic()+20
            while True:
                try:issuing=acme.read_issuing_root(metadata);break
                except Exception:
                    if time.monotonic()>deadline:raise
                    time.sleep(.2)
            acme.write(root/'trust.pem',(root/'transport-ca.pem').read_text()+Path(issuing['path']).read_text())
            executor=ApacheExecutor(executable=binary,config=str(config),pid_file=str(pid),listener=('127.0.0.1',port),expected_argv=argv,owner_uid=os.getuid(),timeout=5,tls_listener=('127.0.0.1',metadata['ports']['https']),tls_ca_file=str(root/'trust.pem'))
            real_reload=executor.reload
            executor.reload=counted_reload
            manager,certificate_calls=certificates(root,metadata)
            process=subprocess.Popen(argv,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            deadline=time.monotonic()+8
            while not executor.running():
                if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
                if time.monotonic()>deadline:raise RuntimeError('Apache did not become ready')
                time.sleep(.05)
            sentinel_before=request(port,'sentinel.fixture.test')
            ingress.initialize(db)
            provider_metadata={'api_access':'verified','zone_read':'verified','token_kind':'account','account_id':'a'*32,'zones':[{'id':'b'*32,'name':'fixture.test','status':'active','account_id':'a'*32}]}
            now=time.time();db.execute('INSERT INTO integration_connections VALUES(?,?,?,?,?,?,?,?,?)',('cloudflare',1,b'synthetic-unused',1,registry.canonical(provider_metadata),'{}',now,now,'fixture'))
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
                        if method=='activate_tls' and self.armed:
                            self.armed=False;raise TimeoutError('Synthetic lost response after actual host operation')
                        return result
                lossy=LostReply();routes=[]
                def public_transport(hostname,addresses,deadline,context):
                    assert addresses==['127.0.0.1']
                    with socket.create_connection(('127.0.0.1',metadata['ports']['https']),timeout=10) as raw:
                        with context.wrap_socket(raw,server_hostname=hostname) as secured:
                            secured.sendall(('GET '+ingress_external.PATH+' HTTP/1.1\r\nHost: '+hostname+'\r\nConnection: close\r\n\r\n').encode())
                            cert=secured.getpeercert(binary_form=True)
                            body=ingress_external._response(secured,deadline)
                            return {'status':200,'body':body,'peer_address':'127.0.0.1','certificate_fingerprint':hashlib.sha256(cert).hexdigest()}
                def external_verifier(hostname,token):
                    return ingress_external.verify(hostname,token,ca_file=str(root/'trust.pem'),resolver=lambda *_:['127.0.0.1'],transport=public_transport)

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
                    def readiness(self,*args,**kwargs):return gateway.readiness(*args,**kwargs)
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
                    for _ in range(30):
                        domain_execution.process_once(db,worker_id='live-domain-worker',gateway=interrupted_gateway,host_client=lossy,key_path='unused',provider_transport=provider,external_verifier=external_verifier)
                        observed=action(env,'domain-operation',operation_id=operation)['operation']
                        if observed['status']=='reconciliation-required':
                            count=len(reloads);action(env,'domain-resume',operation_id=operation);resumed=True
                            domain_execution.process_once(db,worker_id='live-domain-worker',gateway=interrupted_gateway,host_client=client,key_path='unused',provider_transport=provider,external_verifier=external_verifier)
                            assert len(reloads)==count,'Reconciliation repeated Apache reload'
                        elif observed['status']=='ready':break
                        elif observed['status'] in ('failed','awaiting-external'):raise AssertionError(observed)
                    final=action(env,'domain-operation',operation_id=operation)['operation']
                    assert final['status']=='ready' and final['dns']['status']=='verified' and final['ready'],final
                    assert final['tls']['status']=='verified',final
                    assert certificate_calls['certonly']==len(routes)+1
                    try:external_verifier(hostname,'0'*64)
                    except ingress_external.ExternalError:pass
                    else:raise AssertionError('Wrong route proof was accepted')
                    accepted_operation=domain_execution._operation(db,operation)
                    journal=json.loads(domain_execution._row(db,operation)['journal'])
                    renewal={'route_id':accepted_operation['route_id'],'operation_id':operation,'renewal_id':uuid.uuid4().hex,'expected_generation':journal['certificate']['generation']}
                    # Persist the caller attempt before the actual request, then
                    # reconcile and replay using that same durable identity.
                    acme.write(root/('renewal-'+env+'.json'),json.dumps(renewal))
                    reload_count=len(reloads)
                    count=dict(certificate_calls)
                    absent=client.call('reconcile_certificate',{k:renewal[k] for k in ('route_id','operation_id','renewal_id')})
                    assert absent['status']=='absent' and certificate_calls==count,absent
                    renewed=client.call('renew_certificate',renewal)
                    if renewed['status']!='verified':
                        saved=manager._journal(renewal['route_id']) or {}
                        diagnostic={'result':renewed,'commands':certificate_calls,'journal':{k:saved.get(k) for k in ('state','attempt','process_ended','command_succeeded')}}
                        renewal_text=(root/'certbot-config'/'renewal'/(saved['name']+'.conf')).read_text()
                        diagnostic['renewal_sections']=[line.strip() for line in renewal_text.splitlines() if line.strip().startswith('[')]
                        diagnostic['renewal_keys']=[line.split('=',1)[0].strip() for line in renewal_text.splitlines() if '=' in line and not line.strip().startswith('#')]
                        try:manager._lineage(saved['name'],hostname)
                        except Exception as error:
                            message=str(error).replace(str(root),'<fixture>')
                            import re
                            diagnostic['lineage_error']={'type':type(error).__name__,'message':re.sub(r'[a-f0-9]{32,}', '<id>',message)[:240]}
                        (ROOT/'.runtime/domain-https-renewal-diagnostic.json').write_text(json.dumps(diagnostic,indent=2))
                    assert renewed['status']=='verified',renewed
                    assert renewed['sans']==[hostname],renewed
                    renewal_config=(root/'certbot-config'/'renewal'/(manager._journal(renewal['route_id'])['name']+'.conf')).read_text()
                    ari_lines=renewal_config.split('[acme_renewal_info]',1)
                    assert len(ari_lines)==2,'Real Certbot did not emit ARI metadata'
                    ari_keys=[line.split('=',1)[0].strip() for line in ari_lines[1].splitlines() if '=' in line and not line.strip().startswith('#')]
                    assert ari_keys==['ari_retry_after'],ari_keys
                    assert renewed['generation']==renewal['expected_generation'],'Fresh certificate unexpectedly renewed; inspect due policy'
                    count=dict(certificate_calls)
                    reconciled=client.call('reconcile_certificate',{k:renewal[k] for k in ('route_id','operation_id','renewal_id')})
                    replayed=client.call('renew_certificate',renewal)
                    (ROOT/'.runtime/domain-https-renewal-observation.json').write_text(json.dumps({'environment':env,'initial':renewed,'reconciled':reconciled,'replayed':replayed,'commands_before_replay':count,'commands_after_replay':certificate_calls,'reloads_before':reload_count,'reloads_after':len(reloads)},indent=2))
                    stable=('route_id','operation_id','renewal_id','status','sans','generation','fingerprint','not_after')
                    assert all(reconciled[k]==renewed[k] and replayed[k]==renewed[k] for k in stable)
                    assert reconciled['observed_at']>=renewed['observed_at'] and replayed['observed_at']>=reconciled['observed_at']
                    assert certificate_calls==count and len(reloads)==reload_count,'Renewal replay repeated a command/reload'
                    assert certificate_calls['renew']==len(routes)+1
                    expected=PROJECT+':'+env+':v1';assert request(port,hostname)==(200,expected)
                    routes.append({'environment':env,'hostname':hostname,'operation_id':operation,'status':final['status'],'dns':final['dns'],'tls':final['tls'],'external':final['external'],'lost_reply_resumed':resumed,'renewal_metadata':{'section':'acme_renewal_info','keys':ari_keys},'renewal_noop_verified':True,'renewal_same_id_replay_without_command':True})
                changed=changed_generation_acceptance(db,client,manager,root,routes,reloads,provider,interrupted_gateway,external_verifier,public_transport,certificate_calls) if CHANGED_RENEWAL else None
                assert any(row['lost_reply_resumed'] for row in routes)
                assert len(gateway_reloads)==2 and gateway_lost[0],'Gateway reconciliation repeated reload'
                assert len(writes)==2;assert request(port,'sentinel.fixture.test')==sentinel_before
                assert hashlib.sha256(sentinel.read_bytes()).hexdigest()==sentinel_hash
                report={'changed_generation_renewal':changed,'source_sha256':source_hashes,'routes':routes,'authenticated_http':True,'actual_unix_rpc':True,'actual_apache':True,'actual_gateway_additions':True,'synthetic_dns_writes':len(writes),'dns_live':False,'tls_verified':True,'ready':True,'public_internet_verified':False,'certificate_calls':certificate_calls,'images':acme.IMAGES,'reloads':len(reloads),'gateway_reloads':len(gateway_reloads),'gateway_lost_reply_reconciled':gateway_lost[0]}
        finally:
            try:
                if http:http.shutdown();http.server_close();http_thread.join(timeout=5)
                stop.set()
                if thread.is_alive():
                    try:
                        wake=socket.socket(socket.AF_UNIX);wake.connect(str(socket_path));wake.close()
                    except OSError:pass
                    thread.join(timeout=35)
                if process:
                    if process.poll() is None:process.terminate();process.wait(timeout=5)
                    if process.stderr:process.stderr.close()
            finally:
                discovered=acme.command(['docker','ps','-aq','--no-trunc','--filter',f'label={acme.LABEL}={metadata["run_id"]}']).decode().split()
                metadata['container_ids']=discovered;acme.cleanup_owned(metadata)
            assert not thread.is_alive(),'Host fixture thread did not stop'
    after=subprocess.run(['pgrep','-x','apache2'],capture_output=True,text=True).stdout.split();assert after==before
    assert source_hashes=={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in source_names},'Source changed during acceptance'
    report['apache_cleanup_verified']=True
    (ROOT/'.runtime/domain-https-acceptance.json').write_text(json.dumps(report,indent=2)+'\n')
    return report

if __name__=='__main__':main(edge_acceptance=edge_acceptance)
