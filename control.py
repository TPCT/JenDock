"""Allowlisted container operations over a private Unix socket.

Environment values never enter SQLite, audit details, command lines or logs.
"""
import copy
import hashlib
import json
import os
import re
import socketserver
import subprocess
import threading
import time
from pathlib import Path

import yaml
from store import connect, projects, audit

DOCKER = '/usr/local/bin/docker'
COMPOSE = '/usr/local/bin/docker-compose'
ROOT = Path('/opt/docker-engine/projects')
SOCKET = '/state/control.sock'
DATABASE = os.environ.get('DATABASE', '/state/state.sqlite')
CONFIG = os.environ.get('PROJECTS_FILE', '/policy/projects.json')
LOCK = threading.Lock()
JOURNAL = Path('/worker-state/environment-transaction.json')

class ControlError(Exception):
    pass

def command(args, timeout=30):
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ControlError('Docker could not complete the operation. The previous configuration is retained or restored.')
    return result.stdout.decode()

def checked_path(value):
    p = Path(value).resolve(strict=True)
    if not p.is_relative_to(ROOT.resolve()) or not p.is_file():
        raise ControlError('Configuration path is outside the managed project directory.')
    return p

def inspect_target(project_name, container_name):
    policy = projects(CONFIG).get(project_name)
    if not policy or not isinstance(container_name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{1,127}',container_name):
        raise ControlError('Unknown project or container.')
    if not any(container_name.startswith(p) for p in policy.get('container_prefixes',[])):
        raise ControlError('This container does not belong to this project.')
    result = json.loads(command([DOCKER,'inspect',container_name]))[0]
    if result['Name'].lstrip('/') != container_name:
        raise ControlError('Container identity changed. Refresh and try again.')
    labels = result['Config'].get('Labels') or {}
    group,service = labels.get('com.docker.compose.project'),labels.get('com.docker.compose.service')
    if not group or not service or not re.fullmatch(r'[a-zA-Z0-9_.-]+',group+service):
        raise ControlError('Container has no valid Compose owner.')
    files = [checked_path(f) for f in labels.get('com.docker.compose.project.config_files','').split(',') if f]
    if not files:
        raise ControlError('Compose configuration was not found.')
    args = [COMPOSE,'--profile','*','-p',group]
    for f in files: args += ['-f',str(f)]
    # Resolve from the actual Compose project directory, including its .env file.
    directory = files[0].parent
    dotenv = directory/'.env'
    if dotenv.exists():args += ['--env-file',str(checked_path(dotenv))]
    return result,service,files,args

def effective_environment(container):
    return dict(item.split('=',1) for item in container['Config'].get('Env',[]) if '=' in item)

def configuration(args):
    return json.loads(command(args+['config','--format','json']))

def revision(files, container, config):
    digest=hashlib.sha256(container['Id'].encode())
    for f in files:digest.update(f.read_bytes())
    digest.update(json.dumps(config,sort_keys=True).encode())
    return digest.hexdigest()

def atomic_write(path, content, mode):
    temp=path.with_name(path.name+'.monitor-tmp')
    with temp.open('w') as stream:
        os.chmod(temp,mode);stream.write(content);stream.flush();os.fsync(stream.fileno())
    temp.replace(path)

def mutate_service(document,service,changes):
    result=copy.deepcopy(document)
    services=result.get('services',{})
    if service not in services:raise ControlError('Service is not defined in this Compose file.')
    # YAML anchors may share mapping objects: detach this service before editing.
    target=copy.deepcopy(services[service]);services[service]=target
    environment=target.get('environment') or {}
    if isinstance(environment,list):environment=dict(v.split('=',1) if '=' in v else (v,None) for v in environment)
    environment=dict(environment)
    for key,value in changes.items():environment[key]=value.replace('$','$$')
    target['environment']=environment
    return result

def validate_changes(changes):
    if not isinstance(changes,dict) or not changes or len(changes)>200:raise ControlError('Provide between 1 and 200 environment changes.')
    for key,value in changes.items():
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}',key) or not isinstance(value,str) or '\x00' in value or len(value)>32768:
            raise ControlError('An environment key or value is invalid.')

