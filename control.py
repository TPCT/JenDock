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
from application_operations import OperationError
from database_bindings import BindingError, SaveRejected
from database_inspection import InspectionError
from database_candidates import CandidateError
from database_inventory import InventoryError
from database_helper import HelperError
from compose_identity import ordered_files
from store import connect, effective_projects, audit

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
    policy_db=connect(DATABASE)
    try:policy=effective_projects(policy_db,CONFIG).get(project_name)
    finally:policy_db.close()
    if not policy or not isinstance(container_name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{1,127}',container_name):
        raise ControlError('Unknown project or container.')
    result = json.loads(command([DOCKER,'inspect',container_name]))[0]
    if result['Name'].lstrip('/') != container_name:
        raise ControlError('Container identity changed. Refresh and try again.')
    labels = result['Config'].get('Labels') or {}
    group,service = labels.get('com.docker.compose.project'),labels.get('com.docker.compose.service')
    if not group or not service or not re.fullmatch(r'[a-zA-Z0-9_.-]+',group+service):
        raise ControlError('Container has no valid Compose owner.')
    try:
        observed_files = ordered_files(labels.get('com.docker.compose.project.config_files', '').split(','))
        import ownership
        policy_db = connect(DATABASE)
        try:
            owner = ownership.resolve(policy_db, project_name, group, service, list(observed_files))
        finally:
            policy_db.close()
        if owner:
            recipe = owner['recipe']
            result['_monitor_environment'] = owner['environment']
        else:
            if not any(container_name.startswith(p) for p in policy.get('container_prefixes', [])):
                raise ControlError('This container does not belong to this project.')
            recipe = policy.get('recipe') or {}
            if group != recipe.get('compose_project'):
                raise ControlError('Container Compose ownership does not match this project.')
        expected_files = ordered_files(recipe.get('compose_files'))
    except (ValueError, AttributeError) as error:
        raise ControlError(str(error) or 'Container configuration identity is invalid.') from None
    if 'owned_services' in recipe or recipe.get('service_provenance') is not None:
        import provenance
        if not provenance.matches(recipe, group, service, list(observed_files)):
            raise ControlError('Container configuration does not match the registered project.')
        try:
            for owned in recipe.get('service_provenance', {}):
                if owned not in recipe.get('owned_services', recipe.get('services', [])):
                    raise ValueError('Compose provenance claims an unowned service.')
                provenance.revalidate(recipe, owned, review_compose)
        except ValueError as error:raise ControlError(str(error)) from None
    elif observed_files != expected_files:
        raise ControlError('Container configuration does not match the registered project.')
    # Operate the canonical invocation only after alternate provenance passes.
    files = [checked_path(f) for f in expected_files]
    args = [COMPOSE,'--profile','*','-p',group]
    for f in files: args += ['-f',str(f)]
    # Resolve from the actual Compose project directory, including its .env file.
    directory = files[0].parent
    dotenv = checked_path(recipe['env_file']) if recipe.get('env_file') else directory/'.env'
    if dotenv.exists():args += ['--env-file',str(checked_path(dotenv))]
    result['_monitor_recipe'] = recipe
    return result,service,files,args

def review_compose(recipe, service, observed_files):
    from compose_review import review
    return review(recipe, service, observed_files, ROOT, command, COMPOSE)

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
    options=['up','-d'] if running else ['up','--no-start']
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
    if 'previous_resources' in record and resource_limits(restored)!=record['previous_resources']:
        raise ControlError('Previous resource limits were not restored.')
    if record.get('resource_transition'):
        import direct_limits
        db=connect(DATABASE)
        try:direct_limits.rollback(db,record['resource_transition'])
        finally:db.close()
    if record.get('provenance_transition'):
        import provenance_store
        db=connect(DATABASE)
        try:provenance_store.rollback(db,record['provenance_transition'])
        finally:db.close()
    JOURNAL.unlink()


def managed_runtime_service(db,project,container):
    """Resolve only a registered managed binding, never infer from name prefixes."""
    import managed_services
    identifier=(container.get('Config',{}).get('Labels') or {}).get('net.shiftcodes.managed.service')
    if not identifier:return None
    try:row=managed_services.get(db,identifier)
    except ValueError:raise ControlError('Managed service registration is unavailable.') from None
    if row['project']!=project or not row.get('binding') or row['binding']['container_id']!=container['Id']:
        raise ControlError('Managed service binding changed. Reconcile it before editing.')
    return row


def target_configuration(container,files,args):
    if (container.get('_monitor_recipe') or {}).get('managed_environment'):
        if len(files)!=1:raise ControlError('Managed services require one canonical manifest.')
        try:return json.loads(files[0].read_text())
        except (ValueError,OSError):raise ControlError('Managed service manifest is invalid.') from None
    return configuration(args)


def save_managed_environment(db,body,container,service):
    import service_runtime
    import service_runtime_operations
    import workload_environment
    workload_environment.validate_user_values(body['changes'])
    p,e=service['project'],service['environment']
    def authorize():
        if p not in effective_projects(db,CONFIG):raise ControlError('Project authority is unavailable.')
        current=__import__('managed_services').get(db,service['id'])
        if current['project']!=p or current['environment']!=e:raise ControlError('Service authority changed.')
    key_path=os.environ.get('DATABASE_INSPECTION_KEY_FILE','')
    # Preserve the editor's existing effective values, including image defaults;
    # only explicitly submitted keys are changed and every value is encrypted.
    pin=(service['definition'].get('runtime') or {}).get('database_attachment')
    if pin is not None:
        import application_database_bindings
        reserved=application_database_bindings.recorded_environment_keys(db,p,e,service['id'],pin)
        if set(body['changes']) & set(reserved):
            raise ControlError('Database-managed variables must be edited through Application database settings.')
    values=workload_environment.user_values(effective_environment(container));values.update(body['changes'])
    service_runtime_operations.accept(db,body['request_id'],service['id'],values,
        expected_container_id=container['Id'],key_path=key_path,authorize=authorize,request=body,
        **({'database_attachment':body['database_attachment']} if 'database_attachment' in body else {}))
    return service_runtime_operations.apply(db,body['request_id'],docker=DOCKER,compose=COMPOSE,authorize=authorize)

def managed_lifecycle_action(db,body,container,service):
    import managed_lifecycle
    p,e=service['project'],service['environment']
    def authorize():
        if p not in effective_projects(db,CONFIG):raise ControlError('Project authority is unavailable.')
        current=__import__('managed_services').get(db,service['id'])
        if current['project']!=p or current['environment']!=e:raise ControlError('Service authority changed.')
    managed_lifecycle.accept(db,body['request_id'],service['id'],body['action'].removeprefix('container-'),
        expected_container_id=container['Id'],docker=DOCKER,authorize=authorize,request=body)
    return managed_lifecycle.apply(db,body['request_id'],docker=DOCKER,authorize=authorize)


def save_managed_resources(db,body,container,service,limits,transition):
    import service_runtime_operations
    import workload_environment
    p,e=service['project'],service['environment']
    def authorize():
        if p not in effective_projects(db,CONFIG):raise ControlError('Project authority is unavailable.')
        current=__import__('managed_services').get(db,service['id'])
        if current['project']!=p or current['environment']!=e:raise ControlError('Service authority changed.')
    service_runtime_operations.accept(db,body['request_id'],service['id'],workload_environment.user_values(effective_environment(container)),
        expected_container_id=container['Id'],key_path=os.environ.get('DATABASE_INSPECTION_KEY_FILE',''),
        authorize=authorize,resources=limits,resource_transition=transition)
    return service_runtime_operations.apply(db,body['request_id'],docker=DOCKER,compose=COMPOSE,authorize=authorize)

def details(project, name, _with_identity=False):
    c,service,files,args=inspect_target(project,name)
    image=json.loads(command([DOCKER,'image','inspect',c['Image']]))[0]
    config=target_configuration(c,files,args)
    owner=next((f for f in reversed(files) if service in (yaml.safe_load(f.read_text()) or {}).get('services',{})),None)
    if service not in config['services'] or owner is None:raise ControlError('Service configuration is incomplete.')
    env=effective_environment(c)
    result={'container':name,'container_id':c['Id'],'service':service,'image':c['Config']['Image'],
            'image_id':c['Image'],'image_created':image.get('Created'),'image_digests':image.get('RepoDigests',[]),
            'commit':(image.get('Config',{}).get('Labels') or {}).get('org.opencontainers.image.revision'),
            'state':c['State']['Status'],'health':c['State'].get('Health',{}).get('Status','not configured'),
            'started_at':c['State'].get('StartedAt'),'restart_count':c.get('RestartCount',0),
            'environment':env,'revision':revision(files,c,config),'configuration_file':str(owner),
            'environment_scope':'Overrides apply to this Compose service only. Other services sharing an env file are unchanged.',
            'resources':resource_limits(c),
            'ports':c.get('NetworkSettings',{}).get('Ports',{}),
            'mounts':[{'type':m['Type'],'destination':m['Destination'],'read_only':not m['RW']} for m in c.get('Mounts',[])]}
    metadata_db=connect(DATABASE)
    try:
        managed=managed_runtime_service(metadata_db,project,c)
        if managed:
            import service_runtime
            result['managed_service_id']=managed['id']
            result['managed_environment']=managed['environment']
            result['runtime']=service_runtime.status(metadata_db,project,managed['environment'],managed['id'])
            result['runtime']['platform_managed_variables']=sorted(key for key in env if __import__('workload_environment').reserved(key))
            pin=(managed['definition'].get('runtime') or {}).get('database_attachment')
            result['runtime']['database_attachment']=copy.deepcopy(pin)
            result['runtime']['database_managed_variables']=[]
            if pin is not None:
                import application_database_bindings
                result['runtime']['database_managed_variables']=application_database_bindings.recorded_environment_keys(
                    metadata_db,project,managed['environment'],managed['id'],pin)

    finally:metadata_db.close()
    if _with_identity:result['_actual']=c
    return result

def resource_limits(c):
    host=c.get('HostConfig',{})
    cpu=host.get('NanoCpus',0)/1e9
    if not cpu and host.get('CpuQuota',0)>0:cpu=host['CpuQuota']/(host.get('CpuPeriod') or 100000)
    return {'memory_mib':host.get('Memory',0)/1048576,'cpu':cpu,
            'swap_mib':host.get('MemorySwap',0)/1048576}

def mutate_resources(document,service,limits):
    result=copy.deepcopy(document)
    target=copy.deepcopy(result['services'][service]);result['services'][service]=target
    target['mem_limit']=int(limits['memory_mib']*1048576)
    target['memswap_limit']=target['mem_limit']
    target['cpus']=limits['cpu']
    # Use one CPU control mechanism; preserve unrelated deployment settings.
    target.pop('cpu_quota',None);target.pop('cpu_period',None)
    deploy=target.setdefault('deploy',{}).setdefault('resources',{}).setdefault('limits',{})
    deploy.update(memory=str(target['mem_limit']),cpus=str(limits['cpu']))
    return result

def without_resource_limits(config,service):
    result=copy.deepcopy(config);target=copy.deepcopy(result['services'][service]);result['services'][service]=target
    for key in ('mem_limit','memswap_limit','cpus','cpu_quota','cpu_period'):target.pop(key,None)
    limits=target.get('deploy',{}).get('resources',{}).get('limits',{})
    limits.pop('memory',None);limits.pop('cpus',None)
    if not limits:target.get('deploy',{}).get('resources',{}).pop('limits',None)
    if not target.get('deploy',{}).get('resources',{}):target.get('deploy',{}).pop('resources',None)
    if target.get('deploy',{}).get('placement') == {}:target['deploy'].pop('placement')
    if not target.get('deploy',{}):target.pop('deploy',None)
    return result

def database_inspector(db,authority):
    import base64
    import hmac
    import database_bindings
    from database_candidates import resolve
    from database_inventory import DockerInventory
    from database_helper import InspectorHelper
    from database_inspection import InspectorService
    key_path=os.environ.get('DATABASE_INSPECTION_KEY_FILE')
    material=database_bindings._key_material(key_path)
    cursor_key=base64.urlsafe_b64encode(hmac.digest(material,b'database-cursors-v1','sha256')).decode()
    state=Path('/worker-state/database-inspector')
    state.mkdir(mode=0o700,exist_ok=True)
    socket=os.environ.get('DOCKER_HOST','unix:///run/docker-isolation.sock')
    inventory=DockerInventory(docker=DOCKER,socket=socket,timeout=8)
    def candidate(project,environment,reference,**options):
        return resolve(db,project,environment,reference,inventory=inventory,provenance_resolver=review_compose,**options)
    helper=InspectorHelper(docker=DOCKER,socket=socket,image=os.environ.get('DATABASE_INSPECTOR_IMAGE'),state_dir=state)
    return InspectorService(db,authority,resolve_candidate=candidate,helper=helper,key_path=key_path,cursor_key=cursor_key)

def execute(body, *, peer_uid=None):
    if not isinstance(body,dict):raise ControlError('Invalid operation request.')
    if isinstance(body.get('action'),str) and body['action'].startswith('database-'):
        import database_control
        import database_rpc
        envelope=body.get('authentication')
        if isinstance(envelope,dict) and envelope.get('kind')=='mcp':
            # MCP is disabled until an independent peer UID is explicitly configured.
            if 'DATABASE_MCP_UID' not in os.environ:
                raise ControlError('MCP database control is not configured.')
            expected_peer=database_rpc.expected_uid('DATABASE_MCP_UID',-1)
        else:
            expected_peer=database_rpc.expected_uid('DATABASE_RECEIVER_UID',65534)
        if peer_uid != expected_peer:
            raise ControlError('Database control peer is not authorized.')
        if not LOCK.acquire(blocking=False):raise ControlError('Another operation is running. Retry after it completes.')
        db=None
        try:
            if body['action'] in ('database-options','database-preview','database-accept','database-read') and (JOURNAL.exists() or (Path(os.environ.get('STATE_DIR','/worker-state'))/'transaction.json').exists()):
                raise ControlError('Database inspection is unavailable while environment recovery is pending.')
            db=connect(DATABASE)
            return database_control.execute(db,body,key_path=os.environ.get('DATABASE_INSPECTION_KEY_FILE'),inspection_factory=database_inspector)
        finally:
            if db is not None:db.close()
            LOCK.release()
    action=body.get('action');project=body.get('project');name=body.get('container');actor=body.get('actor','admin')
    if action in ('adoption-preview','adoption-save'):
        import adoption
        if not LOCK.acquire(blocking=False):raise ControlError('Another deployment or container operation is running. Retry after it completes.')
        db=None
        try:
            db=connect(DATABASE)
            effective_projects(db,CONFIG)
            if JOURNAL.exists():raise ControlError('Container recovery is pending. Resolve it before adopting a workload.')
            inventory=adoption.collect(DOCKER,command)
            if action=='adoption-preview':return adoption.public(adoption.preview(db,project,body.get('environment'),inventory,resolver=review_compose))
            result=adoption.adopt(db,project,body.get('environment'),body.get('fingerprint'),inventory,actor,resolver=review_compose)
            audit(db,actor,action,project,'Existing workload adopted as '+body['environment']+'. No containers or data moved.')
            return {'project':result,'message':'Existing workload linked to '+body['environment']+'. No containers or data were moved.'}
        except ValueError as error:raise ControlError(str(error)) from None
        finally:
            if db is not None:db.close()
            LOCK.release()
    if action in ('terminal-open','terminal-poll','terminal-close'):
        from terminal_control import execute as terminal_execute
        return terminal_execute(body)
    if action=='container-logs':
        from container_logs import read
        try:return read(body)
        except ValueError as error:raise ControlError(str(error)) from None
    if action in ('container-files-list', 'container-files-preview'):
        from container_files import FileBrowser, FileBrowserError
        from container_file_changes import read as file_changes
        browser = FileBrowser(inspector=inspect_target,
                              proc_root=os.environ.get('CONTAINER_FILES_PROC_ROOT', '/proc'),
                              change_provider=file_changes)
        try:
            arguments = (project, name, body.get('container_id'))
            if action == 'container-files-list':
                return browser.list(*arguments, path=body.get('path', ''),
                                    cursor=body.get('cursor'), limit=body.get('limit', 100))
            return browser.preview(*arguments, path=body.get('path', ''))
        except FileBrowserError as error:
            raise ControlError(str(error)) from None
    if action=='container-details':return details(project,name)
    if action=='resource-details':
        from direct_limits import context
        result=details(project,name,_with_identity=True);actual=result.pop('_actual');result.pop('environment',None)
        policy_db=connect(DATABASE)
        try:
            try:result['resource_policy']=context(policy_db,project,name,actual)
            except ValueError as error:result['resource_policy']={'editable':False,'reason':str(error),'configured':True,'limits_ceiling':None}
        finally:policy_db.close()
        return result
    if action not in ('container-start','container-stop','container-restart','environment-save','resource-apply','container-limits-save'):
        raise ControlError('Unsupported container operation.')
    key=body.get('request_id','')
    if not re.fullmatch(r'[A-Za-z0-9-]{16,80}',key):raise ControlError('Invalid operation ID.')
    if not LOCK.acquire(blocking=False):raise ControlError('Another deployment or container operation is running. Try again after it completes.')
    db=None
    try:
        db=connect(DATABASE)
        if action=='container-limits-save':
            import direct_limits
            try:
                previous=direct_limits.replay(db,body)
                if previous:return previous
            except ValueError as error:raise ControlError(str(error))
        previous=db.execute('SELECT status,detail,project,container,operation FROM control_runs WHERE request_id=?',(key,)).fetchone()
        if previous:
            if (previous['project'],previous['container'],previous['operation'])!=(project,name,action):raise ControlError('Operation ID belongs to a different request.')
            if action in ('container-start','container-stop','container-restart'):
                import managed_lifecycle
                try:
                    result=managed_lifecycle.replay_request(db,body)
                    if result:return result
                except ValueError as error:raise ControlError(str(error)) from None
            if action=='environment-save':
                import service_runtime_operations
                try:
                    result=service_runtime_operations.replay_request(db,body,key_path=os.environ.get('DATABASE_INSPECTION_KEY_FILE',''))
                    if result:return result
                except ValueError as error:raise ControlError(str(error)) from None
            return {'status':previous['status'],'message':previous['detail']}
        if JOURNAL.exists():raise ControlError('An environment recovery is pending. Resolve it before another operation.')
        c,service,files,args=inspect_target(project,name)
        if c['Id']!=body.get('container_id'):raise ControlError('The container was replaced. Reopen its details before making a change.')
        managed=managed_runtime_service(db,project,c)
        if managed:
            import service_runtime_operations
            if service_runtime_operations.pending(db,managed['project'],managed['environment']):raise ControlError('Runtime settings require reconciliation before further changes.')
        was_running=c['State']['Running']
        if action in ('environment-save','resource-apply','container-limits-save'):
            if action=='environment-save':
                if not (managed and 'database_attachment' in body and body.get('changes')=={}):validate_changes(body.get('changes'))
            elif action=='container-limits-save':
                try:
                    limits=direct_limits.limits(body.get('limits'))
                    direct_limits.check(db,project,name,limits,c)
                except ValueError as error:raise ControlError(str(error))
            else:
                from resource_policy import allocation
                try:limits=allocation(db,project,body.get('environment'),name,body.get('policy_revision'))
                except ValueError as error:raise ControlError(str(error))
            assert_image(target_configuration(c,files,args),service,c['Image'])
            if revision(files,c,target_configuration(c,files,args))!=body.get('revision'):raise ControlError('Configuration changed. Reopen the editor before saving.')
        now=time.time()
        resource_transition=None
        if action=='container-limits-save':
            try:resource_transition=direct_limits.admit(db,body,c)
            except ValueError as error:raise ControlError(str(error))
        else:
            db.execute('INSERT INTO control_runs(request_id,created,updated,actor,project,container,operation,status,detail) VALUES(?,?,?,?,?,?,?,?,?)',(key,now,now,actor,project,name,action,'running','Operation started.'))
        audit(db,actor,action,project,json.dumps({'container':name,'keys':sorted(body.get('changes',{}))}))
        try:
            if managed and action in ('container-start','container-stop','container-restart'):
                return managed_lifecycle_action(db,body,c,managed)
            if action=='environment-save' and managed:
                result=save_managed_environment(db,body,c,managed)
                db.execute('UPDATE control_runs SET status=?,detail=?,updated=? WHERE request_id=?',
                           (result['status'],result['message'],time.time(),key))
                return result
            if managed and action in ('resource-apply','container-limits-save'):
                result=save_managed_resources(db,body,c,managed,limits,resource_transition)
                db.execute('UPDATE control_runs SET status=?,detail=?,updated=? WHERE request_id=?',
                           (result['status'],result['message'],time.time(),key))
                return result
            if action=='resource-apply':
                limits=allocation(db,project,body.get('environment'),name,body.get('policy_revision'))
            if action in ('environment-save','resource-apply','container-limits-save'):
                before_config=configuration(args)
                owner=next((f for f in reversed(files) if service in (yaml.safe_load(f.read_text()) or {}).get('services',{})),None)
                if owner is None:raise ControlError('Service configuration is incomplete.')
                before=owner.read_text();mode=owner.stat().st_mode&0o777
                document=mutate_service(yaml.safe_load(before),service,body['changes']) if action=='environment-save' else mutate_resources(yaml.safe_load(before),service,limits)
                record={'path':str(owner),'before':before,'mode':mode,'compose':args,'service':service,'container':name,'was_running':was_running,'image_id':c['Image'],'environment':effective_environment(c),'activated':False,'request_id':key}
                if action in ('resource-apply','container-limits-save'):record['previous_resources']=resource_limits(c)
                if resource_transition:record['resource_transition']=resource_transition
                atomic_write(JOURNAL,json.dumps(record),0o600)
                # Compose now contains explicitly edited secrets: keep it owner-only.
                atomic_write(owner,yaml.safe_dump(document,sort_keys=False),0o600)
                after=configuration(args)
                if action=='environment-save':
                    expected=copy.deepcopy(before_config)
                    expected['services'][service].setdefault('environment',{}).update({k:v.replace('$','$$') for k,v in body['changes'].items()})
                    if after!=expected:raise ControlError('The change would affect other configuration. It was rejected.')
                elif without_resource_limits(after,service)!=without_resource_limits(before_config,service):
                    raise ControlError('Resource changes would affect other configuration. They were rejected.')
                record['activated']=True;atomic_write(JOURNAL,json.dumps(record),0o600)
                recreate(args,service,was_running)
                updated=wait_runtime(name,was_running)
                actual=effective_environment(updated)
                expected_env=effective_environment(c)
                if action=='environment-save':expected_env.update(body.get('changes',{}))
                if updated['Image']!=c['Image'] or actual!=expected_env:raise ControlError('Runtime environment does not match the saved changes.')
                if action in ('resource-apply','container-limits-save'):
                    observed=resource_limits(updated)
                    if abs(observed['memory_mib']-limits['memory_mib'])>.001 or abs(observed['cpu']-limits['cpu'])>.00001 or abs(observed['swap_mib']-limits['memory_mib'])>.001:
                        raise ControlError('Runtime resource limits differ from the requested allocation.')
                if resource_transition:direct_limits.commit(db,resource_transition)
                import provenance_store
                transition=provenance_store.prepare(db,project,c.get('_monitor_recipe',{}),review_compose,
                    recreated={service: updated})
                if transition:
                    record['provenance_transition']=transition
                    atomic_write(JOURNAL,json.dumps(record),0o600)
                    provenance_store.commit(db,transition)
                JOURNAL.unlink()
                message=('RAM and CPU limits applied and verified.' if action in ('resource-apply','container-limits-save') else 'Environment saved and verified.')+' The service was recreated; its previous running or stopped state was preserved.'
            else:
                operation=action.removeprefix('container-')
                command([DOCKER,operation,*(['--time','20'] if operation in ('stop','restart') else []),c['Id']],timeout=60)
                wait_runtime(c['Id'],operation!='stop')
                message='Container '+{'start':'started','stop':'stopped','restart':'restarted'}[operation]+' and runtime state verified.'
            status='succeeded'
        except Exception:
            if JOURNAL.exists():
                try:restore_environment();message='The change failed. Previous environment and runtime state were restored.';status='rolled-back'
                except Exception:message='Environment recovery needs attention. Further changes are blocked.';status='recovery-failed'
            else:
                message='Operation failed. Check the current container state before retrying.';status='failed'
                if managed and action in ('container-start','container-stop','container-restart'):
                    try:
                        import managed_lifecycle
                        result=managed_lifecycle.public(db,key);status=result['status'];message=result['message']
                    except ValueError:pass
                if managed and action in ('environment-save','resource-apply','container-limits-save'):
                    try:
                        result=service_runtime_operations.public(db,key);status=result['status'];message=result['message']
                    except ValueError:pass
        db.execute('UPDATE control_runs SET status=?,detail=?,updated=? WHERE request_id=?',(status,message,time.time(),key))
        return {'status':status,'message':message}
    finally:
        if db is not None:db.close()
        LOCK.release()

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(10)
        body=None
        try:
            line=self.rfile.readline(262145)
            if len(line)>262144 or not line.endswith(b'\n'):raise ControlError('Request is incomplete or too large.')
            import database_rpc
            body=database_rpc.decode(line)
            if isinstance(body,dict) and isinstance(body.get('action'),str) and body['action'].startswith('database-') and len(line)>65536:
                raise ControlError('Database request is too large.')
            result=execute(body,peer_uid=database_rpc.peer_uid(self.connection))
            response={'ok':True,'result':result}
        except OperationError as error:
            response={'ok':False,'error':str(error),'status':error.status}
        except InspectionError as error:
            response={'ok':False,'error':str(error),'code':error.code,'status':409}
        except SaveRejected as error:
            response={'ok':False,'error':str(error),'status':409,'outcome':'not_committed'}
        except (BindingError,CandidateError,InventoryError,HelperError) as error:
            response={'ok':False,'error':str(error),'status':409}
        except ControlError as error:response={'ok':False,'error':str(error)}
        except Exception:response={'ok':False,'error':'Container management is temporarily unavailable.'}
        encoded=json.dumps(response,allow_nan=False).encode()+b'\n'
        if isinstance(body,dict) and isinstance(body.get('action'),str) and body['action'].startswith('database-') and len(encoded)>262144:
            encoded=b'{"ok":false,"error":"Response is too large. Request a smaller page.","status":413}\n'
        try:self.wfile.write(encoded)
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
