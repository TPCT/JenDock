import dataclasses
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import workload_auth as auth
from application_operations import OperationError
from store import connect


class WorkloadAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=connect(Path(self.tmp.name)/'db');self.addCleanup(self.db.close)
        self.path=Path(self.tmp.name)/'credentials.json'
        self.path.write_text(json.dumps({'username':'owner','salt':'never-return','password_hash':'never-return'}))
        self.digest='a'*64;self.envelope={'kind':'console','session_digest':self.digest}
        self.db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(self.digest,'csrf','owner',99000,99900))
        self.clock=patch.object(auth.time,'time',return_value=100000);self.clock.start();self.addCleanup(self.clock.stop)

    def test_actual_session_immutable_principal_and_no_touch(self):
        authority=auth.ConsoleAuthority(self.db,self.envelope,self.path)
        self.assertEqual(authority.principal.scopes,auth.SCOPES)
        self.assertEqual(authority.recheck(),authority.principal)
        self.assertIsNone(authority.principal.credential_id)
        with self.assertRaises(dataclasses.FrozenInstanceError):authority.principal.subject_id='forged'
        with self.assertRaises(dataclasses.FrozenInstanceError):authority.session_digest='b'*64
        self.assertEqual(self.db.execute('SELECT touched FROM sessions').fetchone()[0],99900)
        self.envelope['session_digest']='b'*64
        self.assertEqual(authority().subject_id,'owner')

    def test_rechecks_deleted_expired_and_changed_owner(self):
        for mutation in ('DELETE FROM sessions',"UPDATE sessions SET touched=96400", "UPDATE sessions SET username='other'"):
            with self.subTest(mutation=mutation):
                self.db.execute('UPDATE sessions SET username=?,touched=?',('owner',99900))
                authority=auth.ConsoleAuthority(self.db,self.envelope,self.path)
                self.db.execute(mutation)
                with self.assertRaises(OperationError):authority.recheck()
                if mutation=='DELETE FROM sessions':self.db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(self.digest,'csrf','owner',99000,99900))
        self.db.execute("UPDATE sessions SET username='owner'")
        authority=auth.ConsoleAuthority(self.db,self.envelope,self.path)
        self.path.write_text('{"username":"replacement"}')
        with self.assertRaises(OperationError):authority.recheck()
        self.db.execute("UPDATE sessions SET username='replacement'")
        with self.assertRaises(OperationError):authority.recheck()

    def test_absolute_idle_and_invalid_timestamps(self):
        for created,touched in ((56800,99999),(90000,96400),(99000,96400),(100001,100001),(99000,100001),(-1,99999),(99999,99000),(float('inf'),99999),(99000,None),('bad',99999)):
            self.db.execute('UPDATE sessions SET created=?,touched=?',(created,touched))
            with self.subTest(created=created,touched=touched),self.assertRaises(OperationError):auth.resolve_console(self.db,self.envelope,self.path)

    def test_closed_envelope_no_actor_or_mcp_shortcut(self):
        for value in ({'kind':'mcp','credential_id':'a'*32},dict(self.envelope,actor='owner'),dict(self.envelope,scopes=['databases:manage']),{'kind':'console','session_digest':'a'*63},None):
            with self.subTest(value=value),self.assertRaises(OperationError):auth.resolve_console(self.db,value,self.path)
        with self.assertRaises(OperationError):auth.resolve_console(self.db,dict(self.envelope,session_digest='b'*64),self.path)

    def test_config_required_reloaded_and_errors_sanitized(self):
        with patch.dict(os.environ,{'CREDENTIALS_FILE':str(self.path)}):
            authority=auth.ConsoleAuthority(self.db,self.envelope)
            self.path.unlink()
            with self.assertRaises(OperationError) as error:authority.recheck()
            self.assertNotIn(str(self.path),str(error.exception))
        for value in ('not-json','[]','{"username":""}','{"username":false}'):
            self.path.write_text(value)
            with self.assertRaises(OperationError):auth.resolve_console(self.db,self.envelope,self.path)
        with patch.dict(os.environ,{},clear=True),self.assertRaises(OperationError):auth.resolve_console(self.db,self.envelope)

