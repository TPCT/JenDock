"""Real local ACME acceptance: --prepare writes files; --run starts exact fixtures.

The transport CA authenticates Pebble's API only. Application certificates MUST
come from real Certbot HTTP-01 orders and Pebble's separate issuing root.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import urllib.request
import uuid

PROJECT = 'deploy-monitor'
IMAGES = {
    'pebble': 'ghcr.io/letsencrypt/pebble@sha256:5080492187bea12909c053d7a2b90a9e95104ef1c82381828e7823db71ce7e54',
    'dns': 'ghcr.io/letsencrypt/pebble-challtestsrv@sha256:1994a326ec60e0de5e8051aa192269c1aaeff0969a4b703d11fa51370f3dc1a8',
    'certbot': 'certbot/certbot@sha256:398c47284a6d6782825be71685f677ef3a1e65b8b5c278a8b1e99f6da84b4eb9',
}
LABEL = 'net.shiftcodes.fixture.ingress-tls'


def command(argv):
    return subprocess.run(argv, check=True, capture_output=True, timeout=30).stdout


def free_ports(count):
    # Reservations are released before future startup; Docker/Apache bind is final authority.
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket();sock.bind(('127.0.0.1', 0));sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets: sock.close()


def write(path, value):
    path.write_text(value);path.chmod(0o600)


def transport_identity(root):
    """Local transport trust only; never used as a served application's certificate."""
    command(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2',
             '-subj','/CN=deploy-monitor fixture ACME transport CA','-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign',
             '-keyout',str(root/'transport-ca.key'),'-out',str(root/'transport-ca.pem')])
    command(['openssl','req','-newkey','rsa:2048','-nodes','-subj','/CN=localhost',
             '-keyout',str(root/'transport.key'),'-out',str(root/'transport.csr')])
    write(root/'transport.ext','subjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n')
    command(['openssl','x509','-req','-in',str(root/'transport.csr'),'-CA',str(root/'transport-ca.pem'),
             '-CAkey',str(root/'transport-ca.key'),'-CAcreateserial','-days','2',
             '-extfile',str(root/'transport.ext'),'-out',str(root/'transport.pem')])
    for path in root.iterdir():
        if path.is_file():path.chmod(0o600)


def prepare(root):
    root = Path(root).absolute()
    if root.exists():raise ValueError('Use a new fixture directory; never overwrite an existing fixture')
    root.mkdir(mode=0o700)
    run_id=uuid.uuid4().hex
    ports=dict(zip(('acme','management','dns','dns_management','http','https'),free_ports(6)))
    transport_identity(root)
    for name in ('challenge','certbot-config','certbot-work','certbot-logs'):(root/name).mkdir(mode=0o700)
    config={'pebble':{'listenAddress':f"127.0.0.1:{ports['acme']}",
        'managementListenAddress':f"127.0.0.1:{ports['management']}",
        'certificate':str(root/'transport.pem'),'privateKey':str(root/'transport.key'),
        'httpPort':ports['http'],'tlsPort':ports['https'],'externalAccountBindingRequired':False}}
    write(root/'pebble.json',json.dumps(config,indent=2))
    common={'network_mode':'host','platform':'linux/amd64','user':f'{os.getuid()}:{os.getgid()}',
        'labels':{LABEL:run_id},'read_only':True,'cap_drop':['ALL'],
        'security_opt':['no-new-privileges:true'],'mem_limit':'128m','cpus':0.5,
        'pids_limit':64,'tmpfs':['/tmp:rw,nosuid,noexec,size=16m']}
    services={
      'tls-pebble-'+run_id[:8]:dict(common,image=IMAGES['pebble'],
        volumes=[f'{root}:{root}:ro'],command=['-config',str(root/'pebble.json'),'-strict','-dnsserver',f"127.0.0.1:{ports['dns']}"],
        environment={'PEBBLE_VA_NOSLEEP':'1','PEBBLE_WFE_NONCEREJECT':'0'}),
      'tls-dns-'+run_id[:8]:dict(common,image=IMAGES['dns'],command=[
        '-defaultIPv4','127.0.0.1','-defaultIPv6','',
        '-dnsserver',f"127.0.0.1:{ports['dns']}",'-management',f"127.0.0.1:{ports['dns_management']}",
        '-http01','','-https01','','-tlsalpn01','']),
    }
    write(root/'compose.json',json.dumps({'name':PROJECT,'services':services},indent=2))
    metadata={'stage':'prepared-not-run','run_id':run_id,'root':str(root),'ports':ports,'images':IMAGES,
        'directory_url':f"https://localhost:{ports['acme']}/dir",'transport_ca':str(root/'transport-ca.pem'),
        'container_ids':[],'services':list(services),'application_certificate_source':'real ACME issuance required'}
    write(root/'fixture.json',json.dumps(metadata,indent=2))
    return metadata


