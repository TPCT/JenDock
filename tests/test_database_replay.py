import dataclasses
import json
import unittest
from store import connect
import database_bindings as bindings
import test_database_bindings as fixtures


class DatabaseReplayTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DatabaseBindingsTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db;self.p=self.f.principal
    def save(self,**changes):
        args={'values':{'username':'reader','password':'private-request-password'},'expected_revision':0,'key_path':self.f.key,'request_id':'request-000001'};args.update(changes)
        return bindings.save_credential(self.db,self.p,'app','development',**args)
    def test_reconnect_replay_original_metadata_no_duplicate_or_plaintext(self):
        first=self.save();other=connect(self.f.f.path)
        try:
            replay=bindings.save_credential(other,self.p,'app','development',{'username':'reader','password':'private-request-password'},expected_revision=0,key_path=self.f.key,request_id='request-000001')
            self.assertEqual(first,replay)
        finally:other.close()
        self.assertEqual(self.db.execute('SELECT count(*) FROM database_credentials').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM database_binding_events').fetchone()[0],1)
        row=self.db.execute('SELECT payload,result FROM database_save_requests').fetchone()
        self.assertNotIn(b'private-request-password',row['payload']);self.assertNotIn('private-request-password',row['result'])
        bindings.revoke_credential(self.db,self.p,'app','development',first['id'],expected_revision=1)
        self.assertEqual(self.save(),first)
        self.assertFalse(bindings.list_credentials(self.db,self.p,'app','development')[0]['enabled'])
    def test_conflicts_scope_and_live_recheck(self):
        self.save()
        with self.assertRaises(bindings.BindingError):self.save(values={'username':'reader','password':'changed'})
        with self.assertRaises(bindings.BindingError):self.save(expected_revision=1)
        def revoked():raise ValueError('session revoked')
        with self.assertRaises(ValueError):self.save(revalidate=revoked)
        other=dataclasses.replace(self.p,subject_id='other')
        result=bindings.save_credential(self.db,other,'app','development',{'username':'reader','password':'other'},expected_revision=0,key_path=self.f.key,request_id='request-000001')
        self.assertTrue(result['id'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM database_credentials').fetchone()[0],2)
    def test_binding_replay_conflict_and_atomic_failure(self):
        secret=self.save();values=self.f.definition(secret)
        def save(value=values):return bindings.save_binding(self.db,self.p,'app','development',value,expected_revision=0,request_id='binding-000001')
        first=save();self.assertEqual(save(),first)
        with self.assertRaises(bindings.BindingError):save(dict(values,database_name='other'))
        self.assertEqual(self.db.execute('SELECT count(*) FROM database_binding_definitions').fetchone()[0],1)
        self.db.execute("CREATE TRIGGER fail_replay BEFORE INSERT ON database_save_requests WHEN NEW.request_id='failure-00001' BEGIN SELECT RAISE(ABORT,'test persistence failure'); END")
        with self.assertRaises(Exception):self.save(request_id='failure-00001')
        self.assertEqual(self.db.execute('SELECT count(*) FROM database_credentials').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM database_binding_events').fetchone()[0],2)
    def test_ciphertext_transplant_and_invalid_request_keys_rejected(self):
        self.save();self.save(request_id='request-000002')
        raw=self.db.execute("SELECT payload FROM database_save_requests WHERE request_id='request-000001'").fetchone()[0]
        self.db.execute("UPDATE database_save_requests SET payload=? WHERE request_id='request-000002'",(raw,))
        with self.assertRaises(bindings.BindingError):self.save(request_id='request-000002')
        for key in ('',True,'short','bad key','a'*129):
            with self.subTest(key=key),self.assertRaises(bindings.BindingError):self.save(request_id=key)