class WorkloadGrantTests(unittest.TestCase):
    def setUp(self):
        import registry,mcp_credentials
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=connect(Path(self.tmp.name)/'db');self.addCleanup(self.db.close)
        self.path=Path(self.tmp.name)/'owner';self.path.write_text('{"username":"owner"}')
        registry.create_project(self.db,'demo',{'repository':'TPCT/demo','repository_id':42,'label':'Demo'})
        self.credentials=mcp_credentials
        from application_operations import Principal
        self.owner=Principal('owner','console',mcp_credentials.SCOPES)
        import time
        self.values={'label':'Workload reader','scopes':['workloads:read'],'allowed_projects':['demo'],'allowed_repository_ids':[42],'allowed_environments':[['demo','development']],'expires_at':time.time()+3600}

    def authority(self,**changes):
        created=self.credentials.create(self.db,self.owner,dict(self.values,**changes))
        return auth.MCPAuthority(self.db,{'kind':'mcp','token':created['token']},self.path),created

    def test_independent_read_grant_no_database_access_and_no_retry(self):
        authority,_=self.authority()
        self.assertNotIn('databases:read',authority.principal.scopes)
        request={'project':'demo','environment':'development'}
        self.assertEqual(auth.authorize(self.db,authority,auth.actor_identity(authority.principal),request,'read'),authority.principal)
        for action in ('accept','dispatch','commit','replay'):
            with self.assertRaises(OperationError):auth.authorize(self.db,authority,auth.actor_identity(authority.principal),request,action)
        for actor,req in [('other',request),(auth.actor_identity(authority.principal),dict(request,environment='production')),(auth.actor_identity(authority.principal),dict(request,project='other'))]:
            with self.assertRaises(OperationError):auth.authorize(self.db,authority,actor,req,'read')

    def test_retry_live_revocation_and_owner_replacement(self):
        authority,created=self.authority(scopes=['workloads:read','workloads:retry'])
        req={'project':'demo','environment':'development'}
        auth.authorize(self.db,authority,auth.actor_identity(authority.principal),req,'dispatch')
        self.credentials.revoke(self.db,self.owner,created['credential']['id'])
        with self.assertRaises(OperationError):auth.authorize(self.db,authority,auth.actor_identity(authority.principal),req,'commit')
        other,_=self.authority()
        self.path.write_text('{"username":"replacement"}')
        with self.assertRaises(OperationError):other.recheck()

    def test_scope_catalog_does_not_expand_old_tokens_and_requires_read(self):
        old=self.credentials.create(self.db,self.owner,dict(self.values,scopes=['projects:read']))
        principal=self.credentials.authenticate(self.db,old['token'])
        self.assertEqual(principal.scopes,frozenset({'projects:read'}))
        with self.assertRaises(OperationError):auth.MCPAuthority(self.db,{'kind':'mcp','token':old['token']},self.path)
        with self.assertRaises(OperationError):self.authority(scopes=['workloads:retry'])
        authority,_=self.authority(allowed_repository_ids=[99])
        with self.assertRaises(OperationError):auth.authorize(self.db,authority,auth.actor_identity(authority.principal),{'project':'demo','environment':'development'},'read')

    def test_private_peer_channels_explicit_and_independent(self):
        _,created=self.authority()
        envelope={'kind':'mcp','token':created['token']}
        with patch.dict(os.environ,{'WORKLOAD_RECEIVER_UID':'123'},clear=True):
            with self.assertRaises(OperationError):auth.from_peer(self.db,envelope,123,self.path)
        with patch.dict(os.environ,{'WORKLOAD_MCP_UID':'124','WORKLOAD_RECEIVER_UID':'123'},clear=True):
            with self.assertRaises(OperationError):auth.from_peer(self.db,envelope,123,self.path)
            self.assertEqual(auth.from_peer(self.db,envelope,124,self.path).principal.subject_id,'owner')

    def test_distinct_credentials_cannot_share_actor_identity(self):
        first,_=self.authority(scopes=['workloads:read','workloads:retry'])
        second,_=self.authority(scopes=['workloads:read','workloads:retry'])
        self.assertNotEqual(auth.actor_identity(first.principal),auth.actor_identity(second.principal))
        with self.assertRaises(OperationError):
            auth.authorize(self.db,second,auth.actor_identity(first.principal),{'project':'demo','environment':'development'},'read')
        self.assertEqual(auth.authorize(self.db,second,auth.actor_identity(second.principal),{'project':'demo','environment':'development'},'reconcile'),second.principal)
