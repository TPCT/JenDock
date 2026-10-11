"""Local HTTP project creation against real GitHub metadata; no upstream writes.

Uses the current gh credential in memory, encrypts it only inside a temporary
registry, and removes that registry/key at exit. Never touches preview state.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))
from cryptography.fernet import Fernet
from server import Application, handler
from store import connect
import integrations


def main():
    output = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True, timeout=10)
    if output.returncode: raise RuntimeError('An authenticated local GitHub CLI is required.')
    credential = output.stdout.strip(); output = None
    report = {}
    with tempfile.TemporaryDirectory(prefix='deploy-monitor-project-create-') as directory:
        root = Path(directory); key = root/'key'; key.write_bytes(Fernet.generate_key()); key.chmod(0o600)
        config = root/'projects.json'; config.write_text('{}')
        credentials = root/'credentials.json'; salt = 'ab'*16
        credentials.write_text(json.dumps({'username':'fixture-owner','salt':salt,
            'password_hash':hashlib.pbkdf2_hmac('sha256',b'local-test-only',bytes.fromhex(salt),260000).hex(),'webhook_secret':'local-fixture-only'}))
        database = root/'db'; db = connect(database)
        integrations.save_connection(db,'github',0,credential,key,actor='fixture-owner'); credential = None; db.close()
        app = Application(config,database,credentials); app.integration_key=str(key)
        server = ThreadingHTTPServer(('127.0.0.1',0),handler(app)); app.origin='http://127.0.0.1:'+str(server.server_address[1]); app.secure=False;app.cookie_name='monitor_session'
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        cookie='';csrf=''
        def post(path,body, *, use_csrf=True):
            headers={'Origin':app.origin,'Content-Type':'application/json','Cookie':cookie}
            if use_csrf:headers['X-CSRF-Token']=csrf
            request=urllib.request.Request(app.origin+path,data=json.dumps(body).encode(),headers=headers)
            try:
                with urllib.request.urlopen(request,timeout=30) as response:
                    return response.status,json.load(response),response.headers.get('Set-Cookie','')
            except urllib.error.HTTPError as error:
                try:return error.code,json.load(error),''
                finally:error.close()
        try:
            code,login,set_cookie=post('/api/login',{'username':'fixture-owner','password':'local-test-only'})
            assert code==200;cookie=set_cookie.split(';',1)[0];csrf=login['csrf']
            body={'action':'registry-create','project':'verified-local-project','values':{'repository':'TPCT/deploy-monitor','repository_id':1413328469},
                  'request_id':'verified-local-http-request','actor':'forged-client-actor'}
            assert post('/api/action',body,use_csrf=False)[0]==403
            code,created,_=post('/api/action',body);assert code==200,created
            assert created['project']['repository_id']==1413328469
            code,repeated,_=post('/api/action',body);assert code==200 and repeated==created
            changed=dict(body,values={'repository':'TPCT/deploy-monitor','label':'Changed'})
            assert post('/api/action',changed)[0]==409
            db=connect(database)
            assert db.execute('SELECT count(*) FROM registry_projects').fetchone()[0]==1
            assert db.execute('SELECT count(*) FROM registry_environments').fetchone()[0]==2
            assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
            row=db.execute('SELECT subject,actor_kind FROM application_operations').fetchone()
            assert row['subject']=='fixture-owner' and row['actor_kind']=='console'
            integrations.disable_connection(db,'github',1);db.close()
            code,replayed,_=post('/api/action',body);assert code==200 and replayed==created
            code,status,_=post('/api/action',{'action':'operation-status','operation_id':created['operation']['operation_id']})
            assert code==200 and status['operation']==created['operation']
            for environment in created['project']['environments'].values():
                assert environment['state']=='not-provisioned' and environment['recipe_revision'] is None and environment['automatic']
            report={'real_github_repository_verified':True,'local_http_creation':True,'csrf_enforced':True,
                    'replay_after_database_reopen_and_connection_disable':True,'changed_payload_rejected':True,
                    'trusted_actor_attribution':True,'one_project_two_inactive_environments':True,'no_jobs_created':True,
                    'operation_status_persisted':True,'external_writes':False}
        finally:server.shutdown();server.server_close();thread.join()
    report['temporary_credentials_and_database_removed']=not root.exists()
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
