"""Actual HTTP/session -> Unix peer-authenticated worker -> SQLite config flow."""
import hashlib
import json
import os
from pathlib import Path
import socket
import socketserver
import uuid
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch
from cryptography.fernet import Fernet
import control
import database_rpc
import database_bindings as bindings
from database_auth import ConsoleAuthority
from application_operations import OperationError
from server import Application, handler
import test_managed_services as fixtures


class DatabaseControlFlowTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ManagedServiceTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        root=Path(self.f.tmp.name);self.credentials=root/'console.json';self.key=root/'key'
        self.credentials.write_text(json.dumps({'username':'owner','salt':'ab'*16,'password_hash':hashlib.pbkdf2_hmac('sha256',b'fixture-login',bytes.fromhex('ab'*16),260000).hex()}))
        self.key.write_bytes(Fernet.generate_key());self.key.chmod(0o600)
        self.socket=str(root/'control.sock')
        for patcher in (patch.dict(os.environ,{'CREDENTIALS_FILE':str(self.credentials),'DATABASE_INSPECTION_KEY_FILE':str(self.key),'DATABASE_RECEIVER_UID':str(os.getuid()),'DATABASE_WORKER_UID':str(os.getuid())}),patch.object(control,'DATABASE',self.f.path),patch.object(database_rpc.request,'__defaults__',(self.socket,))):
            patcher.start();self.addCleanup(patcher.stop)
        self.unix=socketserver.ThreadingUnixStreamServer(self.socket,control.Handler)
        self.unix.daemon_threads=True
        self.ut=threading.Thread(target=self.unix.serve_forever,daemon=True);self.ut.start()
        self.addCleanup(self.close_unix)
        config=root/'projects.json';config.write_text('{}')
        app=Application(config,self.f.path,self.credentials);app.secure=False;app.cookie_name='monitor_session'
        self.http=ThreadingHTTPServer(('127.0.0.1',0),handler(app));self.origin=f'http://127.0.0.1:{self.http.server_port}';app.origin=self.origin
        self.ht=threading.Thread(target=self.http.serve_forever,daemon=True);self.ht.start();self.addCleanup(self.close_http)
        result,headers=self.post('/_deploy/api/login',{'username':'owner','password':'fixture-login'},authenticated=False)
        self.cookie=headers['Set-Cookie'].split(';',1)[0];self.csrf=result['csrf']
        self.digest=hashlib.sha256(self.cookie.split('=',1)[1].encode()).hexdigest()
        bindings.initialize(self.f.db)

    def close_unix(self):self.unix.shutdown();self.unix.server_close();self.ut.join()
    def close_http(self):self.http.shutdown();self.http.server_close();self.ht.join()
    def post(self,path,body,authenticated=True,csrf=None):
        headers={'Origin':self.origin,'Content-Type':'application/json'}
        if authenticated:headers.update(Cookie=self.cookie,**{'X-CSRF-Token':self.csrf if csrf is None else csrf})
        with urlopen(Request(self.origin+path,data=json.dumps(body).encode(),headers=headers),timeout=5) as response:return json.load(response),response.headers
    def action(self,action,**values):
        if action.endswith('-save'):values.setdefault('request_id',uuid.uuid4().hex)
        return self.post('/_deploy/api/action',dict(action=action,project='app',environment='development',**values))[0]
    def save(self):return self.action('database-credential-save',values={'username':'inspect_user','password':'private-db-password'},expected_revision=0)['credential']
    def payload(self):return {'action':'database-credential-save','request_id':uuid.uuid4().hex,'project':'app','environment':'development','authentication':{'kind':'console','session_digest':self.digest},'values':{'username':'inspect_user','password':'private-db-password'},'expected_revision':0}

    def test_migration_configuration_over_real_http_and_unix(self):
        self.assertIsNone(self.action('database-migration-binding-current')['binding'])
        values={'username':'migrator','password':'private-migration-password'}
        first=self.action('database-migration-credential-save',values=values,expected_revision=0,request_id='migration-http-001')
        self.assertTrue(first['state_requires_reload'])
        self.assertEqual(first,self.action('database-migration-credential-save',values=values,expected_revision=0,request_id='migration-http-001'))
        credential=first['credential']
        self.assertEqual(self.action('database-migration-credential-get',credential_id=credential['id'])['credential'],credential)
        self.assertEqual(self.action('database-migration-credentials')['credentials'],[credential])
        self.assertEqual(self.action('database-credentials')['credentials'],[])
        definition={'engine':'postgresql','adapter_version':1,'ownership':{'kind':'adopted-compose','reference':'db','revision':1},'database_name':'app','database_port':5432,'secret_id':credential['id'],'secret_revision':1,'framework':'laravel','grammar':'laravel12-stock-postgresql-v1','migration_paths':['database/migrations'],'ledger_schema':'public'}
        saved=self.action('database-migration-binding-save',values=definition,expected_revision=0)
        self.assertTrue(saved['state_requires_reload']);binding=saved['binding']
        self.assertEqual(binding['status'],'configured');self.assertFalse(binding['verified'])
        self.assertEqual(self.action('database-migration-binding-current')['binding'],binding)
        self.assertEqual(self.action('database-migration-binding-get',binding_id=binding['id'])['binding'],binding)
        with self.assertRaises(HTTPError) as wrong_environment:
            self.post('/_deploy/api/action',{'action':'database-migration-binding-get','project':'app','environment':'production','binding_id':binding['id']})
        wrong_environment.exception.close()
        self.action('database-migration-credential-revoke',credential_id=credential['id'],expected_revision=1)
        self.assertEqual(self.action('database-migration-binding-current')['binding']['status'],'credential-stale')
        disabled=self.action('database-migration-binding-disable',binding_id=binding['id'],expected_revision=1)['binding']
        self.assertEqual(disabled['status'],'disabled')
        self.assertNotIn(values['password'],json.dumps([first,saved,disabled]))

    def test_migration_save_failure_outcome_and_closed_fields(self):
        for _ in range(2):
            with self.assertRaises(HTTPError) as caught:
                self.action('database-migration-credential-save',values={'username':'migrator','password':'private'},expected_revision=1,request_id='migration-rejected-001')
            self.assertEqual(json.load(caught.exception)['outcome'],'not_committed')
            caught.exception.close()
        self.assertEqual(self.action('database-migration-credentials')['credentials'],[])
        with self.assertRaises(HTTPError) as invalid:self.action('database-migration-binding-current',unexpected=True)
        invalid.exception.close()
        from database_control import execute
        with self.assertRaises(ValueError):
            execute(self.f.db,{'action':'database-migration-binding-current','project':'app','environment':'development','authentication':{'kind':'mcp','token':'unused'}})

    def test_credential_lookup_is_current_scoped_and_secret_free(self):
        credential=self.save()
        self.assertEqual(self.action('database-credential-get',credential_id=credential['id'])['credential'],credential)
        self.action('database-credential-revoke',credential_id=credential['id'],expected_revision=1)
        current=self.action('database-credential-get',credential_id=credential['id'])['credential']
        self.assertFalse(current['enabled']);self.assertEqual(current['revision'],2)
        self.assertNotIn('private-db-password',json.dumps(current))
        with self.assertRaises(HTTPError) as caught:
            self.post('/_deploy/api/action',{'action':'database-credential-get','project':'app','environment':'production','credential_id':credential['id']})
        with caught.exception as response:self.assertEqual(response.code,409)

    def test_http_socket_configuration_persists_unverified_without_secret_echo(self):
        credential=self.save();self.assertEqual(self.action('database-credentials')['credentials'],[credential])
        definition={'engine':'postgresql','adapter_version':1,'ownership':{'kind':'adopted-compose','reference':'web','revision':1},'database_name':'app','database_port':5432,'secret_id':credential['id'],'secret_revision':1,'allowed_schemas':['public'],'policy_revision':1}
        binding=self.action('database-binding-save',values=definition,expected_revision=0)['binding']
        self.assertEqual(binding['status'],'unverified');self.assertFalse(binding['verified'])
        self.assertEqual(self.action('database-bindings')['bindings'],[binding])
        self.assertEqual(self.action('database-binding-get',binding_id=binding['id'])['binding'],binding)
        revoked=self.action('database-credential-revoke',credential_id=credential['id'],expected_revision=1)['credential']
        self.assertFalse(revoked['enabled']);self.assertEqual(revoked['revision'],2)
        stale=self.action('database-bindings')['bindings'][0];self.assertEqual(stale['status'],'credential-stale')
        events=[dict(row) for row in self.f.db.execute('SELECT * FROM database_binding_events')]
        public=json.dumps([credential,binding,revoked,stale,events])
        self.assertNotIn('private-db-password',public);self.assertNotIn('inspect_user',public)
        self.assertNotIn('ciphertext',public)
        self.assertEqual(self.f.db.execute('SELECT COUNT(*) FROM database_bindings').fetchone()[0],1)

    def test_forged_auth_csrf_expired_and_missing_session_cannot_mutate(self):
        for key,value in (('authentication',{'kind':'console','session_digest':'a'*64}),('actor','owner'),('terminal_owner','owner')):
            with self.assertRaises(HTTPError) as error:self.action('database-credential-save',values={'username':'x','password':'y'},expected_revision=0,**{key:value})
            self.assertEqual(error.exception.code,400);error.exception.close()
        with self.assertRaises(HTTPError) as error:self.post('/_deploy/api/action',self.payload(),csrf='wrong')
        self.assertEqual(error.exception.code,403);error.exception.close()
        self.f.db.execute('UPDATE sessions SET touched=?',(time.time()-3601,))
        with self.assertRaises(HTTPError) as error:self.action('database-bindings')
        self.assertEqual(error.exception.code,401);error.exception.close()
        with self.assertRaises(HTTPError) as error:self.post('/_deploy/api/action',self.payload(),authenticated=False)
        self.assertEqual(error.exception.code,401);error.exception.close()
        self.assertEqual(self.f.db.execute('SELECT COUNT(*) FROM database_credentials').fetchone()[0],0)

    def test_actual_wrong_peer_uid_is_rejected_before_mutation(self):
        with patch.dict(os.environ,{'DATABASE_RECEIVER_UID':str(os.getuid()+1)}):
            response=database_rpc.request(self.payload())
        self.assertFalse(response['ok']);self.assertIn('peer',response['error'])
        self.assertEqual(self.f.db.execute('SELECT COUNT(*) FROM database_credentials').fetchone()[0],0)
        with patch.dict(os.environ,{'DATABASE_WORKER_UID':str(os.getuid()+1)}),self.assertRaises(ValueError):database_rpc.request(self.payload())
        self.assertEqual(self.f.db.execute('SELECT COUNT(*) FROM database_credentials').fetchone()[0],0)

    def test_real_owner_change_at_transaction_revalidation_rolls_back(self):
        authority=ConsoleAuthority(self.f.db,self.payload()['authentication'],self.credentials)
        observed=[]
        def recheck():
            observed.append(self.f.db.in_transaction)
            if self.f.db.in_transaction:self.credentials.write_text('{"username":"replacement"}')
            return authority.recheck()
        with self.assertRaises(OperationError):
            bindings.save_credential(self.f.db,authority.principal,'app','development',{'username':'inspect_user','password':'private-db-password'},expected_revision=0,key_path=self.key,revalidate=recheck)
        self.assertIn(True,observed)
        self.assertEqual(self.f.db.execute('SELECT COUNT(*) FROM database_credentials').fetchone()[0],0)
        self.assertEqual(self.f.db.execute('SELECT COUNT(*) FROM database_binding_events').fetchone()[0],0)
        response=database_rpc.request(self.payload());self.assertFalse(response['ok']);self.assertEqual(response['status'],403)

    def test_incomplete_frame_cannot_commit(self):
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(3);client.connect(self.socket)
            client.sendall(json.dumps(self.payload()).encode())
            client.shutdown(socket.SHUT_WR)
            response=json.loads(client.recv(4096))
        self.assertFalse(response['ok'])
        self.assertEqual(self.f.db.execute('SELECT count(*) FROM database_credentials').fetchone()[0],0)
        self.assertEqual(self.f.db.execute('SELECT count(*) FROM database_binding_events').fetchone()[0],0)

    def test_catalog_pagination_preserves_all_records(self):
        ids={self.save()['id'] for _ in range(3)}
        first=self.action('database-credentials',limit=2)
        second=self.action('database-credentials',limit=2,cursor=first['next_cursor'])
        self.assertEqual(ids,{r['id'] for r in first['credentials']+second['credentials']})
        self.assertIsNone(second['next_cursor'])

    def test_oversized_catalog_has_bounded_error_and_smaller_page_recovers(self):
        credential=self.save()
        definition={'engine':'postgresql','adapter_version':1,'ownership':{'kind':'adopted-compose','reference':'web','revision':1},'database_name':'app','database_port':5432,'secret_id':credential['id'],'secret_revision':1,'allowed_schemas':['public'],'policy_revision':1,'row_columns':{'public':{f'table_{n}':[f'column_{k}_'+('x'*100) for k in range(20)] for n in range(10)}}}
        for _ in range(13):self.action('database-binding-save',values=definition,expected_revision=0)
        with self.assertRaises(HTTPError) as caught:self.action('database-bindings')
        with caught.exception as response:
            self.assertEqual(response.code,413)
            self.assertLess(len(response.read()),512)
        self.assertEqual(len(self.action('database-bindings',limit=1)['bindings']),1)

    def test_http_create_replay_returns_original_without_duplicate(self):
        values={'username':'inspect_user','password':'private-db-password'}
        key=uuid.uuid4().hex
        first=self.action('database-credential-save',values=values,expected_revision=0,request_id=key)
        second=self.action('database-credential-save',values=values,expected_revision=0,request_id=key)
        self.assertEqual(first,second)
        self.assertEqual(len(self.action('database-credentials')['credentials']),1)
        with self.assertRaises(HTTPError) as caught:
            self.action('database-credential-save',values={**values,'password':'changed'},expected_revision=0,request_id=key)
        with caught.exception as response:self.assertEqual(response.code,409)

    def test_recovery_journal_blocks_runtime_but_keeps_revocation_available(self):
        credential=self.save()
        journal=Path(self.f.tmp.name)/'pending-environment.json'
        journal.write_text('{}')
        with patch.object(control,'JOURNAL',journal),patch.object(control,'database_inspector') as factory:
            for action in ('database-options','database-preview','database-accept','database-read'):
                with self.assertRaises(HTTPError) as caught:self.action(action)
                with caught.exception as response:
                    self.assertEqual(response.code,409)
                    self.assertIn('recovery',json.load(response)['error'])
            factory.assert_not_called()
            result=self.action('database-credential-revoke',credential_id=credential['id'],expected_revision=1)
            self.assertFalse(result['credential']['enabled'])
        journal.unlink()
        deployment=Path(self.f.tmp.name)/'transaction.json';deployment.write_text('{}')
        with patch.dict(os.environ,{'STATE_DIR':self.f.tmp.name}),patch.object(control,'database_inspector') as factory:
            with self.assertRaises(HTTPError) as caught:self.action('database-preview')
            with caught.exception as response:self.assertEqual(response.code,409)
            factory.assert_not_called()

    def test_http_durable_rejection_outcome_and_committed_conflict_boundary(self):
        values={'username':'inspector','password':'private-value'}
        for _ in range(2):
            with self.assertRaises(HTTPError) as caught:
                self.action('database-credential-save',values=values,expected_revision=1,request_id='http-rejected-001')
            with caught.exception as response:
                body=json.load(response);self.assertEqual(response.code,409)
            self.assertEqual(body['outcome'],'not_committed')
            self.assertNotIn('private-value',json.dumps(body))
        self.assertEqual(self.f.db.execute('SELECT COUNT(*) FROM database_credentials').fetchone()[0],0)
        first=self.action('database-credential-save',values=values,expected_revision=0,request_id='http-committed-001')
        with self.assertRaises(HTTPError) as caught:
            self.action('database-credential-save',values=values,expected_revision=1,request_id='http-committed-001')
        with caught.exception as response:body=json.load(response)
        self.assertNotIn('outcome',body)
        self.assertEqual(self.action('database-credential-save',values=values,expected_revision=0,request_id='http-committed-001'),first)

    def test_http_postcommit_authorization_failure_never_claims_noncommit(self):
        original=bindings._remember;credentials=self.credentials.read_text()
        def remember(*args,**kwargs):
            result=original(*args,**kwargs)
            self.credentials.write_text('{"username":"replacement"}')
            return result
        for revision in (0,1):
            self.credentials.write_text(credentials)
            with patch.object(bindings,'_remember',side_effect=remember):
                with self.assertRaises(HTTPError) as caught:
                    self.action('database-credential-save',values={'username':'inspector','password':'private-value'},expected_revision=revision,request_id='http-auth-after-'+str(revision))
            with caught.exception as response:
                body=json.load(response);self.assertEqual(response.code,403)
            self.assertNotIn('outcome',body)
        self.assertEqual(self.f.db.execute('SELECT COUNT(*) FROM database_credentials').fetchone()[0],1)
        self.assertEqual(self.f.db.execute('SELECT COUNT(*) FROM database_save_requests').fetchone()[0],2)