def wait_runtime(name, expected_running, timeout=60):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        c=json.loads(command([DOCKER,'inspect',name]))[0];state=c['State']
        if not expected_running:
            if not state['Running']:return c
        elif state['Running'] and state.get('Health',{}).get('Status') not in ('starting','unhealthy'):
            return c
        time.sleep(2)
    raise ControlError('The container did not reach the expected runtime state.')

def recreate(args, service, running):
    options=['up','-d'] if running else ['create']
    command(args+options+['--no-deps','--no-build','--pull','never','--force-recreate',service],timeout=180)

def assert_image(config,service,image_id):
    image=config['services'][service].get('image')
    if not image or json.loads(command([DOCKER,'image','inspect',image]))[0]['Id']!=image_id:
        raise ControlError('The configured image differs from the running version. Reconcile the deployment before editing environment values.')
    if config['services'][service].get('scale',1)!=1 or config['services'][service].get('deploy',{}).get('replicas',1)!=1:
        raise ControlError('Environment editing requires a single-instance Compose service.')

def restore_environment():
    if not JOURNAL.exists():return
    record=json.loads(JOURNAL.read_text())
    path=checked_path(record['path'])
    atomic_write(path,record['before'],record['mode'])
    if not record.get('activated'):
        JOURNAL.unlink();return
    assert_image(configuration(record['compose']),record['service'],record['image_id'])
    recreate(record['compose'],record['service'],record['was_running'])
    restored=wait_runtime(record['container'],record['was_running'])
    if restored['Image']!=record['image_id'] or effective_environment(restored)!=record['environment']:
        raise ControlError('Restored runtime differs from the previous container.')
    JOURNAL.unlink()

def details(project, name):
    c,service,files,args=inspect_target(project,name)
    image=json.loads(command([DOCKER,'image','inspect',c['Image']]))[0]
    config=configuration(args)
    owner=next((f for f in reversed(files) if service in (yaml.safe_load(f.read_text()) or {}).get('services',{})),None)
    if service not in config['services'] or owner is None:raise ControlError('Service configuration is incomplete.')
    env=effective_environment(c)
    return {'container':name,'container_id':c['Id'],'service':service,'image':c['Config']['Image'],
            'image_id':c['Image'],'image_created':image.get('Created'),'image_digests':image.get('RepoDigests',[]),
            'commit':(image.get('Config',{}).get('Labels') or {}).get('org.opencontainers.image.revision'),
            'state':c['State']['Status'],'health':c['State'].get('Health',{}).get('Status','not configured'),
            'started_at':c['State'].get('StartedAt'),'restart_count':c.get('RestartCount',0),
            'environment':env,'revision':revision(files,c,config),'configuration_file':str(owner),
            'environment_scope':'Overrides apply to this Compose service only. Other services sharing an env file are unchanged.',
            'ports':c.get('NetworkSettings',{}).get('Ports',{}),
            'mounts':[{'type':m['Type'],'destination':m['Destination'],'read_only':not m['RW']} for m in c.get('Mounts',[])]}

