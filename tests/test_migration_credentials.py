import dataclasses
import json
import unittest
from unittest.mock import patch
import database_bindings as bindings
import migration_credentials as credentials
import test_database_bindings as fixtures


class MigrationCredentialTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DatabaseBindingsTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db,self.principal,self.key=self.f.db,self.f.principal,self.f.key

    def save(self,**kwargs):
        return credentials.save_credential(self.db,self.principal,'app','development',
            {'username':'migrator','password':'test-migration-secret'},key_path=self.key,
            **({'expected_revision':0}|kwargs))

    def test_separate_purpose_encryption_and_scoped_resolution(self):
        saved=self.save()
        self.assertEqual(credentials.credential_for_use(self.db,'app','development',saved['id'],1,self.key)['username'],'migrator')
        self.assertNotIn('test-migration-secret',json.dumps(saved))
        self.assertNotIn(b'test-migration-secret',self.db.execute('SELECT ciphertext FROM migration_credentials').fetchone()[0])
        with self.assertRaises(ValueError):bindings.credential_for_use(self.db,'app','development',saved['id'],1,self.key)
        with self.assertRaises(ValueError):credentials.credential_for_use(self.db,'app','production',saved['id'],1,self.key)
        inspector=self.f.secret()
        with self.assertRaises(ValueError):credentials.credential_for_use(self.db,'app','development',inspector['id'],1,self.key)

    def test_rotation_revocation_and_replayed_creation(self):
        first=self.save(request_id='migration-create-1')
        self.assertEqual(self.save(request_id='migration-create-1'),first)
        rotated=self.save(credential_id=first['id'],expected_revision=1)
        self.assertEqual(rotated['revision'],2)
        with self.assertRaises(ValueError):credentials.credential_for_use(self.db,'app','development',first['id'],1,self.key)
        revoked=credentials.revoke_credential(self.db,self.principal,'app','development',first['id'],expected_revision=2)
        self.assertFalse(revoked['enabled'])
        with self.assertRaises(ValueError):credentials.credential_for_use(self.db,'app','development',first['id'],3,self.key)

    def test_current_authority_rechecked_inside_transaction(self):
        import application_operations as ops
        def check(db,principal):
            if db.in_transaction:raise ops.OperationError('forbidden','Revoked',403)
        with patch.object(ops,'recheck_credential',side_effect=check),self.assertRaises(ValueError):self.save()
        self.assertEqual(self.db.execute('SELECT count(*) FROM migration_credentials').fetchone()[0],0)
        denied=dataclasses.replace(self.principal,scopes=frozenset({'databases:read'}))
        with self.assertRaises(ValueError):credentials.list_credentials(self.db,denied,'app','development')

    def test_ciphertext_cannot_be_rebound_or_downgraded(self):
        first=self.save();raw=self.db.execute('SELECT ciphertext FROM migration_credentials').fetchone()[0]
        self.save(credential_id=first['id'],expected_revision=1)
        self.db.execute('UPDATE migration_credentials SET ciphertext=? WHERE id=?',(raw,first['id']))
        with self.assertRaises(ValueError):credentials.credential_for_use(self.db,'app','development',first['id'],2,self.key)
        inspector=self.f.secret();raw=self.db.execute('SELECT ciphertext FROM database_credentials WHERE id=?',(inspector['id'],)).fetchone()[0]
        self.db.execute('UPDATE migration_credentials SET ciphertext=? WHERE id=?',(raw,first['id']))
        with self.assertRaises(ValueError):credentials.credential_for_use(self.db,'app','development',first['id'],2,self.key)

    def test_current_metadata_differs_from_historical_receipt(self):
        first=self.save(request_id='migration-create-reload')
        credentials.revoke_credential(self.db,self.principal,'app','development',first['id'],expected_revision=1)
        self.assertEqual(self.save(request_id='migration-create-reload'),first)
        current=credentials.get_credential(self.db,self.principal,'app','development',first['id'])
        self.assertFalse(current['enabled']);self.assertEqual(current['revision'],2)
        self.assertNotIn('password',current);self.assertNotIn('username',current)

    def test_rejected_request_cannot_become_a_later_success(self):
        with self.assertRaises(bindings.SaveRejected):self.save(expected_revision=1,request_id='migration-rejected-1')
        with self.assertRaises(bindings.SaveRejected):self.save(expected_revision=1,request_id='migration-rejected-1')
        with self.assertRaises(bindings.BindingError):self.save(expected_revision=0,request_id='migration-rejected-1')
        self.assertEqual(self.db.execute('SELECT count(*) FROM migration_credentials').fetchone()[0],0)
        receipt=self.db.execute('SELECT payload FROM database_save_requests').fetchone()[0]
        self.assertNotIn(b'test-migration-secret',receipt)
