"""Authenticated management flows, persisted branch routing, and race boundaries."""
import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from test_receiver import ReceiverTests
from server import handler,Application
from store import connect,snapshot,set_status,valid_branch


class ManagementTests(ReceiverTests):
    def setUp(self):
        super().setUp()
        policy=json.loads(self.config.read_text());policy['test']['recipe']={'tests':[{}]};self.config.write_text(json.dumps(policy))
        db=connect(self.app.database)
        snapshot(db,'branches:test',{'branches':{'main':'a'*40,'feature/login':'b'*40},'error':False});db.close()
        code,response,token=self.app.login({'username':'admin','password':'local-test-only'},'fixture')
        self.token=token;self.session=self.app.session(self.app.cookie_name+'='+token)
        self.body={'action':'build','project':'test','branch':'feature/login','sha':'b'*40,'mode':'validate','request_id':'test-request-123456'}

    def test_manual_branch_build_persists_and_is_idempotent(self):
        code,result=self.app.action(self.session,self.body);self.assertEqual(code,202)
        code,retry=self.app.action(self.session,self.body);self.assertEqual(code,200);self.assertEqual(result['job_id'],retry['job_id'])
        reopened=Application(self.config,self.app.database,self.credentials)
        job=reopened.state()['jobs'][0]
        self.assertEqual((job['branch'],job['sha'],job['status'],job['actor']),('feature/login','b'*40,'validation-queued','admin'))

    def test_branch_routing_changes_without_building(self):
        result=self.app.action(self.session,{'action':'settings','project':'test','branch':'feature/login','automatic':True})
        self.assertEqual(result[0],200);self.assertEqual(self.app.state()['jobs'],[])
        self.assertIn('ignored',self.deliver()[1])
        self.deliver(dict(self.payload,ref='refs/heads/feature/login',after='b'*40))
        self.assertEqual(self.app.state()['jobs'][0]['branch'],'feature/login')

    def test_legacy_enabled_flag_does_not_gate_owner_deployment(self):
        policy=json.loads(self.config.read_text());policy['test']['enabled']=False;self.config.write_text(json.dumps(policy))
        self.assertEqual(self.app.action(self.session,dict(self.body,mode='deploy'))[0],202)

    def test_missing_branch_stale_observation_and_changed_sha_are_rejected(self):
        for change in ({'branch':'missing'},{'branch':'--upload-pack=bad'},{'sha':'c'*40}):
            self.assertIn(self.app.action(self.session,dict(self.body,**change))[0],(400,409))
        db=connect(self.app.database);db.execute('UPDATE snapshots SET updated=?',(time.time()-601,));db.close()
        self.assertEqual(self.app.action(self.session,self.body)[0],409)
        self.assertEqual(self.app.state()['jobs'],[])

    def test_concurrent_manual_requests_do_not_duplicate_project_work(self):
        results=[]
        def start(n): results.append(self.app.action(self.session,dict(self.body,request_id='concurrent-request-'+str(n))))
        threads=[threading.Thread(target=start,args=(n,)) for n in range(6)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(len(results),6);self.assertEqual(sum(x[0]==202 for x in results),1)
        self.assertEqual(len(self.app.state()['jobs']),1)

    def test_retry_cancel_and_audit(self):
        _,result=self.app.action(self.session,self.body);job_id=result['job_id']
        self.assertEqual(self.app.action(self.session,{'action':'cancel','project':'test','job_id':job_id})[0],200)
        _,retry=self.app.action(self.session,dict(self.body,action='retry',job_id=job_id,request_id='retry-request-123456'))
        job=self.app.state()['jobs'][0];self.assertEqual(job['retry_of'],job_id)
        db=connect(self.app.database);set_status(db,retry['job_id'],'building','بدأ البناء');db.close()
        self.assertEqual(self.app.action(self.session,{'action':'cancel','project':'test','job_id':retry['job_id']})[0],409)
        self.assertTrue(self.app.state()['events']);self.assertTrue(self.app.state()['audit'])

    def test_logout_and_idle_expiry_revoke_sessions(self):
        self.assertEqual(self.app.action(self.session,{'action':'logout'})[0],200)
        self.assertIsNone(self.app.session(self.app.cookie_name+'='+self.token))
        _,_,token=self.app.login({'username':'admin','password':'local-test-only'},'fixture')
        db=connect(self.app.database);db.execute('UPDATE sessions SET touched=?',(time.time()-3601,));db.close()
        self.assertIsNone(self.app.session(self.app.cookie_name+'='+token))

    def test_http_login_csrf_origin_cookie_and_no_basic_bypass(self):
        http=ThreadingHTTPServer(('127.0.0.1',0),handler(self.app));thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(http.server_port)
        def request(path,body=None,**headers):
            req=Request(base+path,data=json.dumps(body).encode() if body else None,headers={'Content-Type':'application/json',**headers})
            try:
                with urlopen(req) as r:return r.status,dict(r.headers),json.load(r) if 'application/json' in r.headers.get('Content-Type','') else r.read()
            except HTTPError as e:
                with e:return e.code,dict(e.headers),json.load(e)
        try:
            self.assertEqual(request('/')[0],200)
            self.assertEqual(request('/api/state')[0],401)
            creds={'username':'admin','password':'local-test-only'}
            self.assertEqual(request('/api/login',creds,Origin='https://evil.invalid')[0],403)
            code,headers,value=request('/api/login',creds,Origin=self.app.origin)
            self.assertEqual(code,200);cookie=headers['Set-Cookie'];self.assertIn('HttpOnly',cookie);self.assertIn('Secure',cookie);self.assertIn('SameSite=Strict',cookie)
            cookie=cookie.split(';')[0]
            self.assertEqual(request('/api/state',Cookie=cookie)[0],200)
            self.assertEqual(request('/api/action',self.body,Origin=self.app.origin,Cookie=cookie)[0],403)
            self.assertEqual(request('/api/action',self.body,Origin='https://evil.invalid',Cookie=cookie,**{'X-CSRF-Token':value['csrf']})[0],403)
            self.assertEqual(request('/api/action',self.body,Origin=self.app.origin,Cookie=cookie,**{'X-CSRF-Token':value['csrf']})[0],202)
        finally:http.shutdown();http.server_close();thread.join()

    def test_manual_deploy_from_waiting_and_validated_run(self):
        db=connect(self.app.database)
        from store import enqueue
        old,_=enqueue(db,'old-push','test','b'*40,False,'feature/login')
        db.close()
        code,result=self.app.action(self.session,dict(self.body,action='retry',mode='deploy',job_id=old))
        self.assertEqual(code,202)
        job=self.app.state()['jobs'][0]
        self.assertEqual((job['status'],job['retry_of']),('queued',old))

    def test_auto_default_and_persisted_opt_out(self):
        policy=json.loads(self.config.read_text());policy['test']['enabled']=False;self.config.write_text(json.dumps(policy))
        self.assertTrue(self.app.state()['projects']['test']['automatic'])
        code,_=self.app.action(self.session,{'action':'settings','project':'test','branch':'main','automatic':False})
        self.assertEqual(code,200)
        self.deliver()
        self.assertEqual(self.app.state()['jobs'][0]['status'],'auto-deploy-disabled')
        self.assertFalse(Application(self.config,self.app.database,self.credentials).state()['projects']['test']['automatic'])

    def test_branch_validation(self):
        for value in ('main','feature/a','fix-123','release/2026'):
            self.assertTrue(valid_branch(value))
        for value in ('-main','a..b','a\nb','a\\b','a@{b','a b','x.lock','/main','a*'):
            self.assertFalse(valid_branch(value),value)

    def test_live_stream_authentication_state_updates_and_logout(self):
        from http.client import HTTPConnection
        http=ThreadingHTTPServer(('127.0.0.1',0),handler(self.app))
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        client=HTTPConnection('127.0.0.1',http.server_port,timeout=8)
        try:
            client.request('GET','/api/events')
            response=client.getresponse();self.assertEqual(response.status,401);response.read()
            client.request('GET','/api/events',headers={'Cookie':self.app.cookie_name+'='+self.token})
            response=client.getresponse();self.assertEqual(response.status,200)
            self.assertEqual(response.getheader('Content-Type'),'text/event-stream')
            self.assertEqual(response.fp.readline().strip(),b'event: state')
            initial=json.loads(response.fp.readline().removeprefix(b'data: '));response.fp.readline()
            self.assertIn('projects',initial)
            code,result=self.app.action(self.session,self.body);self.assertEqual(code,202)
            while True:
                line=response.fp.readline().strip()
                if line==b'event: state':
                    updated=json.loads(response.fp.readline().removeprefix(b'data: '));response.fp.readline()
                    if any(j['id']==result['job_id'] for j in updated['jobs']):break
            self.app.action(self.session,{'action':'logout'})
            while response.fp.readline().strip()!=b'event: expired':pass
            response.close()
        finally:client.close();http.shutdown();http.server_close();thread.join()
