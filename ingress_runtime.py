"""Owned, journaled HTTP gateway effects. Caller holds the worker control lock.

Nginx upstream resolve uses official OSS support since 1.27.3 and Docker's embedded
DNS. Images are administrator-selected immutable nginx-unprivileged references.
No application is started, replaced, or given a published port by this module.
"""
import copy
import hashlib
import json
from pathlib import Path
import os
import re
import select
import subprocess
import time
import uuid

import build_profiles
import ingress_bindings
import ingress_route_generations as generations
import ingress_ports
import managed_services
import registry
import service_provisioning

PREFIX='net.shiftcodes.ingress.'
HEALTHCHECK=['CMD','wget','-q','-O','/dev/null','http://127.0.0.1:8081/healthz']
IMAGE_PATTERN=r'nginxinc/nginx-unprivileged@sha256:[a-f0-9]{64}'


class GatewayError(ValueError): pass


def digest(value):return hashlib.sha256(registry.canonical(value).encode()).hexdigest()


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS ingress_runtime_intents(
        project TEXT NOT NULL,environment TEXT NOT NULL,operation_id TEXT NOT NULL UNIQUE,
        definition TEXT NOT NULL,digest TEXT NOT NULL,root TEXT NOT NULL,journal TEXT NOT NULL,
        phase TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(project,environment))''')


def _command(args,timeout=40,input=None):
    try:result=subprocess.run(args,capture_output=True,timeout=timeout,input=input)
    except (OSError,subprocess.TimeoutExpired):raise GatewayError('Gateway command outcome is unavailable; reconcile recorded identities before retrying.') from None
    if result.returncode or len(result.stdout)>2*1024*1024:raise GatewayError('Gateway operation could not be verified; recorded resources are retained.')
    return result.stdout.decode()


def _json(args,timeout=40):
    try:return json.loads(_command(args,timeout=timeout))
    except (ValueError,UnicodeError):raise GatewayError('Gateway runtime evidence is unavailable.') from None


def _names(d):return d['environment']+'-ingress',d['project']+'-'+d['environment']+'-ingress-transport'


def _validate(d):
    fields={'schema_version','project','environment','operation_id','image','image_id','port','resources','application_network_id','application_network_name','routes'}
    if not isinstance(d,dict) or set(d)!=fields or type(d['schema_version']) is not int or d['schema_version'] not in (1,2,3):raise GatewayError('Invalid gateway definition.')
    registry.identifier(d['project'])
    if d['environment'] not in registry.ENVIRONMENTS or not isinstance(d['operation_id'],str) or not re.fullmatch('[A-Za-z0-9-]{16,100}',d['operation_id']):raise GatewayError('Invalid gateway owner.')
    if not isinstance(d['image'],str) or not re.fullmatch(IMAGE_PATTERN,d['image']) or not re.fullmatch('sha256:[a-f0-9]{64}',str(d['image_id'])):raise GatewayError('A pinned nginx-unprivileged image is required.')
    if type(d['port']) is not int or not 1024<=d['port']<=65535:raise GatewayError('Invalid reserved gateway port.')
    r=d['resources']
    if not isinstance(r,dict) or set(r)!={'memory_mib','cpu'} or type(r['memory_mib']) is not int or not 16<=r['memory_mib']<=4096 or type(r['cpu']) not in (int,float) or not .05<=r['cpu']<=8:raise GatewayError('Invalid gateway resource limits.')
    if not re.fullmatch('[a-f0-9]{64}',str(d['application_network_id'])) or d['application_network_name']!=d['project']+'-'+d['environment']+'-managed':raise GatewayError('Invalid application network identity.')
    if not isinstance(d['routes'],list) or not 1<=len(d['routes'])<=100:raise GatewayError('Gateway requires bounded hostname routes.')
    seen=set()
    for route in d['routes']:
        if not isinstance(route,dict) or set(route)!={'hostname','service_id','definition_id','definition_digest','container_id','alias','http_endpoint'}:raise GatewayError('Invalid gateway route.')
        host=route['hostname']
        if not isinstance(host,str) or len(host)>253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+',host) or host in seen:raise GatewayError('Choose distinct normalized hostnames.')
        seen.add(host)
        if not isinstance(route['alias'],str) or not re.fullmatch(re.escape(d['environment'])+r'-[a-z][a-z0-9-]{0,47}',route['alias']):raise GatewayError('Invalid owned application DNS alias.')
        for key in ('container_id','definition_digest'):
            if not re.fullmatch('[a-f0-9]{64}',str(route[key])):raise GatewayError('Invalid pinned backend identity.')
        for key in ('service_id','definition_id'):
            if not isinstance(route[key],str) or not 1<=len(route[key])<=100:raise GatewayError('Invalid service identity.')
        build_profiles.validate_http_endpoint(route['http_endpoint'])
    return d


ROUTE_PROOF_PATH='/.well-known/deploy-monitor/route-proof'


def route_proof_token(project, environment, route):
    try: return generations.route_proof_token(project, environment, route)
    except ValueError as error: raise GatewayError(str(error)) from None


def expected_route_proof(definition, hostname):
    """Expected proof for exact supplied immutable pins, never a health verdict.

    Callers verifying current readiness must supply current_routes(db, definition)
    and independently verify runtime/backend health. Old config fails after drift.
    """
    d=_validate(definition)
    if d['schema_version']<3:raise GatewayError('Gateway proof requires an explicit owned configuration upgrade.')
    route=next((route for route in d['routes'] if route['hostname']==hostname),None)
    if route is None:raise GatewayError('Exact configured proof hostname is required.')
    return route_proof_token(d['project'],d['environment'],route)


def render_nginx(definition, *, generation_token=None):
    d=_validate(definition)
    lines=['worker_processes 1;','pid /tmp/nginx.pid;','error_log stderr warn;','events { worker_connections 128; }',
        'http {','access_log off;','client_body_temp_path /tmp/client;','proxy_temp_path /tmp/proxy;','fastcgi_temp_path /tmp/fastcgi;','uwsgi_temp_path /tmp/uwsgi;','scgi_temp_path /tmp/scgi;',
        'resolver 127.0.0.11 valid=1s ipv6=off;','resolver_timeout 2s;',
        'map $http_x_forwarded_proto $trusted_proto { default http; https https; }',
        'server { listen 127.0.0.1:8081; server_name localhost; location = /healthz { return 200; } }',
        'server { listen 8080 default_server; server_name _; return 421; }']
    for index,route in enumerate(d['routes']):
        lines.extend([f'upstream backend_{index} {{ zone backend_{index} 32k; server {route["alias"]}:{route["http_endpoint"]["port"]} resolve; }}',
            f'server {{ listen 8080; server_name {route["hostname"]};',
            *([f'location = {ROUTE_PROOF_PATH} {{', 'default_type text/plain;', 'add_header Cache-Control "no-store" always;', f'return 200 "{expected_route_proof(d,route["hostname"])}";', '}'] if d['schema_version']>=3 else []),
            'location / {',
            *(['proxy_hide_header '+name+';' for name in ('X-Deploy-Monitor-Route','X-Deploy-Monitor-Route-Proof','X-Deploy-Monitor-Generation','X-Deploy-Monitor-Backend')] if d['schema_version']>=3 else []),
            f'proxy_set_header Host {route["hostname"]};','proxy_set_header X-Real-IP $remote_addr;',
            'proxy_set_header X-Forwarded-For $remote_addr;','proxy_set_header X-Forwarded-Proto $trusted_proto;',
            'proxy_set_header Forwarded "";','proxy_connect_timeout 3s;','proxy_read_timeout 15s;',
            f'proxy_pass http://backend_{index};','}','}'])
    if d['schema_version']>=2:
        generation_token = generation_token or generations.token(d, {'generation':1,'routes':d['routes']})
        if not re.fullmatch('[a-f0-9]{64}',generation_token):raise GatewayError('Invalid configuration generation token.')
        lines[lines.index('server { listen 127.0.0.1:8081; server_name localhost; location = /healthz { return 200; } }')] = 'server { listen 127.0.0.1:8081; server_name localhost; location = /healthz { return 200; } location = /generation { return 200 "'+generation_token+'"; } }'
    return '\n'.join(lines+['}'])+'\n'


def _labels(d,role):return {PREFIX+'project':d['project'],PREFIX+'environment':d['environment'],PREFIX+'operation':d['operation_id'],PREFIX+'definition':digest(d),PREFIX+'role':role}


def gateway_manifest(definition,config_path,transport_id):
    d=_validate(definition);service,transport=_names(d)
    return {'services':{service:{'image':d['image'],'user':'101:101','read_only':True,'cap_drop':['ALL'],
        'security_opt':['no-new-privileges:true'],'pids_limit':128,'mem_limit':str(d['resources']['memory_mib'])+'m',
        'memswap_limit':str(d['resources']['memory_mib'])+'m','cpus':d['resources']['cpu'],
        'entrypoint':['nginx'],'command':['-c',('/etc/nginx/ingress/nginx.conf' if d['schema_version']>=2 else '/etc/nginx/ingress.conf'),'-g','daemon off;'],
        'labels':_labels(d,'gateway'),'healthcheck':{'test':HEALTHCHECK,'interval':'2s','timeout':'2s','retries':3,'start_period':'1s'},'tmpfs':['/tmp:rw,nosuid,noexec,size=16777216,mode=1777'],
        'volumes':[{'type':'bind','source':str(Path(config_path).parent if d['schema_version']>=2 else config_path),'target':('/etc/nginx/ingress' if d['schema_version']>=2 else '/etc/nginx/ingress.conf'),'read_only':True}],
        'ports':[{'target':8080,'published':str(d['port']),'host_ip':'127.0.0.1','protocol':'tcp'}],
        'networks':{'application':{},'transport':{}},'restart':'no'}},
        'networks':{'application':{'external':True,'name':d['application_network_name']},'transport':{'external':True,'name':transport}}}


def _capacity(db,d,container_id=None):
    try:
        capacity=db.execute('SELECT * FROM ingress_capacity WHERE project=? AND environment=?',(d['project'],d['environment'])).fetchone()
        reservation=db.execute('SELECT * FROM ingress_ports WHERE project=? AND environment=?',(d['project'],d['environment'])).fetchone()
    except Exception:raise GatewayError('Gateway reservations are unavailable.') from None
    if not capacity or capacity['operation_id']!=d['operation_id'] or any(capacity[k]!=d['resources'][k] for k in ('memory_mib','cpu')):raise GatewayError('Matching gateway capacity reservation is required.')
    if not reservation or reservation['operation_id']!=d['operation_id'] or reservation['port']!=d['port']:raise GatewayError('Gateway port reservation changed.')
    if container_id is not None and any(row['container_id'] not in (None,container_id) for row in (capacity,reservation)):
        raise GatewayError('Gateway reservation binds a different container identity.')


def _backend(db,project,environment,service_id,docker,*,running=True):
    service=managed_services.get(db,service_id);definition=service['definition'];binding=service.get('binding')
    if service['project']!=project or service['environment']!=environment or not binding:raise GatewayError('A bound application from this environment is required.')
    artifact=managed_services.artifact_for_service(db,project,definition['artifact_id'])
    if artifact!=definition['artifact'] or 'http_endpoint' not in artifact:raise GatewayError('A trusted HTTP endpoint is required.')
    build_profiles.validate_http_endpoint(artifact['http_endpoint'])
    actual=_json([docker,'inspect',binding['container_id']])[0]
    path=Path(binding['compose_files'][0]);network_id=binding['network_ids'][0]
    effective=next((item.get('effective_resources') for item in managed_services.environment_definitions(db,project,environment) if item['id']==service_id),None)
    service_provisioning._verify(actual,definition,service['operation_id'],binding['image_id'],network_id,binding['container_name'],path,effective)
    if actual['Id']!=binding['container_id']:raise GatewayError('Backend container identity changed.')
    if running and (not actual['State']['Running'] or actual['State'].get('Health',{}).get('Status')!='healthy'):raise GatewayError('Application must already be running and healthy; ingress never starts it.')
    network=_json([docker,'network','inspect',network_id])[0]
    service_provisioning._network(network,definition,network_id)
    alias=binding['compose_service'];owners=[]
    for identifier in network.get('Containers',{}):
        peer=_json([docker,'inspect',identifier])[0]
        addresses=peer.get('NetworkSettings',{}).get('Networks',{}).get(network['Name'],{})
        if alias in (addresses.get('Aliases') or []) or peer.get('Name','').lstrip('/')==alias:owners.append(peer['Id'])
    if owners!=[actual['Id']]:raise GatewayError('Application DNS alias is missing or has multiple owners.')
    return {'service_id':service_id,'definition_id':service['definition_id'],'definition_digest':digest(definition),
        'container_id':actual['Id'],'alias':alias,'http_endpoint':copy.deepcopy(artifact['http_endpoint'])},network


def prepare_gateway(db,project,environment,operation_id,routes,*,image,resources,docker):
    if not isinstance(routes,list) or not 1<=len(routes)<=100 or any(not isinstance(r,dict) or set(r)!={'hostname','service_id'} for r in routes):raise GatewayError('Only hostname and managed service identity may be supplied.')
    reservation=ingress_ports.get(db,project,environment)
    if not reservation or reservation['operation_id']!=operation_id:raise GatewayError('Reserve the environment gateway port first.')
    if not isinstance(image,str) or not re.fullmatch(IMAGE_PATTERN,image):raise GatewayError('Choose a pinned nginx-unprivileged image.')
    inspected=_json([docker,'image','inspect',image])[0]
    backend_routes=[];network=None
    for route in routes:
        backend,current=_backend(db,project,environment,route['service_id'],docker)
        if network and network['Id']!=current['Id']:raise GatewayError('Route applications must share this exact private environment network.')
        network=current;backend_routes.append(dict(backend,hostname=route['hostname']))
    definition={'schema_version':3,'project':project,'environment':environment,'operation_id':operation_id,
        'image':image,'image_id':inspected['Id'],'port':reservation['port'],'resources':copy.deepcopy(resources),
        'application_network_id':network['Id'],'application_network_name':network['Name'],
        'routes':sorted(backend_routes,key=lambda row:row['hostname'])}
    _validate(definition);_capacity(db,definition)
    return definition


def _recheck_backends(db,d,docker):
    for route in ingress_bindings.current_routes(db,d):
        current,network=_backend(db,d['project'],d['environment'],route['service_id'],docker)
        if current!={k:v for k,v in route.items() if k!='hostname'} or network['Id']!=d['application_network_id']:raise GatewayError('Pinned route backend changed; explicitly reconcile its route definition.')


def _save(db,d,root,journal,phase):
    initialize(db)
    ingress_bindings.initialize(db)
    generations.initialize(db)
    with registry.transaction(db):
        old=db.execute('SELECT * FROM ingress_runtime_intents WHERE project=? AND environment=?',(d['project'],d['environment'])).fetchone()
        if old and (old['digest']!=digest(d) or old['definition']!=registry.canonical(d) or old['root']!=str(root)):raise GatewayError('Gateway intent changed; immutable operation cannot be overwritten.')
        generations.seed(db,d)
        ingress_bindings.seed(db,d)
        db.execute('INSERT OR REPLACE INTO ingress_runtime_intents VALUES(?,?,?,?,?,?,?,?,?)',
            (d['project'],d['environment'],d['operation_id'],registry.canonical(d),digest(d),str(root),registry.canonical(journal),phase,time.time()))


def _load(db,d,root):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingress_runtime_intents'").fetchone():return {}
    row=db.execute('SELECT * FROM ingress_runtime_intents WHERE project=? AND environment=?',(d['project'],d['environment'])).fetchone()
    if not row:return {}
    if row['digest']!=digest(d) or row['definition']!=registry.canonical(d) or row['root']!=str(root):raise GatewayError('Gateway intent differs from the persisted operation.')
    return json.loads(row['journal'])


def _find(docker,kind,name):
    args=[docker,'network','ls','-q','--no-trunc','--filter','name=^'+name+'$'] if kind=='network' else [docker,'ps','-aq','--no-trunc','--filter','name=^/'+name+'$']
    identifiers=_command(args).split()
    if len(identifiers)>1:raise GatewayError('Runtime name is ambiguous.')
    return _json([docker,*(['network'] if kind=='network' else []),'inspect',identifiers[0]])[0] if identifiers else None


def _transport(row,d,expected=None,gateway_id=None):
    if (row['Name']!=_names(d)[1] or expected and row['Id']!=expected or row.get('Driver')!='bridge'
        or row.get('Internal') or row.get('EnableIPv6') or row.get('Options')
        or any((row.get('Labels') or {}).get(k)!=v for k,v in _labels(d,'transport').items())
        or set(row.get('Containers',{})) - ({gateway_id} if gateway_id else set())):raise GatewayError('Gateway transport identity or exclusive membership differs.')


def _paths(root,d,create=False):
    root=Path(root)
    if root.is_symlink() or not root.is_absolute():raise GatewayError('Trusted absolute gateway root is required.')
    root=root.resolve(strict=True);directory=root
    for name in (d['project'],d['environment'],'ingress'):
        directory=directory/name
        if directory.is_symlink():raise GatewayError('Gateway directory cannot be a symlink.')
        if create:directory.mkdir(mode=0o755,exist_ok=True)
        elif not directory.is_dir():raise GatewayError('Gateway configuration directory is missing.')
    path=directory/'compose.json'
    if d['schema_version']>=2:
        directory=directory/'config'
        if directory.is_symlink():raise GatewayError('Gateway config directory cannot be a symlink.')
        if create:directory.mkdir(mode=0o755,exist_ok=True)
        elif not directory.is_dir():raise GatewayError('Gateway configuration directory is missing.')
    return root,directory/'nginx.conf',path


def _write_once(path,text):
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_text()!=text:raise GatewayError('Gateway configuration differs from the accepted immutable manifest.')
        return
    temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
    with os.fdopen(fd,'w') as stream:stream.write(text);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def _gateway(row,d,config,path,network,*,expected=None,running=True):
    service,_=_names(d);cfg=row.get('Config',{});host=row.get('HostConfig',{});networks=row.get('NetworkSettings',{}).get('Networks',{})
    labels={**_labels(d,'gateway'),'com.docker.compose.project':d['project'],'com.docker.compose.service':service,'com.docker.compose.project.config_files':str(path)}
    ports={'8080/tcp':[{'HostIp':'127.0.0.1','HostPort':str(d['port'])}]}
    mounts=[m for m in row.get('Mounts',[]) if m.get('Type')!='tmpfs']
    if (expected and row['Id']!=expected or row['Name'].lstrip('/')!=d['project']+'-'+service+'-1'
        or row['Image']!=d['image_id'] or any((cfg.get('Labels') or {}).get(k)!=v for k,v in labels.items())
        or cfg.get('Healthcheck',{}).get('Test')!=HEALTHCHECK
        or cfg.get('User')!='101:101' or cfg.get('Entrypoint')!=['nginx'] or cfg.get('Cmd')!=['-c',('/etc/nginx/ingress/nginx.conf' if d['schema_version']>=2 else '/etc/nginx/ingress.conf'),'-g','daemon off;']
        or host.get('Privileged') or not host.get('ReadonlyRootfs') or host.get('CapAdd') or host.get('CapDrop')!=['ALL']
        or host.get('SecurityOpt')!=['no-new-privileges:true'] or host.get('PidsLimit')!=128
        or host.get('Memory')!=d['resources']['memory_mib']*1048576 or host.get('MemorySwap')!=host.get('Memory')
        or host.get('NanoCpus')!=round(d['resources']['cpu']*1e9) or host.get('PortBindings')!=ports
        or host.get('Devices') or host.get('DeviceRequests') or host.get('PidMode') or host.get('IpcMode') not in ('private','')
        or len(mounts)!=1 or mounts[0].get('Type')!='bind' or mounts[0].get('Source')!=str(config.parent if d['schema_version']>=2 else config) or mounts[0].get('Destination')!=('/etc/nginx/ingress' if d['schema_version']>=2 else '/etc/nginx/ingress.conf') or mounts[0].get('RW')
        or set(host.get('Tmpfs') or {})!={'/tmp'} or set((host.get('Tmpfs') or {}).get('/tmp','').split(','))!={'rw','nosuid','noexec','size=16777216','mode=1777'} or set(networks)!={d['application_network_name'],_names(d)[1]}
        or networks.get(d['application_network_name'],{}).get('NetworkID') not in ({d['application_network_id']} if running else {d['application_network_id'],''})
        or networks.get(_names(d)[1],{}).get('NetworkID') not in ({network['Id']} if running else {network['Id'],''})
        or running and (not row.get('State',{}).get('Running') or row.get('State',{}).get('Health',{}).get('Status')!='healthy')):raise GatewayError('Gateway container ownership, isolation, resources or immutable configuration differs.')


def _http_status(docker,container_id,hostname,path,timeout=6):
    request=('GET '+path+' HTTP/1.0\r\nHost: '+hostname+'\r\nConnection: close\r\n\r\n').encode('ascii')
    process=None;deadline=time.monotonic()+timeout
    try:
        process=subprocess.Popen([docker,'exec','-i',container_id,'busybox','nc','-w','3','127.0.0.1','8080'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        # Keep stdin open until a response arrives. BusyBox nc can exit early at
        # stdin EOF, before a proxied backend has returned its first response byte.
        process.stdin.write(request);process.stdin.flush()
        response=b''
        while b'\n' not in response and len(response)<4096:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not select.select([process.stdout],[],[],remaining)[0]:break
            chunk=os.read(process.stdout.fileno(),4096-len(response))
            if not chunk:break
            response+=chunk
        match=re.match(rb'HTTP/1\.[01] ([0-9]{3}) ',response)
        if not match:raise GatewayError('Gateway HTTP readiness was not observed.')
        return int(match.group(1))
    except (OSError,subprocess.SubprocessError):raise GatewayError('Gateway HTTP readiness was not observed.') from None
    finally:
        if process is not None:
            for stream in (process.stdin,process.stdout):
                try:stream.close()
                except OSError:pass
            if process.poll() is None:process.kill()
            process.wait()


def _wait_http(d,docker,container_id,authorize):
    deadline=time.monotonic()+20
    def remaining():
        budget=deadline-time.monotonic()
        if budget<=0:raise GatewayError('Gateway HTTP readiness deadline expired.')
        return min(6,budget)
    while True:
        authorize()
        try:
            actual=_json([docker,'inspect',container_id],timeout=remaining())[0]
            ready=actual.get('State',{}).get('Health',{}).get('Status')=='healthy'
            if ready:
                for route in d['routes']:
                    if not 200<=_http_status(docker,container_id,route['hostname'],route['http_endpoint']['health_path'],timeout=remaining())<300:
                        ready=False;break
            if ready and _http_status(docker,container_id,'unconfigured.invalid','/',timeout=remaining())==421:return
        except GatewayError:pass
        if time.monotonic()>=deadline:raise GatewayError('Gateway HTTP or backend health verification failed; resources remain recorded for recovery.')
        time.sleep(min(.3,max(0,deadline-time.monotonic())))


def _application_membership(db,d,docker,gateway_id=None):
    network=_json([docker,'network','inspect',d['application_network_id']])[0]
    labels={service_provisioning.PREFIX+'project':d['project'],service_provisioning.PREFIX+'environment':d['environment']}
    if network.get('Id')!=d['application_network_id'] or network.get('Name')!=d['application_network_name'] or not network.get('Internal') or any((network.get('Labels') or {}).get(k)!=v for k,v in labels.items()):
        raise GatewayError('Application network identity changed.')
    allowed={row['binding']['container_id'] for row in managed_services.environment_definitions(db,d['project'],d['environment']) if row.get('binding')}
    if gateway_id is not None:allowed.add(gateway_id)
    if set(network.get('Containers',{}))-allowed:raise GatewayError('Application network contains an unowned peer.')


def apply_gateway(db,definition,*,root,docker,compose,authorize):
    d=_validate(definition);authorize();_capacity(db,d);_recheck_backends(db,d,docker)
    root,config,path=_paths(root,d,create=True);journal=_load(db,d,root)
    _application_membership(db,d,docker,journal.get('container_id'))
    service,network_name=_names(d);name=d['project']+'-'+service+'-1'
    try:
        _write_once(config,_effective_config(db,d));_save(db,d,root,journal,'prepared')
        network=_find(docker,'network',network_name)
        if network is not None and not (journal.get('network_started') or journal.get('transport_id')):
            raise GatewayError('Existing transport has no persisted creation intent; adoption is forbidden.')
        if network is None:
            if journal.get('network_started'):raise GatewayError('Transport creation outcome is unknown; automatic duplicate creation is blocked.')
            authorize();journal['network_started']=True;_save(db,d,root,journal,'transport-intent')
            args=[docker,'network','create','--driver','bridge']
            for key,value in _labels(d,'transport').items():args+=['--label',key+'='+value]
            _command(args+[network_name]);network=_find(docker,'network',network_name)
        if network is not None and not (journal.get('network_started') or journal.get('transport_id')):
            raise GatewayError('Existing transport has no persisted creation intent; adoption is forbidden.')
        if network is None:raise GatewayError('Transport creation was not observed.')
        _transport(network,d,journal.get('transport_id'),journal.get('container_id'))
        journal['transport_id']=network['Id'];_save(db,d,root,journal,'transport-verified')
        _write_once(path,registry.canonical(gateway_manifest(d,config,network['Id'])))
        args=[compose,'-p',d['project'],'-f',str(path)]
        actual=_find(docker,'container',name)
        if actual is not None and not (journal.get('create_started') or journal.get('container_id')):
            raise GatewayError('Existing gateway has no persisted creation intent; adoption is forbidden.')
        if actual is None:
            if journal.get('create_started'):raise GatewayError('Gateway creation outcome is unknown; automatic duplicate creation is blocked.')
            authorize();_capacity(db,d);_recheck_backends(db,d,docker)
            _application_membership(db,d,docker,journal.get('container_id'))
            import ingress_capacity
            ingress_ports.begin_creation(db,d['project'],d['environment'],d['operation_id'])
            ingress_capacity.begin_creation(db,d['project'],d['environment'],d['operation_id'])
            journal['create_started']=True;_save(db,d,root,journal,'container-intent')
            _command(args+['create','--no-build','--pull','never',service]);actual=_find(docker,'container',name)
        if actual is not None and not (journal.get('create_started') or journal.get('container_id')):
            raise GatewayError('Existing gateway has no persisted creation intent; adoption is forbidden.')
        if actual is None:raise GatewayError('Gateway container creation was not observed.')
        # Compose create may defer network attachment until start; pin the observed ID
        # before start, then verify the entire effective runtime immediately afterward.
        if any((actual.get('Config',{}).get('Labels') or {}).get(k)!=v for k,v in _labels(d,'gateway').items()):raise GatewayError('Existing gateway container belongs to another intent.')
        if journal.get('container_id') not in (None,actual['Id']):raise GatewayError('Gateway container was replaced.')
        _gateway(actual,d,config,path,network,expected=journal.get('container_id'),running=False)
        _capacity(db,d,actual['Id'])
        journal['container_id']=actual['Id'];_save(db,d,root,journal,'container-recorded')
        if not actual['State']['Running']:
            if journal.get('start_started'):raise GatewayError('Gateway is stopped after a start attempt; inspect failure before recovery.')
            authorize();_recheck_backends(db,d,docker);_application_membership(db,d,docker,actual['Id']);journal['start_started']=True;_save(db,d,root,journal,'start-intent')
            _command([docker,'start',actual['Id']])
        _wait_http(dict(d,routes=ingress_bindings.current_routes(db,d)),docker,actual['Id'],authorize)
        if d['schema_version']>=2:_verify_generation(docker,actual['Id'],generations.token(d,generations.head(db,d)))
        binding=verify_gateway(db,d,root=root,docker=docker,compose=compose,authorize=authorize)
        ingress_ports.bind(db,d['project'],d['environment'],d['operation_id'],binding['container_id'])
        import ingress_capacity
        ingress_capacity.mark_bound(db,d['project'],d['environment'],d['operation_id'],binding['container_id'])
        journal['binding']=binding;_save(db,d,root,journal,'verified');return binding
    except Exception:
        _save(db,d,root,journal,'recovery-required')
        raise


def _verify_gateway(db,definition,*,root,docker,compose=None,authorize=lambda:None,check_backends=True,expected_config=None):
    d=_validate(definition);authorize();_capacity(db,d)
    root,config,path=_paths(root,d);journal=_load(db,d,root)
    if not journal.get('container_id') or not journal.get('transport_id'):raise GatewayError('Gateway runtime has no verified identity journal.')
    if config.is_symlink() or config.read_text()!=(expected_config if expected_config is not None else _effective_config(db,d)) or path.is_symlink() or path.read_text()!=registry.canonical(gateway_manifest(d,config,journal['transport_id'])):raise GatewayError('Gateway configuration changed outside its accepted operation.')
    actual=_json([docker,'inspect',journal['container_id']])[0]
    network=_json([docker,'network','inspect',journal['transport_id']])[0]
    _capacity(db,d,actual['Id'])
    _transport(network,d,journal['transport_id'],actual['Id']);_gateway(actual,d,config,path,network,expected=journal['container_id'])
    appnet=_json([docker,'network','inspect',d['application_network_id']])[0]
    expected_labels={service_provisioning.PREFIX+'project':d['project'],service_provisioning.PREFIX+'environment':d['environment']}
    if appnet.get('Name')!=d['application_network_name'] or not appnet.get('Internal') or any((appnet.get('Labels') or {}).get(k)!=v for k,v in expected_labels.items()):raise GatewayError('Application network identity changed.')
    if actual['Id'] not in appnet.get('Containers',{}):raise GatewayError('Gateway is missing from its application network.')
    if check_backends:
        allowed={row['binding']['container_id'] for row in managed_services.environment_definitions(db,d['project'],d['environment']) if row.get('binding')}
        if set(appnet.get('Containers',{}))-allowed-{actual['Id']}:raise GatewayError('Application network contains an unowned peer.')
        _recheck_backends(db,d,docker)
    return {'container_id':actual['Id'],'container_name':actual['Name'].lstrip('/'),'compose_project':d['project'],
        'compose_service':_names(d)[0],'compose_files':[str(path)],'image_id':d['image_id'],
        'application_network_id':d['application_network_id'],'transport_network_id':network['Id'],
        'network_ids':[d['application_network_id'],network['Id']],'port':d['port'],'definition_digest':digest(d),'observed':time.time()}


def verified_network_members(db,project,environment,*,docker,application_network_id,authorize=lambda:None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingress_runtime_intents'").fetchone():return frozenset()
    row=db.execute('SELECT * FROM ingress_runtime_intents WHERE project=? AND environment=?',(project,environment)).fetchone()
    if row is None:return frozenset()
    definition=json.loads(row['definition'])
    if digest(definition)!=row['digest'] or definition['application_network_id']!=application_network_id:raise GatewayError('Gateway network intent changed.')
    binding=verify_gateway(db,definition,root=row['root'],docker=docker,authorize=authorize,check_backends=False)
    return frozenset({binding['container_id']})


def _effective_config(db,d):
    if d['schema_version']==1:return render_nginx(d)
    h=generations.head(db,d)
    return render_nginx(dict(d,routes=h['routes']),generation_token=generations.token(d,h))


def _verify_generation(docker,container_id,token):
    observed=_command([docker,'exec',container_id,'wget','-q','-O','-','http://127.0.0.1:8081/generation'],timeout=6)
    if observed!=token:raise GatewayError('Gateway running configuration generation is unverified.')


def verify_gateway(db,definition,*,root,docker,compose=None,authorize=lambda:None,check_backends=True):
    binding=_verify_gateway(db,definition,root=root,docker=docker,compose=compose,authorize=authorize,check_backends=check_backends)
    if definition['schema_version']>=2:
        _verify_generation(docker,binding['container_id'],generations.token(definition,generations.head(db,definition)))
    return binding


def prepare_route_addition(db,definition,operation_id,route,*,root,docker,authorize):
    """Caller holds the same worker control lock used by managed delivery."""
    d=_validate(definition);authorize()
    if d['schema_version']<2:raise GatewayError('Existing single-file gateway requires explicit verified migration before route addition.')
    if not isinstance(operation_id,str) or not re.fullmatch('[A-Za-z0-9-]{16,100}',operation_id):raise GatewayError('Invalid route operation identity.')
    if not isinstance(route,dict) or set(route)!={'hostname','service_id'}:raise GatewayError('Only hostname and managed service identity may be supplied.')
    generations.initialize(db)
    old=generations.operation(db,operation_id)
    if old:
        document=old['document']
        if document.get('kind','addition')!='addition' or document['gateway_digest']!=digest(d) or document['route']!=route or document['root']!=str(Path(root).resolve()):raise GatewayError('Route operation replay differs.')
        return old
    binding=verify_gateway(db,d,root=root,docker=docker,authorize=authorize)
    before=generations.head(db,d);current=ingress_bindings.current_routes(db,d)
    backend,network=_backend(db,d['project'],d['environment'],route['service_id'],docker)
    if network['Id']!=d['application_network_id']:raise GatewayError('New route belongs to another application network.')
    if any(r['hostname']==route['hostname'] for r in current):raise GatewayError('Hostname already exists in gateway configuration.')
    candidate=sorted([*current,dict(backend,hostname=route['hostname'])],key=lambda row:row['hostname'])
    _validate(dict(d,routes=candidate));after={'generation':before['generation']+1,'routes':candidate}
    document={'gateway_digest':digest(d),'route':copy.deepcopy(route),'root':str(Path(root).resolve()),'before':before,'after':after,
              'backends':current,'container_id':binding['container_id'],'transport_id':binding['transport_network_id'],
              'previous':_effective_config(db,d),'candidate':render_nginx(dict(d,routes=candidate),generation_token=generations.token(d,after))}
    with registry.transaction(db):
        authorize()
        if generations.head(db,d)!=before or ingress_bindings.current_routes(db,d)!=current:raise GatewayError('Gateway routes changed before preparation.')
        if generations.pending(db,d['project'],d['environment']):raise GatewayError('Another route addition requires recovery.')
        db.execute('INSERT INTO ingress_route_operations VALUES(?,?,?,?,?)',(operation_id,d['project'],d['environment'],registry.canonical(document),'prepared'))
    return generations.operation(db,operation_id)


def _replace_route_config(config,text):
    if config.is_symlink() or not config.is_file():raise GatewayError('Gateway config file is missing or linked.')
    temporary=config.with_name('.candidate-'+uuid.uuid4().hex)
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
    try:
        with os.fdopen(fd,'w') as stream:stream.write(text);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,config)
        fd=os.open(config.parent,os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if temporary.exists():temporary.unlink()


def _addition_evidence(db,d,document,*,root,docker,authorize):
    if generations.head(db,d)!=document['before'] or ingress_bindings.current_routes(db,d)!=document['backends']:
        raise GatewayError('Gateway route or backend generation changed.')
    binding=_verify_gateway(db,d,root=root,docker=docker,authorize=authorize,expected_config=document['candidate'])
    if binding['container_id']!=document['container_id'] or binding['transport_network_id']!=document['transport_id']:
        raise GatewayError('Gateway physical identity changed during route addition.')
    for route in document['after']['routes']:
        actual,network=_backend(db,d['project'],d['environment'],route['service_id'],docker)
        if actual!={k:v for k,v in route.items() if k!='hostname'} or network['Id']!=d['application_network_id']:
            raise GatewayError('Route addition backend evidence changed.')
    return binding


def _apply_route_transition(db,definition,operation_id,*,root,docker,authorize,kind):
    """Resume deterministic pre-signal effects; after signal intent only observe."""
    d=_validate(definition);authorize();operation=generations.operation(db,operation_id)
    if not operation or operation['document'].get('kind','addition')!=kind or operation['document']['gateway_digest']!=digest(d) or operation['document']['root']!=str(Path(root).resolve()):raise GatewayError('Route addition intent is unavailable or changed.')
    document=operation['document'];phase=operation['phase'];root,config,path=_paths(root,d)
    if phase=='committed':
        if kind=='proof-refresh' and ingress_bindings.current_routes(db,d)!=document['after']['routes']:raise GatewayError('Completed proof refresh backend pins have been superseded.')
        if generations.head(db,d)!=document['after']:raise GatewayError('Completed route addition has been superseded.')
        return verify_gateway(db,d,root=root,docker=docker,authorize=authorize)
    if phase=='aborted':raise GatewayError('Route addition aborted before reload; prepare a new operation.')
    if generations.head(db,d)!=document['before'] or ingress_bindings.current_routes(db,d)!=document['backends']:raise GatewayError('Gateway route or backend generation changed.')
    if config.is_symlink() or config.read_text() not in (document['previous'],document['candidate']):raise GatewayError('Gateway configuration changed outside its route journal.')
    if phase in ('prepared','installing','tested'):
        try:
            authorize()
            # Physical identity and all original backends must still match before disk effects.
            binding=_verify_gateway(db,d,root=root,docker=docker,authorize=authorize,expected_config=config.read_text())
            if binding['container_id']!=document['container_id'] or binding['transport_network_id']!=document['transport_id']:raise GatewayError('Gateway physical identity changed.')
            if phase=='prepared':
                generations.phase(db,operation_id,'prepared','installing');phase='installing'
            _replace_route_config(config,document['candidate'])
            _addition_evidence(db,d,document,root=root,docker=docker,authorize=authorize)
            _command([docker,'exec',document['container_id'],'nginx','-t','-c','/etc/nginx/ingress/nginx.conf'])
            if phase=='installing':generations.phase(db,operation_id,'installing','tested');phase='tested'
            authorize();_addition_evidence(db,d,document,root=root,docker=docker,authorize=authorize)
        except Exception:
            # No signal has been attempted; restore exactly our owned candidate.
            if not config.is_symlink() and config.read_text() in (document['previous'],document['candidate']):
                _replace_route_config(config,document['previous'])
                with registry.transaction(db):
                    document['abort_reason']='pre-signal-failure'
                    db.execute('UPDATE ingress_route_operations SET document=? WHERE operation_id=? AND phase=?',(registry.canonical(document),operation_id,phase))
                    generations.phase(db,operation_id,phase,'aborted')
            raise
        generations.phase(db,operation_id,'tested','reload-intent');phase='reload-intent'
        _command([docker,'exec',document['container_id'],'nginx','-s','reload','-c','/etc/nginx/ingress/nginx.conf'])
    if phase!='reload-intent':raise GatewayError('Unknown route addition recovery phase.')
    binding=_addition_evidence(db,d,document,root=root,docker=docker,authorize=authorize)
    _wait_http(dict(d,routes=document['after']['routes']),docker,document['container_id'],authorize)
    _verify_generation(docker,document['container_id'],generations.token(d,document['after']))
    _addition_evidence(db,d,document,root=root,docker=docker,authorize=authorize)
    with registry.transaction(db):
        authorize()
        if generations.head(db,d)!=document['before'] or ingress_bindings.current_routes(db,d)!=document['backends']:raise GatewayError('Gateway route evidence changed before commit.')
        if kind=='addition':
            sid=document['route']['service_id'];route=next(r for r in document['after']['routes'] if r['hostname']==document['route']['hostname'])
            backend={k:v for k,v in route.items() if k!='hostname'}
            row=db.execute('SELECT backend FROM ingress_backend_bindings WHERE project=? AND environment=? AND service_id=?',(d['project'],d['environment'],sid)).fetchone()
            if row:
                if json.loads(row['backend'])!=backend:raise GatewayError('Existing service route binding changed.')
                db.execute('UPDATE ingress_backend_bindings SET generation=generation+1 WHERE project=? AND environment=? AND service_id=?',(d['project'],d['environment'],sid))
            else:db.execute('INSERT INTO ingress_backend_bindings VALUES(?,?,?,?,?,?)',(d['project'],d['environment'],sid,digest(d),1,registry.canonical(backend)))
        result=db.execute('UPDATE ingress_route_heads SET generation=?,routes=? WHERE project=? AND environment=? AND gateway_digest=? AND generation=? AND routes=?',(document['after']['generation'],registry.canonical(document['after']['routes']),d['project'],d['environment'],digest(d),document['before']['generation'],registry.canonical(document['before']['routes'])))
        if result.rowcount!=1:raise GatewayError('Gateway generation CAS failed.')
        generations.phase(db,operation_id,'reload-intent','committed')
    return binding


def apply_route_addition(db,definition,operation_id,*,root,docker,authorize):
    return _apply_route_transition(db,definition,operation_id,root=root,docker=docker,authorize=authorize,kind='addition')


def prepare_gateway_proof_refresh(db,definition,operation_id,*,root,docker,authorize):
    """Capture committed binding changes; caller serializes delivery and routing."""
    d=_validate(definition);authorize()
    if d['schema_version']!=3:raise GatewayError('Proof refresh requires an explicitly provisioned schema-3 gateway.')
    if not isinstance(operation_id,str) or not re.fullmatch('[A-Za-z0-9-]{16,100}',operation_id):raise GatewayError('Invalid proof refresh operation identity.')
    generations.initialize(db)
    old=generations.operation(db,operation_id)
    if old:
        document=old['document']
        if document.get('kind')!='proof-refresh' or document['gateway_digest']!=digest(d) or document['root']!=str(Path(root).resolve()):raise GatewayError('Proof refresh replay differs.')
        return old
    binding=verify_gateway(db,d,root=root,docker=docker,authorize=authorize)
    before=generations.head(db,d);current=ingress_bindings.current_routes(db,d)
    # Only backend definition/container pins may advance. Route ownership is fixed.
    stable=lambda rows:sorted(({k:r[k] for k in ('hostname','service_id','alias','http_endpoint')} for r in rows),key=lambda r:r['hostname'])
    if stable(before['routes'])!=stable(current):raise GatewayError('Proof refresh cannot add or change hostname, service, alias or endpoint.')
    _validate(dict(d,routes=current))
    for route in current:
        actual,network=_backend(db,d['project'],d['environment'],route['service_id'],docker)
        if actual!={k:v for k,v in route.items() if k!='hostname'} or network['Id']!=d['application_network_id']:raise GatewayError('Proof refresh backend is not currently verified.')
    changed=before['routes']!=current
    after={'generation':before['generation']+int(changed),'routes':current}
    document={'kind':'proof-refresh','gateway_digest':digest(d),'root':str(Path(root).resolve()),'before':before,'after':after,
        'backends':current,'container_id':binding['container_id'],'transport_id':binding['transport_network_id'],
        'previous':_effective_config(db,d),'candidate':render_nginx(dict(d,routes=current),generation_token=generations.token(d,after))}
    with registry.transaction(db):
        authorize()
        if generations.head(db,d)!=before or ingress_bindings.current_routes(db,d)!=current:raise GatewayError('Proof evidence changed before preparation.')
        if generations.pending(db,d['project'],d['environment']):raise GatewayError('Another route operation requires recovery.')
        db.execute('INSERT INTO ingress_route_operations VALUES(?,?,?,?,?)',(operation_id,d['project'],d['environment'],registry.canonical(document),'prepared' if changed else 'committed'))
    return generations.operation(db,operation_id)


def refresh_gateway_proof(db,definition,operation_id,*,root,docker,authorize):
    prepare_gateway_proof_refresh(db,definition,operation_id,root=root,docker=docker,authorize=authorize)
    return _apply_route_transition(db,definition,operation_id,root=root,docker=docker,authorize=authorize,kind='proof-refresh')


def recover_gateway_proof(db,definition,operation_id,*,root,docker,authorize):
    return _apply_route_transition(db,definition,operation_id,root=root,docker=docker,authorize=authorize,kind='proof-refresh')


def recover_route_addition(db,definition,operation_id,*,root,docker,authorize):
    return apply_route_addition(db,definition,operation_id,root=root,docker=docker,authorize=authorize)


def retry_aborted_route_addition(db,definition,operation_id,*,root,docker,authorize):
    """Explicit domain Resume only: prove pre-signal compensation before retry.

    Caller holds the worker control lock. The accepted route/operation cannot
    change; prior attempts remain in the durable journal rather than overwritten.
    """
    d=_validate(definition);authorize();operation=generations.operation(db,operation_id)
    if not operation or operation['phase']!='aborted':raise GatewayError('Only an aborted pre-signal addition can be retried.')
    old=operation['document']
    if old.get('kind','addition')!='addition' or old.get('abort_reason')!='pre-signal-failure' or old['gateway_digest']!=digest(d) or old['root']!=str(Path(root).resolve()):
        raise GatewayError('Aborted route intent is not eligible for safe retry.')
    history=copy.deepcopy(old.get('attempt_history',[]))
    if len(history)>=16:raise GatewayError('Route retry history reached its bound; administrative reconciliation is required.')
    root,config,path=_paths(root,d)
    if config.is_symlink() or config.read_text()!=old['previous'] or generations.head(db,d)!=old['before']:
        raise GatewayError('Compensated configuration or generation changed before retry.')
    binding=verify_gateway(db,d,root=root,docker=docker,authorize=authorize)
    if binding['container_id']!=old['container_id'] or binding['transport_network_id']!=old['transport_id']:
        raise GatewayError('Gateway physical identity changed before retry.')
    current=ingress_bindings.current_routes(db,d)
    _wait_http(dict(d,routes=current),docker,binding['container_id'],authorize)
    _verify_generation(docker,binding['container_id'],generations.token(d,old['before']))
    route=old['route'];backend,network=_backend(db,d['project'],d['environment'],route['service_id'],docker)
    if network['Id']!=d['application_network_id'] or any(r['hostname']==route['hostname'] for r in current):raise GatewayError('Aborted route target changed.')
    candidate=sorted([*current,dict(backend,hostname=route['hostname'])],key=lambda row:row['hostname'])
    _validate(dict(d,routes=candidate))
    after={'generation':old['before']['generation']+1,'routes':candidate}
    prior=copy.deepcopy(old);prior.pop('attempt_history',None)
    history.append({'phase':'aborted','document':prior})
    document=dict(old,backends=current,after=after,candidate=render_nginx(dict(d,routes=candidate),generation_token=generations.token(d,after)),
                  attempt_history=history,attempt=len(history)+1)
    document.pop('abort_reason',None)
    with registry.transaction(db):
        authorize()
        if generations.head(db,d)!=old['before'] or ingress_bindings.current_routes(db,d)!=current:raise GatewayError('Route backend evidence changed before retry.')
        if config.is_symlink() or config.read_text()!=old['previous']:raise GatewayError('Compensated configuration changed before retry.')
        result=db.execute("UPDATE ingress_route_operations SET document=?,phase='prepared' WHERE operation_id=? AND phase='aborted' AND document=?",(registry.canonical(document),operation_id,registry.canonical(old)))
        if result.rowcount!=1:raise GatewayError('Aborted route attempt changed concurrently.')
    return generations.operation(db,operation_id)