def execute(body):
    if not isinstance(body,dict):raise ControlError('Invalid operation request.')
    action=body.get('action');project=body.get('project');name=body.get('container');actor=body.get('actor','admin')
    if action=='container-details':return details(project,name)
    if action not in ('container-start','container-stop','container-restart','environment-save'):
        raise ControlError('Unsupported container operation.')
    key=body.get('request_id','')
    if not re.fullmatch(r'[A-Za-z0-9-]{16,80}',key):raise ControlError('Invalid operation ID.')
    if not LOCK.acquire(blocking=False):raise ControlError('Another deployment or container operation is running. Try again after it completes.')
    db=None
    try:
        db=connect(DATABASE)
        previous=db.execute('SELECT status,detail,project,container,operation FROM control_runs WHERE request_id=?',(key,)).fetchone()
        if previous:
            if (previous['project'],previous['container'],previous['operation'])!=(project,name,action):raise ControlError('Operation ID belongs to a different request.')
            return {'status':previous['status'],'message':previous['detail']}
        if JOURNAL.exists():raise ControlError('An environment recovery is pending. Resolve it before another operation.')
        c,service,files,args=inspect_target(project,name)
        if c['Id']!=body.get('container_id'):raise ControlError('The container was replaced. Reopen its details before making a change.')
        was_running=c['State']['Running']
        if action=='environment-save':
            validate_changes(body.get('changes'))
            assert_image(configuration(args),service,c['Image'])
            if revision(files,c,configuration(args))!=body.get('revision'):raise ControlError('Configuration changed. Reopen the editor before saving.')
        now=time.time()
        db.execute('INSERT INTO control_runs(request_id,created,updated,actor,project,container,operation,status,detail) VALUES(?,?,?,?,?,?,?,?,?)',(key,now,now,actor,project,name,action,'running','Operation started.'))
        audit(db,actor,action,project,json.dumps({'container':name,'keys':sorted(body.get('changes',{}))}))
        try:
            if action=='environment-save':
                before_config=configuration(args)
                owner=next((f for f in reversed(files) if service in (yaml.safe_load(f.read_text()) or {}).get('services',{})),None)
                if owner is None:raise ControlError('Service configuration is incomplete.')
                before=owner.read_text();mode=owner.stat().st_mode&0o777
                document=mutate_service(yaml.safe_load(before),service,body['changes'])
                record={'path':str(owner),'before':before,'mode':mode,'compose':args,'service':service,'container':name,'was_running':was_running,'image_id':c['Image'],'environment':effective_environment(c),'activated':False,'request_id':key}
                atomic_write(JOURNAL,json.dumps(record),0o600)
                # Compose now contains explicitly edited secrets: keep it owner-only.
                atomic_write(owner,yaml.safe_dump(document,sort_keys=False),0o600)
                after=configuration(args)
                expected=copy.deepcopy(before_config)
                expected['services'][service].setdefault('environment',{}).update(body['changes'])
                if after!=expected:raise ControlError('The change would affect other configuration. It was rejected.')
                record['activated']=True;atomic_write(JOURNAL,json.dumps(record),0o600)
                recreate(args,service,was_running)
                updated=wait_runtime(name,was_running)
                actual=effective_environment(updated)
                expected_env=effective_environment(c);expected_env.update(body['changes'])
                if updated['Image']!=c['Image'] or actual!=expected_env:raise ControlError('Runtime environment does not match the saved changes.')
                JOURNAL.unlink()
                message='Environment saved and verified. The service was recreated; its previous running or stopped state was preserved.'
            else:
                operation=action.removeprefix('container-')
                command([DOCKER,operation,*(['--time','20'] if operation in ('stop','restart') else []),name],timeout=60)
                wait_runtime(name,operation!='stop')
                message='Container '+{'start':'started','stop':'stopped','restart':'restarted'}[operation]+' and runtime state verified.'
            status='succeeded'
        except Exception:
            if JOURNAL.exists():
                try:restore_environment();message='The change failed. Previous environment and runtime state were restored.';status='rolled-back'
                except Exception:message='Environment recovery needs attention. Further changes are blocked.';status='recovery-failed'
            else:message='Operation failed. Check the current container state before retrying.';status='failed'
        db.execute('UPDATE control_runs SET status=?,detail=?,updated=? WHERE request_id=?',(status,message,time.time(),key))
        return {'status':status,'message':message}
    finally:
        if db is not None:db.close()
        LOCK.release()

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(10)
        try:
            line=self.rfile.readline(262145)
            if len(line)>262144:raise ControlError('Request is too large.')
            result=execute(json.loads(line))
            response={'ok':True,'result':result}
        except ControlError as error:response={'ok':False,'error':str(error)}
        except Exception:response={'ok':False,'error':'Container management is temporarily unavailable.'}
        try:self.wfile.write(json.dumps(response).encode()+b'\n')
        except (BrokenPipeError,ConnectionResetError):pass

def start():
    # Recover a pending environment transaction before admitting mutations.
    if JOURNAL.exists():restore_environment()
    db=connect(DATABASE)
    db.execute("UPDATE control_runs SET status='interrupted',detail='Worker restarted. Inspect current state before retrying.',updated=? WHERE status='running'",(time.time(),));db.close()
    path=Path(SOCKET)
    if path.exists():path.unlink()
    server=socketserver.ThreadingUnixStreamServer(SOCKET,Handler)
    server.daemon_threads=True
    os.chmod(SOCKET,0o660)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server
