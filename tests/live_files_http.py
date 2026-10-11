"""Synthetic local HTTP -> Unix worker-in-Docker -> descriptor file browsing proof.

Optional --hold keeps the authenticated UI fixture for at most ten minutes; create
<fixture-root>/release or interrupt to clean up. No deployed database is used.
"""
import argparse
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import adoption
import compose_review
import registry
from server import Application, handler
from store import connect, snapshot
from live_service_creation import peers

PROJECT='deploy-monitor'
SERVICE='files-check'
NAME=PROJECT+'-'+SERVICE+'-1'
WORKER_NAME=PROJECT+'-files-reader-1'
IMAGE='deploy-monitor-worker:local-current'


def run(*args):
    result=subprocess.run(args,capture_output=True,text=True,timeout=120,env={**os.environ,'COMPOSE_IGNORE_ORPHANS':'true'})
    if result.returncode:raise RuntimeError('Fixture command failed: '+args[0]+' ('+str(result.returncode)+')')
    return result.stdout


def main():
    hold=argparse.ArgumentParser();hold.add_argument('--hold',action='store_true');options=hold.parse_args()
    docker=shutil.which('docker')
    image=json.loads(run(docker,'image','inspect',IMAGE))[0]
    compose=next(row['Path'] for row in json.loads(run(docker,'info','--format','{{json .ClientInfo.Plugins}}')) if row['Name']=='compose')
    for name in (NAME,WORKER_NAME):
        assert not run(docker,'ps','-aq','--filter','name=^/'+name+'$').strip(),'Existing fixture name must not be reused'
    baseline=peers(docker);tracked=set();built=None
    report={'project':PROJECT,'scope':'temporary synthetic local database; no deployed application data','worker_image':image['Id']}
    runtime=ROOT/'.runtime';runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='files-http-',dir=runtime) as directory:
        root=Path(directory);root.chmod(0o777)
        mounted=root/'mounted';mounted.mkdir();mounted.chmod(0o755);(mounted/'mounted.txt').write_text('persistent fixture sentinel')
        build=root/'build';build.mkdir();(build/'image.txt').write_text('image fixture sentinel')
        (build/'Dockerfile').write_text('FROM python:3.13-slim\nWORKDIR /application\nCOPY image.txt image.txt\nRUN chmod 777 /application && printf "fixture-hidden" > /application/.env && ln /application/.env /application/alias.txt && ln -s /etc/passwd /application/link.txt && printf \'password=fixture-ini-secret\' > /application/config.ini && head -c 70000 /dev/zero > /application/large.txt && printf "private-mode" > /application/private.txt && chown 1001:1001 /application/private.txt && chmod 600 /application/private.txt\nUSER 65534:65534\n')
        config=root/'projects.json';path=root/'compose.json';credentials=root/'credentials.json';dbpath=root/'state.sqlite';sockpath=root/'control.sock'
        credentials.write_text(json.dumps({'username':'admin','salt':'ab'*16,'password_hash':hashlib.pbkdf2_hmac('sha256',b'local-test-only',bytes.fromhex('ab'*16),260000).hex(),'webhook_secret':'fixture-only'}))
        http=None;thread=None;db=None
        try:
            built=run(docker,'build','--pull=false','--network=none','-q',str(build)).strip()
            path.write_text(json.dumps({'services':{SERVICE:{'image':built,'network_mode':'none','cap_drop':['ALL'],
                'security_opt':['no-new-privileges:true'],'user':'65534:65534','stop_grace_period':'1s',
                'command':['python','-c',"from pathlib import Path;import time;Path('runtime.txt').write_text('writable fixture sentinel');Path('paged').mkdir();[Path('paged/file-%03d.txt'%i).write_text('pagination fixture '+str(i)) for i in range(105)];time.sleep(3600)"],
                'volumes':[{'type':'bind','source':str(mounted),'target':'/application/data','read_only':True}]}}}))
            recipe={'compose_project':PROJECT,'compose_files':[str(path)],'services':[SERVICE]}
            configuration={PROJECT:{'repository':'TPCT/deploy-monitor','repository_id':12,'branch':'main','container_prefixes':[PROJECT+'-'+SERVICE+'-'],'recipe':recipe}}
            config.write_text(json.dumps(configuration))
            run(compose,'-p',PROJECT,'-f',str(path),'up','-d','--no-deps','--no-build','--pull','never',SERVICE)
            cid=run(compose,'-p',PROJECT,'-f',str(path),'ps','-aq',SERVICE).strip();tracked.add(cid)
            for _ in range(100):
                actual=json.loads(run(docker,'inspect',cid))[0]
                if actual['State']['Running']:break
                time.sleep(.05)
            db=connect(dbpath);registry.import_legacy(db,configuration,[])
            def inventory_command(argv):
                if argv[1]=='ps':argv=[*argv,'--filter','label=com.docker.compose.project='+PROJECT,'--filter','label=com.docker.compose.service='+SERVICE]
                elif argv[1]!='inspect':raise AssertionError('Unexpected inventory probe')
                return run(*argv)
            inventory=adoption.collect(docker,inventory_command)
            resolver=lambda rec,svc,observed:compose_review.review(rec,svc,observed,root,lambda argv:run(*argv),compose)
            preview=adoption.preview(db,PROJECT,'development',inventory,resolver=resolver)
            adoption.adopt(db,PROJECT,'development',preview['fingerprint'],inventory,'local-file-fixture',resolver=resolver)
            labels=actual['Config']['Labels']
            snapshot(db,'containers',[{'ID':cid,'Names':NAME,'State':'running','Image':actual['Config']['Image'],
                'ComposeProject':PROJECT,'ComposeService':SERVICE,'ComposeConfigFiles':labels['com.docker.compose.project.config_files']}])
            snapshot(db,'worker',{'state':'running'})
            for file in root.glob('state.sqlite*'):file.chmod(0o666)
            code="import os,time;from pathlib import Path;import control;os.umask(0);r=Path(os.environ['FIXTURE_ROOT']);control.ROOT=r;control.CONFIG=str(r/'projects.json');control.DATABASE=str(r/'state.sqlite');control.JOURNAL=r/'journal.json';control.SOCKET=str(r/'control.sock');control.start();os.chmod(control.SOCKET,0o666);time.sleep(3600)"
            wid=run(docker,'run','-d','--name',WORKER_NAME,'--label','com.docker.compose.project='+PROJECT,
                '--label','com.docker.compose.service=files-reader','--network=none','--cap-drop=ALL','--cap-add=SYS_PTRACE',
                '--security-opt=no-new-privileges','--read-only','--tmpfs','/tmp:rw,nosuid,noexec,size=16m',
                '-v','/proc:/host/proc:ro','-v','/var/run/docker.sock:/var/run/docker.sock',
                '-v',docker+':/usr/local/bin/docker:ro','-v',compose+':/usr/local/bin/docker-compose:ro',
                '-v','/lib64/ld-linux-x86-64.so.2:/lib64/ld-linux-x86-64.so.2:ro',
                '-v','/usr/lib/x86_64-linux-gnu/libc.so.6:/host-cli-libs/libc.so.6:ro','-e','LD_LIBRARY_PATH=/host-cli-libs',
                '-v',str(root)+':'+str(root)+':rw','-e','FIXTURE_ROOT='+str(root),'-e','CONTAINER_FILES_PROC_ROOT=/host/proc',
                IMAGE,'python','-c',code).strip();tracked.add(wid)
            for _ in range(200):
                if sockpath.exists():break
                time.sleep(.05)
            assert sockpath.exists(),'Worker socket was not created'
            worker_record=json.loads(run(docker,'inspect',wid))[0]
            profile=worker_record['HostConfig']
            assert any(m['Destination']=='/host/proc' and m['RW'] is False for m in worker_record['Mounts'])
            assert any('no-new-privileges' in value for value in profile['SecurityOpt'])
            assert actual['Config']['User']=='65534:65534'
            assert [value.removeprefix('CAP_') for value in profile['CapAdd']]==['SYS_PTRACE'] and profile['CapDrop']==['ALL'] and profile['ReadonlyRootfs']
            app=Application(config,dbpath,credentials);app.web_root=ROOT/'frontend'/'dist';http=ThreadingHTTPServer(('127.0.0.1',0),handler(app))
            app.origin='http://127.0.0.1:'+str(http.server_port);app.secure=False;app.cookie_name='monitor_session'
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            original_socket=socket.socket
            class LocalSocket(original_socket):
                def connect(self,address):
                    if self.family==socket.AF_UNIX and address=='/state/control.sock':address=str(sockpath)
                    return super().connect(address)
            def post(endpoint,body,cookie='',csrf=''):
                request=Request(app.origin+'/_deploy/api/'+endpoint,data=json.dumps(body).encode(),headers={
                    'Content-Type':'application/json','Origin':app.origin,'Cookie':cookie,'X-CSRF-Token':csrf})
                try:
                    with urlopen(request,timeout=30) as response:return response.status,json.load(response),response.headers.get('Set-Cookie','').split(';')[0]
                except HTTPError as error:
                    with error:return error.code,json.load(error),''
            with patch('server.socket.socket',LocalSocket):
                body={'action':'container-files-list','project':PROJECT,'container':NAME,'container_id':cid}
                assert post('action',body)[0]==401
                code,session,cookie=post('login',{'username':'admin','password':'local-test-only'})
                assert code==200 and cookie
                assert post('action',body,cookie,'invalid')[0]==403
                code,listing,_=post('action',body,cookie,session['csrf'])
                assert code==200 and listing['capability']['supported'],listing
                entries={item['name']:item for item in listing['entries']}
                code,page_one,_=post('action',dict(body,path='paged',limit=100),cookie,session['csrf'])
                assert code==200 and len(page_one['entries'])==100 and page_one['next_cursor']
                code,page_two,_=post('action',dict(body,path='paged',limit=100,cursor=page_one['next_cursor']),cookie,session['csrf'])
                assert code==200 and len(page_two['entries'])==5 and page_two['next_cursor'] is None
                report['pagination_http']={'first_page':100,'second_page':5}
                assert entries['image.txt']['storage']=='image',entries
                assert entries['runtime.txt']['storage']=='writable-layer',entries
                for path_value,expected,storage in [('image.txt','image fixture sentinel','image'),('runtime.txt','writable fixture sentinel','writable-layer'),('data/mounted.txt','persistent fixture sentinel','mount')]:
                    code,result,_=post('action',dict(body,action='container-files-preview',path=path_value),cookie,session['csrf'])
                    assert code==200 and result['text']==expected and result['storage']==storage,result
                    assert str(mounted) not in json.dumps(result)
                denied=[]
                for blocked in ('.env','config.ini','link.txt','alias.txt','large.txt','private.txt','../etc/passwd'):
                    code,result,_=post('action',dict(body,action='container-files-preview',path=blocked),cookie,session['csrf'])
                    assert code>=400 and 'text' not in result,(blocked,result)
                    denied.append(blocked)
                code,_,_=post('action',dict(body,container_id='f'*64),cookie,session['csrf']);assert code>=400
                report.update(authenticated_http=True,csrf_enforced=True,worker_inside_docker=True,cross_uid=True,
                    sys_ptrace_only=True,image_writable_mount_verified=True,denied=denied,stale_identity_rejected=True,
                    proc_mount_read_only=True,adopted_environment='development')
                if options.hold:
                    print(json.dumps({'hold_url':app.origin+'/_deploy/','project':PROJECT,'container':NAME,'container_id':cid,
                                      'release_marker':str(root/'release'),'mutation_marker':str(root/'mutate-paged'),'expires_in_seconds':600}),flush=True)
                    deadline=time.monotonic()+600
                    while time.monotonic()<deadline and not (root/'release').exists():
                        if (root/'mutate-paged').exists() and not (root/'mutate-paged.done').exists():
                            # A deliberate test-only mutation, pinned to this fixture's exact ID.
                            run(docker,'exec',cid,'python','-c',"from pathlib import Path;Path('/application/paged/file-added-by-marker.txt').write_text('explicit synthetic pagination mutation')")
                            (root/'mutate-paged.done').write_text('completed')
                            report['explicit_pagination_mutation']=True
                        actual=json.loads(run(docker,'inspect',cid))[0]
                        snapshot(db,'containers',[{'ID':cid,'Names':NAME,'State':actual['State']['Status'],
                            'Image':actual['Config']['Image'],'ComposeProject':PROJECT,'ComposeService':SERVICE,
                            'ComposeConfigFiles':actual['Config']['Labels']['com.docker.compose.project.config_files']}])
                        worker_actual=json.loads(run(docker,'inspect',wid))[0]
                        snapshot(db,'worker',{'state':'running' if worker_actual['State']['Running'] else 'unavailable'})
                        time.sleep(5)
        finally:
            if http:http.shutdown();http.server_close()
            if thread:thread.join(5)
            if db:db.close()
            for identifier in tracked:run(docker,'rm','-f',identifier)
            if built:run(docker,'image','rm',built)
    assert peers(docker)==baseline,'Existing peers changed'
    report.update(cleanup_verified=True,peers_unchanged=True,passed=True)
    (runtime/'files-http-last.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,sort_keys=True),flush=True)


if __name__=='__main__':main()