def read_issuing_root(metadata):
    """After CA startup, authenticate management TLS before trusting its issuing root."""
    context=ssl.create_default_context(cafile=metadata['transport_ca'])
    url=f"https://localhost:{metadata['ports']['management']}/roots/0"
    # No proxy or redirects: management endpoint is the exact loopback fixture.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):raise ValueError('Unexpected CA redirect')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context),NoRedirect())
    with opener.open(url,timeout=5) as response:
        data=response.read(65537)
        if response.status!=200 or len(data)>65536:raise ValueError('Invalid bounded issuing-root response')
    ssl.PEM_cert_to_DER_cert(data.decode('ascii'))
    path=Path(metadata['root'])/'issuing-root.pem';write(path,data.decode('ascii'))
    return {'path':str(path),'sha256':hashlib.sha256(data).hexdigest()}


def cleanup_owned(metadata, docker='docker'):
    """Only saved exact IDs and matching ownership; never compose down/prune."""
    for container_id in metadata['container_ids']:
        if not re.fullmatch('[a-f0-9]{64}',container_id):raise ValueError('Invalid saved fixture ID')
        row=json.loads(command([docker,'inspect',container_id]))[0]
        labels=row['Config']['Labels'] or {}
        if (row['Id']!=container_id or labels.get(LABEL)!=metadata['run_id'] or
                labels.get('com.docker.compose.project')!=PROJECT):raise ValueError('Fixture ownership changed')
        command([docker,'rm','-f',container_id])
    leftovers=command([docker,'ps','-aq','--filter',f"label={LABEL}={metadata['run_id']}"]).strip()
    if leftovers:raise ValueError('Owned fixture containers remain; inspect exact IDs before cleanup')


def prepared_cli():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare',required=True,help='New absolute fixture directory; no containers started')
    args=parser.parse_args()
    print(json.dumps(prepare(args.prepare),indent=2))


