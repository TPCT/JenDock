import dataclasses
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import database_auth as auth
from application_operations import OperationError
from store import connect


class DatabaseAuthTests(unittest.TestCase):
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
