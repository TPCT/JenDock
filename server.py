"""Signed GitHub receiver and authenticated deployment dashboard; no Docker access."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
from http.cookies import SimpleCookie
from urllib.parse import urlsplit
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from telemetry import history as metric_history
from delivery_target import capture_registered_target, request_identity, assert_retry_environment, webhook_bindings, public_job
from store import connect, enqueue, projects, effective_projects, valid_branch, audit, set_status, ensure_registry, existing_delivery, assert_target_identity

PREFIX = '/_deploy'
MAX_BODY = 1024 * 1024


class Application:
    def __init__(self, config, database, credentials):
        self.integration_key = os.environ.get("INTEGRATION_KEY_FILE", "/config/integrations.key")
        self.config = config
        self.database = database
        self.web_root = Path(os.environ.get('WEB_ROOT', str(Path(__file__).parent / 'web'))).resolve()
        self.credentials = json.loads(Path(credentials).read_text())
        self.failures = {}
        self.lock = threading.Lock()
        self.origin = os.environ.get('PUBLIC_ORIGIN', 'https://monitoring.shiftcodes.net')
        self.secure = not self.origin.startswith('http://127.0.0.1:')
        self.cookie_name = '__Host-monitor_session' if self.secure else 'monitor_session'

    def authorized(self, header, peer):
        now = time.monotonic()
        with self.lock:
            self.failures = {k: v for k, v in self.failures.items() if v[1] > now - 60}
            if self.failures.get(peer, (0, 0))[0] >= 10:
                return False
            self.failures[peer] = (self.failures.get(peer, (0, now))[0] + 1, now)
        good = False
        try:
            scheme, value = header.split(' ', 1)
            if scheme.lower() != 'basic' or len(value) > 1024:
                return False
            user, password = base64.b64decode(value, validate=True).decode().split(':', 1)
            digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(self.credentials['salt']), 260000)
            good = hmac.compare_digest(user, self.credentials['username']) and hmac.compare_digest(digest.hex(), self.credentials['password_hash'])
        except (ValueError, UnicodeError, TypeError):
            pass
        with self.lock:
            if good:
                self.failures.pop(peer, None)
        return good

    def session(self, cookie, touch=False):
        try:
            parsed = SimpleCookie(cookie)
            token = parsed[self.cookie_name].value
        except (KeyError, ValueError):
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        db = connect(self.database)
        try:
            now = time.time()
            db.execute('DELETE FROM sessions WHERE created<? OR touched<?', (now-43200, now-3600))
            row = db.execute('SELECT * FROM sessions WHERE token=?', (digest,)).fetchone()
            if row:
                if touch:
                    db.execute('UPDATE sessions SET touched=? WHERE token=?', (now,digest))
                return dict(row)
        finally:
            db.close()
        return None

    def login(self, body, peer):
        user, password = body.get('username',''), body.get('password','')
        if not isinstance(user,str) or not isinstance(password,str) or len(password)>512:
            return 400, {'error':'Invalid login details.'}, None
        header = 'Basic ' + base64.b64encode((user+':'+password).encode()).decode()
        if not self.authorized(header,peer):
            return 401, {'error':'Sign-in failed. Check your credentials. After repeated attempts, wait one minute.'}, None
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db = connect(self.database)
        try:
            now=time.time()
            db.execute('DELETE FROM sessions WHERE created<? OR touched<?',(now-43200,now-3600))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),csrf,user,now,now))
            audit(db,user,'login')
        finally:
            db.close()
        return 200, {'username':user,'csrf':csrf}, token

    def action(self, session, body):
        db=connect(self.database)
        try:
            action=body.get('action')
            if action=='logout':
                db.execute('DELETE FROM sessions WHERE token=?',(session['token'],))
                audit(db,session['username'],'logout')
                return 200, {'ok':True}
            if action in ('mcp-access-list', 'mcp-access-create', 'mcp-access-revoke'):
                import application_operations as operations
                import mcp_credentials
                owner = operations.Principal(session['username'], 'console', frozenset({
                    'projects:read', 'projects:create', 'services:read', 'services:create',
                    'services:start', 'operations:read'}))
                try:
                    if action == 'mcp-access-list':
                        return 200, {'credentials': mcp_credentials.list_credentials(db, owner),
                                     'endpoint': os.environ.get('MCP_PUBLIC_URL', self.origin + '/_deploy/mcp')}
                    if action == 'mcp-access-create':
                        result = mcp_credentials.create(db, owner, body.get('values'))
                        return 201, result
                    return 200, {'credential': mcp_credentials.revoke(db, owner, body.get('credential_id'))}
                except operations.OperationError as error:
                    return error.status, {'error': str(error), 'code': error.code}
            if action in ('integrations-list','integrations-save','integrations-test','integrations-disable'):
                import integrations
                provider=body.get('provider')
                try:
                    if action=='integrations-list':return 200,{'connections':integrations.list_connections(db)}
                    if action=='integrations-save':
                        connection=integrations.save_connection(db,provider,body.get('revision'),body.get('credential'),self.integration_key,options=body.get('options'),actor=session['username'])
                        message='Connection verified and saved.'
                    elif action=='integrations-test':
                        connection=integrations.test_connection(db,provider,self.integration_key,actor=session['username'])
                        message='Connection verified.'
                    else:
                        connection=integrations.disable_connection(db,provider,body.get('revision'),actor=session['username'])
                        message='Connection disabled in this console. The provider credential was not revoked.'
                    audit(db,session['username'],action,detail=provider+' revision '+str(connection['revision']))
                    return 200,{'connection':connection,'message':message}
                except integrations.IntegrationError as error:
                    return 409,{'error':str(error)}
            if action in ('service-templates', 'service-list', 'service-versions', 'service-get', 'service-create', 'service-resume'):
                import application_operations as operations
                ensure_registry(db, self.config)
                principal = operations.Principal(session['username'], 'console',
                    frozenset({'services:read', 'services:create', 'services:start', 'operations:read'}))
                try:
                    if action == 'service-resume':
                        return 202, operations.resume_service(db, principal, body.get('operation_id'))
                    if action == 'service-templates':
                        return 200, operations.service_templates(db, principal, body.get('project'), body.get('environment'))
                    if action == 'service-versions':
                        return 200, operations.service_versions(db, principal, body.get('project'), body.get('environment'),
                            limit=body.get('limit', 25), cursor=body.get('cursor'))
                    if action == 'service-list':
                        return 200, {'services': operations.list_services(db, principal, body.get('project'), body.get('environment'))}
                    if action == 'service-get':
                        return 200, {'service': operations.get_service(db, principal, body.get('service_id'))}
                    typed = operations.service_input(body.get('project'), body.get('environment'), body.get('values'))
                    return 202, operations.create_service(db, principal, typed, body.get('request_id'))
                except operations.OperationError as error:
                    return error.status, {'error': str(error), 'code': error.code}
                except ValueError:
                    return 409, {'error': 'Service configuration is unavailable or changed. Reload this environment.', 'code': 'service_configuration_changed'}
            if action in ('build-profile-get', 'build-profile-save'):
                import build_profiles
                ensure_registry(db, self.config)
                try:
                    if action == 'build-profile-get':
                        return 200, {'profile': build_profiles.get(db, body.get('project'), body.get('environment'))}
                    profile = build_profiles.save(db, body.get('project'), body.get('environment'),
                        body.get('project_revision'), body.get('environment_revision'), body.get('profile_revision'), body.get('values'))
                    audit(db, session['username'], action, body.get('project'), 'Build profile revision '+str(profile['profile_revision']))
                    return 200, {'profile': profile}
                except ValueError as error:
                    return 409, {'error': str(error), 'code': 'build_profile_changed_or_invalid'}
            if action in ('registry-create','operation-status'):
                import application_operations as operations
                ensure_registry(db,self.config)
                principal=operations.Principal(session['username'],'console',frozenset({'projects:create','operations:read'}))
                try:
                    if action=='operation-status':return 200,{'operation':operations.get_operation(db,principal,body.get('operation_id'))}
                    typed=operations.project_input(body.get('project'),body.get('values'),body.get('integration_ref','github'))
                    return 200,operations.create_project(db,principal,typed,body.get('request_id'),self.integration_key)
                except operations.OperationError as error:
                    return error.status,{'error':str(error),'code':error.code}
            if action=='environment-workspace':
                import workspace
                ensure_registry(db,self.config)
                try:
                    return 200,workspace.read(db,body.get('project'),body.get('environment'))
                except ValueError as error:
                    return 409,{'error':str(error)}
            if action in ('registry-list','registry-get','registry-update','environment-settings'):
                import registry
                ensure_registry(db,self.config)
                name=body.get('project','')
                try:
                    if action=='registry-list':return 200,{'projects':registry.list_projects(db)}
                    if action=='registry-get':return 200,{'project':registry.get_project(db,name)}
                    if action=='registry-update':
                        changes=body.get('changes')
                        if not isinstance(changes,dict) or set(changes)-{'label'}:
                            return 400,{'error':'Only the project display name can be changed here. Workload adoption requires verified service and database identity.'}
                        result=registry.update_project(db,name,body.get('revision'),changes)
                    else:
                        changes=body.get('changes')
                        if not isinstance(changes,dict) or set(changes)-{'branch','automatic'}:
                            return 400,{'error':'Only branch and automatic deployment preferences can be saved here.'}
                        result=registry.update_environment(db,name,body.get('environment'),body.get('revision'),changes)
                        audit(db,session['username'],action,name,'Environment '+result['name']+' revision '+str(result['revision']))
                        return 200,{'environment':result,'message':'Environment preferences saved. No containers were started or changed.'}
                    audit(db,session['username'],action,name,'Project revision '+str(result['revision']))
                    return 200,{'project':result,'message':'Project settings saved.'}
                except ValueError as error:
                    return 409,{'error':str(error)}
            name=body.get('project','')
            p=effective_projects(db,self.config).get(name)
            if not p:
                return 404, {'error':'Project not found.'}
            if action in ('resource-policy','resource-policy-save'):
                from resource_policy import read, save
                try:
                    if action=='resource-policy': return 200,read(db,name)
                    document=body.get('document')
                    for environment in (document or {}).get('environments',{}).values():
                        for container in environment.get('services',{}):
                            if not any(container.startswith(prefix) for prefix in p.get('container_prefixes',[])):
                                return 400,{'error':'Service is outside this project.'}
                    result=save(db,name,body.get('revision'),document)
                    audit(db,session['username'],action,name,'Resource budget revision '+str(result['revision']))
                    return 200,dict(result,message='Resource budgets saved. Runtime limits change only after Apply.')
                except ValueError as error:
                    return 409,{'error':str(error)}
                except (TypeError,AttributeError):
                    return 409,{'error':'Invalid or stale resource policy. Check allocation totals and reload if another session changed settings.'}
            if action in ('adoption-preview','adoption-save','container-files-list','container-files-preview','container-logs','container-details','resource-details','resource-apply','container-limits-save','container-start','container-stop','container-restart','environment-save','terminal-open','terminal-poll','terminal-close'):
                payload=dict(body,actor=session['username'],terminal_owner=session['token'])
                try:
                    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                        client.settimeout(600);client.connect('/state/control.sock')
                        client.sendall(json.dumps(payload).encode()+b'\n')
                        result=json.loads(client.makefile('rb').readline(1048576))
                    if not result['ok']:return 409,{'error':result['error']}
                    return 200,result['result']
                except (OSError,ValueError):
                    return 503,{'error':'Container management is temporarily unavailable. Refresh status before retrying.'}
            if action=='refresh-branches':
                db.execute('INSERT OR REPLACE INTO requests VALUES(?,?)',(name,time.time()))
                return 202, {'message':'Fetching branches from GitHub.'}
            if action=='cancel':
                job=db.execute('SELECT * FROM jobs WHERE id=? AND project=?',(body.get('job_id'),name)).fetchone()
                if not job or job['status'] not in ('queued','validation-queued','awaiting-validation','auto-deploy-disabled'):
                    return 409, {'error':'Only queued runs can be cancelled. Active runs complete testing and recovery.'}
                changed=db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=? AND status IN ('queued','validation-queued','awaiting-validation','auto-deploy-disabled')",(time.time(),job['id'])).rowcount
                if not changed:
                    return 409, {'error':'This run has started and can no longer be cancelled.'}
                db.execute('INSERT INTO events(job_id,created,status,detail) VALUES(?,?,?,?)',(job['id'],time.time(),'cancelled','An administrator cancelled the run before execution.'))
                audit(db,session['username'],'cancel',name,str(job['id']))
                return 200, {'message':'Queued run cancelled.'}
            if action in ('build','retry') and re.fullmatch(r'[a-zA-Z0-9-]{16,80}',body.get('request_id','')):
                try:
                    reviewed=request_identity(body)
                    existing=existing_delivery(db,'manual-'+body['request_id'],name,body.get('sha'),body.get('branch'),body.get('mode'),session['username'],body.get('job_id') if action=='retry' else None)
                    if existing:assert_target_identity(db,existing['id'],reviewed)
                except ValueError as error:return 409,{'error':str(error)}
                if existing:
                    return 200, {'job_id':existing['id'],'message':'This request has already been recorded.'}
            branch=body.get('branch','')
            if not valid_branch(branch):
                return 400, {'error':'Invalid branch name.'}
            row=db.execute('SELECT * FROM snapshots WHERE name=?',('branches:'+name,)).fetchone()
            branches=json.loads(row['value']) if row else {}
            if not row or time.time()-row['updated']>600 or branches.get('error'):
                db.execute('INSERT OR REPLACE INTO requests VALUES(?,?)',(name,time.time()))
                return 409, {'error':'Refresh GitHub branches and wait for completion before retrying.'}
            sha=branches.get('branches',{}).get(branch)
            if not sha:
                return 409, {'error':'The branch was not found in the latest GitHub observation.'}
            if action=='settings':
                automatic=body.get('automatic') is True
                import registry
                try:registry.save_legacy_settings(db,name,branch,automatic)
                except ValueError as error:return 409,{'error':str(error)}
                audit(db,session['username'],'settings',name,json.dumps({'branch':branch,'automatic':automatic}))
                return 200, {'message':'Branch and automatic deployment settings saved. No deployment was started.'}
            if action not in ('build','retry'):
                return 400, {'error':'Unknown action.'}
            mode=body.get('mode')
            if mode not in ('validate','deploy'):
                return 400, {'error':'Choose build validation or deployment.'}
            if body.get('environment') is None and not p.get('recipe'):
                return 409, {'error':'Build definitions and tests are not configured for this project.'}
            if body.get('sha') != sha:
                return 409, {'error':'The branch changed since this form opened. Close it, refresh branches and review the new commit.'}
            retry_of=None
            if action=='retry':
                old=db.execute('SELECT * FROM jobs WHERE id=? AND project=?',(body.get('job_id'),name)).fetchone()
                if not old or old['status'] not in ('failed','interrupted','rolled-back','cancelled','needs-review','superseded','awaiting-validation','auto-deploy-disabled','validated'):
                    return 409, {'error':'This run cannot be retried.'}
                retry_of=old['id']
                try:assert_retry_environment(db,retry_of,request_identity(body)['environment'])
                except ValueError as error:return 409,{'error':str(error)}
            key=body.get('request_id','')
            if not re.fullmatch(r'[a-zA-Z0-9-]{16,80}',key):
                return 400, {'error':'Invalid request ID. Refresh the page.'}
            # Durable idempotency: a lost HTTP response cannot enqueue a second deployment.
            try:
                reviewed=request_identity(body)
                existing=existing_delivery(db,'manual-'+key,name,sha,branch,mode,session['username'],retry_of)
                if existing:assert_target_identity(db,existing['id'],reviewed)
            except ValueError as error:return 409,{'error':str(error)}
            if existing:
                return 200, {'job_id':existing['id'],'message':'This request has already been recorded.'}
            try:
                target_options={field:body.get(field) for field in ('environment','environment_revision','project_revision','recipe_revision','source_revision')}
                job_id,_=enqueue(db,'manual-'+key,name,sha,True,branch,mode,session['username'],retry_of,
                                 target_factory=lambda target_db:capture_registered_target(target_db,name,branch,sha,mode,**target_options),target_identity=reviewed)
            except ValueError as error:
                return 409, {'error':str(error)}
            audit(db,session['username'],action,name,json.dumps({'job_id':job_id,'branch':branch,'sha':sha,'mode':mode,'retry_of':retry_of,'environment':reviewed['environment']}))
            return 202, {'job_id':job_id,'message':'Build queued. Runs execute one at a time.'}
        finally:
            db.close()

    def webhook(self, headers, body):
        signature = headers.get('X-Hub-Signature-256', '')
        if not re.fullmatch(r'sha256=[0-9a-f]{64}', signature):
            return 401, {'error': 'Invalid signature'}
        expected = 'sha256=' + hmac.new(self.credentials['webhook_secret'].encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return 401, {'error': 'Invalid signature'}
        delivery = headers.get('X-GitHub-Delivery', '')
        if not re.fullmatch(r'[A-Za-z0-9-]{1,100}', delivery):
            return 400, {'error': 'Invalid delivery identifier'}
        event = headers.get('X-GitHub-Event', '')
        if event == 'ping':
            return 200, {'accepted': 'ping'}
        if event != 'push':
            return 202, {'ignored': 'event'}
        try:
            payload = json.loads(body)
            repo = payload['repository']['full_name']
            repo_id = payload['repository']['id']
            ref, sha = payload['ref'], payload['after']
            if payload.get('deleted') or sha == '0' * 40:
                return 202, {'ignored': 'deleted ref'}
            if not re.fullmatch('[0-9a-f]{40}', sha):
                raise ValueError()
            lookup=connect(self.database)
            try:
                ensure_registry(lookup,self.config)
                if not isinstance(ref,str) or not ref.startswith('refs/heads/') or not valid_branch(ref.removeprefix('refs/heads/')):
                    raise ValueError()
                matches=webhook_bindings(lookup,repo,repo_id,ref.removeprefix('refs/heads/'))
            finally:
                lookup.close()
        except (ValueError, KeyError, TypeError):
            return 400, {'error': 'Invalid push payload'}
        if not matches:
            return 202, {'ignored': 'repository or branch not approved'}
        db = connect(self.database)
        try:
            results,skipped=[],[]
            for binding in matches:
                name=binding['project'];environment=binding['environment']
                options={key:binding.get(key) for key in ('environment','project_revision','environment_revision','recipe_revision','source_revision')}
                key=delivery+'-'+name+('-'+environment if environment else '')
                identity={'environment':environment or 'legacy-unclassified'}
                try:
                    previous=db.execute('SELECT id,mode FROM jobs WHERE delivery=?',(key,)).fetchone()
                    mode=binding.get('mode','deploy')
                    if previous is not None:
                        import job_target
                        if db.execute('SELECT 1 FROM job_targets WHERE job_id=?',(previous['id'],)).fetchone():
                            pinned=job_target.load(db,previous['id'])
                            if pinned['repository'] != repo or pinned['repository_id'] != repo_id:
                                raise ValueError('Delivery repository identity changed.')
                        mode=previous['mode']
                    result=enqueue(db,key,name,sha,binding['automatic'],binding['branch'],mode=mode,
                                   target_factory=lambda target_db,binding=binding,options=options:capture_registered_target(
                                       target_db,binding['project'],binding['branch'],sha,binding.get('mode','deploy'),execution_enabled=binding['automatic'],**options),
                                   target_identity=identity)
                    results.append(result)
                except ValueError as error:
                    skipped.append({'project':name,'environment':environment or 'legacy-unclassified','reason':str(error)})
                    audit(db,'github','webhook-skipped',name,json.dumps(skipped[-1]))
            return 202, {'jobs': [r[0] for r in results], 'new': any(r[1] for r in results),'skipped':skipped}
        except ValueError as error:
            return 409,{'error':str(error)}
        finally:
            db.close()

    def state(self):
        db = connect(self.database)
        try:
            import registry
            effective=effective_projects(db,self.config)
            registered=registry.list_projects(db)
            allowed = ('repository', 'branch', 'enabled', 'automatic', 'reason', 'label', 'image_prefixes', 'container_prefixes')
            return {'observed_at': time.time(), 'environment': os.environ.get('ENVIRONMENT','production'),
                    'projects': {k: dict({x: v[x] for x in allowed if x in v}, build_ready=bool(v.get('recipe')), has_legacy_workload=registered[k]['legacy_settings'] is not None, legacy_binding=registered[k]['legacy_binding']) for k, v in effective.items()},
                    'metric_history':metric_history(db),
                    'control_runs': [dict(r) for r in db.execute('SELECT * FROM control_runs ORDER BY id DESC LIMIT 100')],
                    'audit': [dict(r) for r in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 100')],
                    'events': [dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 500')],
                    'jobs': [public_job(db,r) for r in db.execute('SELECT * FROM jobs ORDER BY id DESC LIMIT 100')],
                    'snapshots': {r['name']: {'updated': r['updated'], 'value': json.loads(r['value'])}
                                  for r in db.execute('SELECT * FROM snapshots')}}
        finally:
            db.close()


def handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version='DeployMonitor'
        sys_version=''

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self,*args):
            pass

        def path_only(self):
            path=urlsplit(self.path).path
            return path[len(PREFIX):] if path.startswith(PREFIX+'/') else path

        def respond(self,code,value,mime='application/json; charset=utf-8',cookie=None):
            data=json.dumps(value).encode() if isinstance(value,dict) else value
            self.send_response(code)
            self.send_header('Content-Type',mime)
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','same-origin')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if cookie is not None:
                self.send_header('Set-Cookie',app.cookie_name+'='+cookie+'; Path=/; HttpOnly; SameSite=Strict; Max-Age='+('43200' if cookie else '0')+('; Secure' if app.secure else ''))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path=self.path_only()
            if path=='/healthz':
                return self.respond(200,{'status':'ok'})
            session=app.session(self.headers.get('Cookie',''),touch=path=='/api/session')
            if path=='/api/events':
                if not session:
                    return self.respond(401,{'error':'Sign in to continue.'})
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.send_header('Cache-Control','no-cache, no-store')
                self.send_header('X-Accel-Buffering','no')
                self.end_headers()
                previous=None
                try:
                    for _ in range(100):
                        if not app.session(self.headers.get('Cookie','')):
                            self.wfile.write(b'event: expired\ndata: {}\n\n');self.wfile.flush();break
                        state=app.state()
                        state.pop('observed_at',None)
                        payload=json.dumps(state,separators=(',',':'))
                        if payload!=previous:
                            self.wfile.write(('event: state\ndata: '+payload+'\n\n').encode());previous=payload
                        else:
                            self.wfile.write(b': heartbeat\n\n')
                        self.wfile.flush()
                        time.sleep(2)
                except (BrokenPipeError,ConnectionResetError,TimeoutError):
                    pass
                return
            if path=='/api/session':
                return self.respond(200 if session else 401,{'username':session['username'],'csrf':session['csrf']} if session else {'error':'Sign in to continue.'})
            if path=='/api/state':
                return self.respond(200,app.state()) if session else self.respond(401,{'error':'Your session expired. Sign in again.'})
            files={
                '/':('index.html','text/html; charset=utf-8'),
                '/login':('index.html','text/html; charset=utf-8'),
                '/app.js':('app.js','text/javascript; charset=utf-8'),
                '/style.css':('style.css','text/css; charset=utf-8'),
                '/vendor/inter-latin.woff2':('vendor/inter-latin.woff2','font/woff2'),
                '/vendor/inter-LICENSE':('vendor/inter-LICENSE','text/plain; charset=utf-8'),
            }
            # Vite emits hashed route chunks into this flat directory only.
            # Never expose arbitrary web paths or follow assets outside web/.
            if re.fullmatch(r'/assets/[A-Za-z0-9_-]+\.js',path):
                files[path]=(path.lstrip('/'),'text/javascript; charset=utf-8')
            if path not in files:
                return self.respond(404,{'error':'Not found'})
            name,mime=files[path]
            root=app.web_root
            asset=(root/name).resolve()
            if not asset.is_relative_to(root) or not asset.is_file():
                return self.respond(404,{'error':'Not found'})
            return self.respond(200,asset.read_bytes(),mime)

        def do_POST(self):
            path=self.path_only()
            if path not in ('/github','/api/login','/api/action'):
                return self.respond(404,{'error':'Not found'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                maximum=MAX_BODY if path=='/github' else 262144
                if length<=0 or length>maximum or self.headers.get('Transfer-Encoding'):
                    return self.respond(413,{'error':'Invalid body length'})
                body=self.rfile.read(length)
                if len(body)!=length:
                    return self.respond(400,{'error':'Incomplete body'})
                if path=='/github':
                    code,response=app.webhook(self.headers,body)
                    return self.respond(code,response)
                if self.headers.get('Origin')!=app.origin:
                    return self.respond(403,{'error':'Request origin is not allowed.'})
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                    return self.respond(415,{'error':'Expected JSON'})
                value=json.loads(body)
                if not isinstance(value,dict):
                    return self.respond(400,{'error':'Invalid request'})
                if path=='/api/login':
                    # Apache overwrites X-Real-IP; the listener is loopback-only.
                    peer=self.headers.get('X-Real-IP',self.client_address[0])
                    code,response,token=app.login(value,peer)
                    return self.respond(code,response,cookie=token)
                session=app.session(self.headers.get('Cookie',''),touch=True)
                if not session:
                    return self.respond(401,{'error':'Your session expired. Sign in again.'})
                if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),session['csrf']):
                    return self.respond(403,{'error':'Session verification failed. Refresh the page.'})
                code,response=app.action(session,value)
                return self.respond(code,response,cookie='' if value.get('action')=='logout' and code==200 else None)
            except (ValueError,TimeoutError,TypeError):
                return self.respond(400,{'error':'Invalid request'})
    return Handler


if __name__=='__main__':
    application=Application(os.environ.get('PROJECTS_FILE','/config/projects.json'),os.environ.get('DATABASE','/state/state.sqlite'),os.environ.get('CREDENTIALS_FILE','/config/credentials.json'))
    ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),handler(application)).serve_forever()
