"""Real managed apps -> gateways -> issued host grants -> Apache loopback HTTP.

Provider metadata is synthetic. No live DNS, certificates or public routing.
"""
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
        principal=ops.Principal('domain-fixture-owner','console',frozenset({'domains:read','domains:create'}))
        current_principal=principal
        adapter=HostAdapter(policy_file,executor,owner_uid=os.getuid())
        issuer=HostGrantIssuer(adapter,db,verify_gateway=DockerGatewayVerifier(docker),resolve_principal=lambda db,operation:current_principal)
        adapter.authorize_route=issuer.authorize_route
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
            now=time.time()
            db.execute('INSERT INTO integration_connections VALUES(?,?,?,?,?,?,?,?,?)',('cloudflare',1,b'synthetic-unused',1,registry.canonical(metadata),'{}',now,now,'fixture'))
            routes=[]
            with patch.dict(os.environ,{'INGRESS_ORIGIN_IPV4':'192.0.2.10'}):
                for env in registry.ENVIRONMENTS:
                    values={'service_id':apps[env]['id'],'hostname':env+'.fixture.test','zone_id':'b'*32,'dns_type':'A','target':'192.0.2.10','proxied':False,'ttl':300}
                    preview=ingress.preview(db,principal,PROJECT,env,values)
                    accepted=ingress.accept(db,principal,PROJECT,env,dict(values,**{k:preview[k] for k in ingress.PINS}),str(uuid.uuid4()))
                    route=accepted['route']['id'];operation=accepted['operation']['id']
                    issued=issuer.issue({'route_id':route,'operation_id':operation,'expected_revision':0})
                    assert issued['grant']['port']==ports[env]
                    intent={'route_id':route,'operation_id':operation,'expected_digest':None}
                    staged=adapter.stage_http(intent)
                    current_principal=dataclasses.replace(principal,scopes=frozenset())
                    try:adapter.activate_http(dict(intent,digest=staged['digest']))
                    except ValueError:pass
                    else:raise AssertionError('Revoked domain principal activated a route')
                    assert not (root/'managed'/(route+'.conf')).exists()
                    current_principal=principal
                    original_configtest=executor.configtest
                    def revoke_during_configtest():
                        nonlocal current_principal
                        original_configtest()
                        current_principal=dataclasses.replace(principal,scopes=frozenset())
                    executor.configtest=revoke_during_configtest
                    try:
                        try:adapter.activate_http(dict(intent,digest=staged['digest']))
                        except ValueError:pass
                        else:raise AssertionError('Revocation during configtest activated a route')
                    finally:executor.configtest=original_configtest
                    assert not (root/'managed'/(route+'.conf')).exists(), 'Denied candidate remained in Apache includes'
                    executor.configtest();executor.reload()
                    deadline=time.monotonic()+5
                    while executor.observe_absence(env+'.fixture.test') is not True:
                        if time.monotonic()>deadline:raise AssertionError('Sibling reload exposed denied route')
                        time.sleep(.05)
                    current_principal=principal
                    try:adapter.activate_http(dict(intent,digest=staged['digest']))
                    except ValueError:
                        deadline=time.monotonic()+5
                        while True:
                            try:
                                if adapter.recover({'route_id':route})['status']=='active':break
                            except ValueError:
                                if time.monotonic()>deadline:raise
                            time.sleep(.1)
                    assert adapter.verify({'route_id':route})['status']=='active'
                    expected=PROJECT+':'+env+':v1'
                    assert request(port,env+'.fixture.test')==(200,expected)
                    replay=issuer.issue({'route_id':route,'operation_id':operation,'expected_revision':0})
                    assert replay==issued
                    routes.append({'environment':env,'route_id':route,'body':expected})
                for row in routes:assert request(port,row['environment']+'.fixture.test')==(200,row['body'])
                assert request(port,'sentinel.fixture.test')==sentinel_before
                assert hashlib.sha256(sentinel.read_bytes()).hexdigest()==sentinel_hash
            report={'routes':routes,'simultaneous_http':True,'grant_derived_from_actual_gateway':True,'revoked_activation_rejected':True,'revoked_candidate_removed_before_sibling_reload':True,'sentinel_preserved':True,'dns_live':False,'tls_verified':False}
        finally:
            if process:
                if process.poll() is None:process.terminate();process.wait(timeout=5)
                if process.stderr:process.stderr.close()
    after=subprocess.run(['pgrep','-x','apache2'],capture_output=True,text=True).stdout.split()
    assert after==before
    report['apache_cleanup_verified']=True
    (ROOT/'.runtime/ingress-domain-acceptance.json').write_text(json.dumps(report,indent=2)+'\n')
    return report

if __name__=='__main__':main(edge_acceptance=edge_acceptance)