def run_acceptance():
    import tempfile,time,sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from ingress_certificates import CertificateManager,run_bounded,CommandError
    source_root=Path(__file__).resolve().parents[1]
    source_names=('ingress_certificates.py','ingress_host.py','ingress_host_tls.py','ingress_apache_executor.py','tests/live_ingress_tls.py')
    source_hashes={name:hashlib.sha256((source_root/name).read_bytes()).hexdigest() for name in source_names}
    before=set(command(['docker','ps','-aq']).decode().split())
    report={};apache=None;metadata=None;upstream=None
    with tempfile.TemporaryDirectory(prefix='deploy-monitor-real-acme-') as tmp:
        root=Path(tmp)/'fixture';metadata=prepare(root)
        compose=['docker','compose','-p',PROJECT,'-f',str(root/'compose.json')]
        try:
            command(compose+['create'])
            ids=command(compose+['ps','-aq']).decode().split()
            metadata['container_ids']=[json.loads(command(['docker','inspect',i]))[0]['Id'] for i in ids]
            write(root/'fixture.json',json.dumps(metadata,indent=2))
            command(compose+['start'])
            deadline=time.monotonic()+20
            while True:
                try:issuing=read_issuing_root(metadata);break
                except Exception:
                    if time.monotonic()>deadline:raise
                    time.sleep(.2)
            write(root/'trust.pem',(root/'transport-ca.pem').read_text()+Path(issuing['path']).read_text())
            port=metadata['ports']['http']
            conf=root/'apache.conf'
            from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
            import threading
            from ingress_host import HostAdapter
            from ingress_host_tls import HostTLSAdapter
            from ingress_apache_executor import ApacheExecutor
            class Application(BaseHTTPRequestHandler):
                def do_GET(self):
                    body=b'development-real-tls-fixture'
                    self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                def log_message(self,*args):pass
            upstream=ThreadingHTTPServer(('127.0.0.1',0),Application)
            threading.Thread(target=upstream.serve_forever,daemon=True).start()
            for folder in ('managed','host-state'):(root/folder).mkdir(mode=0o700)
            route,operation=uuid.uuid4().hex,uuid.uuid4().hex
            policy={'managed_dir':str(root/'managed'),'state_dir':str(root/'host-state'),'listener':{'address':'127.0.0.1','port':port},'routes':{route:{'hostname':'development.fixture.test','zone':'fixture.test','port':upstream.server_port,'revision':1}}}
            write(root/'host-policy.json',json.dumps(policy))
            modules=''.join(f'LoadModule {m}_module /usr/lib/apache2/modules/mod_{m}.so\n' for m in ('mpm_event','authz_core','authz_host','alias','mime','headers','proxy','proxy_http','ssl','socache_shmcb'))
            write(conf,modules+f'ServerRoot {root}\nServerName localhost\nListen 127.0.0.1:{port}\nListen 127.0.0.1:{metadata["ports"]["https"]}\nPidFile {root}/apache.pid\nErrorLog {root}/apache-error.log\nTypesConfig /etc/mime.types\nIncludeOptional {root}/managed/*.conf\n')
            apache_executor=ApacheExecutor(executable='/usr/sbin/apache2',config=str(conf),pid_file=str(root/'apache.pid'),listener=('127.0.0.1',port),expected_argv=('/usr/sbin/apache2','-f',str(conf),'-DFOREGROUND'),owner_uid=os.getuid(),tls_listener=('127.0.0.1',metadata['ports']['https']),tls_ca_file=str(root/'trust.pem'))
            http=HostAdapter(root/'host-policy.json',apache_executor,owner_uid=os.getuid(),challenge_root=root/'challenge',authorize_route=lambda route,grant:None)
            apache=subprocess.Popen(['/usr/sbin/apache2','-f',str(conf),'-DFOREGROUND'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            for _ in range(50):
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.1):break
                except OSError:time.sleep(.1)
            else:raise RuntimeError('Fixture Apache failed to bind')
            http_intent={'route_id':route,'operation_id':operation,'expected_digest':None}
            staged_http=http.stage_http(http_intent)
            http.activate_http(dict(http_intent,digest=staged_http['digest']))
            counter={'calls':0,'certonly':0}
            def executor(argv,*,timeout,env):
                counter['calls']+=1
                if 'certonly' in argv:counter['certonly']+=1
                name='deploy-monitor-tls-certbot-'+uuid.uuid4().hex
                docker=['docker','create','--entrypoint','/usr/local/bin/certbot','--name',name,'--network','host','--user',f'{os.getuid()}:{os.getgid()}',
                    '--label',f'{LABEL}={metadata["run_id"]}','--label',f'com.docker.compose.project={PROJECT}',
                    '--cap-drop','ALL','--security-opt','no-new-privileges','--memory','256m','--cpus','1',
                    '--mount',f'type=bind,src={root},dst={root}']
                for key,value in env.items():docker+=['--env',key+'='+value]
                cid=command(docker+[IMAGES['certbot']]+argv[1:]).decode().strip()
                metadata['container_ids'].append(cid);write(root/'fixture.json',json.dumps(metadata,indent=2))
                try:
                    try:result=run_bounded(['docker','start','-a',cid],timeout=timeout,env={'PATH':os.environ['PATH']})
                    except Exception:
                        print(command(['docker','logs',cid]).decode()[-2000:],file=sys.stderr)
                        raise
                    code=json.loads(command(['docker','inspect',cid]))[0]['State']['ExitCode']
                    if code:raise CommandError()
                    return result
                finally:
                    command(['docker','rm','-f',cid]);metadata['container_ids'].remove(cid)
            env={'REQUESTS_CA_BUNDLE':str(root/'trust.pem'),'HOME':str(root/'certbot-work')}
            args=['certbot','register','--non-interactive','--agree-tos','--email','fixture@example.test','--server',metadata['directory_url']]
            for flag,key in (('--config-dir','certbot-config'),('--work-dir','certbot-work'),('--logs-dir','certbot-logs')):args += [flag,str(root/key)]
            executor(args,timeout=60,env=env)
            accounts=list((root/'certbot-config'/'accounts').rglob('regr.json'))
            assert len(accounts)==1
            for name in ('certificate-state','generations'):(root/name).mkdir(mode=0o700)
            executable=root/'certbot-executable';write(executable,'fixture injected executor identity\n')
            config=dict(executable=str(executable),expected_version='5.8.0',server=metadata['directory_url'],account=accounts[0].parent.name,email='fixture@example.test',agree_tos=True,
                config_dir=str(root/'certbot-config'),work_dir=str(root/'certbot-work'),logs_dir=str(root/'certbot-logs'),challenge_root=str(root/'challenge'),
                state_dir=str(root/'certificate-state'),generations_dir=str(root/'generations'),ca_bundle=str(root/'trust.pem'),timeout=90)
            manager=CertificateManager(config,owner_uid=os.getuid(),executor=executor)
            tls=HostTLSAdapter(http,manager,listener={'address':'127.0.0.1','port':metadata['ports']['https']},authorize_certificate=lambda route,grant:None)
            identity={'route_id':route,'operation_id':operation}
            result=tls.request_certificate(identity)
            if result['status']!='verified':raise AssertionError('CertificateManager did not verify real issuance: '+json.dumps(result))
            count=dict(counter)
            replay=manager.request(route,operation,'development.fixture.test',authorize=lambda:None)
            assert replay['status']=='verified' and replay['generation']==result['generation'] and counter==count
            inspected=manager.inspect(route,operation,'development.fixture.test',authorize=lambda:None)
            assert inspected['status']=='verified' and inspected['generation']==result['generation']
            staged_tls=tls.stage_tls(dict(identity,expected_digest=None,generation=result['generation'],fingerprint=result['fingerprint']))
            original_reload=apache_executor.reload;reloads=[]
            def lost_reload_reply():
                original_reload();reloads.append(True);raise TimeoutError('Synthetic reply loss after actual signal')
            apache_executor.reload=lost_reload_reply
            try:tls.activate_tls(dict(identity,digest=staged_tls['digest']))
            except TimeoutError:pass
            else:raise AssertionError('Lost reply was not exercised')
            deadline=time.monotonic()+5
            while True:
                try:recovered=tls.recover_tls(identity);break
                except ValueError:
                    if time.monotonic()>deadline:raise
                    time.sleep(.05)
            assert len(reloads)==1 and recovered['status']=='active'
            assert tls.verify_tls(identity)['fingerprint']==result['fingerprint']
            context=ssl.create_default_context(cafile=str(root/'trust.pem'))
            with socket.create_connection(('127.0.0.1',metadata['ports']['https']),timeout=5) as sock:
                with context.wrap_socket(sock,server_hostname='development.fixture.test') as connection:
                    connection.sendall(b'GET / HTTP/1.1\r\nHost: development.fixture.test\r\nConnection: close\r\n\r\n')
                    payload=b''
                    while len(payload)<65536:
                        chunk=connection.recv(4096)
                        if not chunk:break
                        payload+=chunk
                    assert b'development-real-tls-fixture' in payload
            renewal_files=list((root/'certbot-config'/'renewal').glob('*.conf'))
            assert len(renewal_files)==1
            renewal_metadata=[line.strip().replace(str(root/'challenge'),'<challenge_root>') for line in renewal_files[0].read_text().splitlines() if line.strip().startswith(('webroot_path','[[webroot_map]]','development.fixture.test'))]
            assert source_hashes=={name:hashlib.sha256((source_root/name).read_bytes()).hexdigest() for name in source_names},'Product source changed during acceptance'
            report={'source_sha256':source_hashes,'renewal_webroot_metadata':renewal_metadata,'issuance':result,'replay_without_command':True,'inspection':True,'images':IMAGES,'tls_activation':'verified actual SNI HTTPS application body','lost_reload_reply_recovered_without_repeat':True}
        finally:
            try:
                if apache is not None:
                    apache.terminate()
                    try:apache.wait(timeout=5)
                    except subprocess.TimeoutExpired:apache.kill();apache.wait(timeout=5)
                    apache.stderr.close()
            finally:
                if upstream is not None:upstream.shutdown();upstream.server_close()
                # Recover IDs from the unique fixture label even if compose reply was lost.
                discovered=command(['docker','ps','-aq','--no-trunc','--filter',f'label={LABEL}={metadata["run_id"]}']).decode().split()
                metadata['container_ids']=discovered
                cleanup_owned(metadata)
                after=set(command(['docker','ps','-aq']).decode().split())
                if before!=after:raise AssertionError('Peer container inventory changed')
                report['cleanup_verified']=True
    return report


if __name__=='__main__':
    if '--run' in __import__('sys').argv:
        print(json.dumps(run_acceptance(),indent=2))
    else:prepared_cli()
