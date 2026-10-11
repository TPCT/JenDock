import dataclasses
import json
import unittest
from unittest.mock import patch
import application_credentials as credentials
import database_bindings as bindings
import test_database_bindings as fixtures

class ApplicationCredentialTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DatabaseBindingsTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db,self.principal,self.key=self.f.db,self.f.principal,self.f.key
    def save(self,password='private-app-secret',**kwargs):
        return credentials.save_credential(self.db,self.principal,'app','development',{'username':'application','password':password},key_path=self.key,**({'expected_revision':0}|kwargs))
    def runtime(self,saved,revision,authorize=lambda:None):
        return credentials.credential_for_runtime(self.db,'app','development',saved['id'],revision,self.key,authorize=authorize)
    def test_rotation_preserves_runtime_but_forbids_old_selection(self):
        first=self.save();second=self.save('rotated-secret',credential_id=first['id'],expected_revision=1)
        self.assertEqual(second['revision'],2)
        self.assertEqual(self.runtime(first,1)['password'],'private-app-secret')
        self.assertEqual(self.runtime(first,2)['password'],'rotated-secret')
        with self.assertRaises(ValueError):credentials.credential_for_selection(self.db,'app','development',first['id'],1,self.key)
        self.assertEqual(credentials.credential_for_selection(self.db,'app','development',first['id'],2,self.key)['username'],'application')
    def test_revoke_and_reenable_never_revives_historical_secrets(self):
        first=self.save();credentials.revoke_credential(self.db,self.principal,'app','development',first['id'],expected_revision=1)
        with self.assertRaises(ValueError):self.runtime(first,1)
        self.save('new-account-secret',credential_id=first['id'],expected_revision=2)
        with self.assertRaises(ValueError):self.runtime(first,1)
        self.assertEqual(self.runtime(first,3)['password'],'new-account-secret')
    def test_revision_ciphertexts_immutable_and_no_public_secret(self):
        first=self.save(request_id='app-create-1')
        self.assertEqual(self.save(request_id='app-create-1'),first)
        for sql in ('UPDATE application_credential_revisions SET ciphertext=x\'01\'', 'DELETE FROM application_credential_revisions'):
            with self.assertRaises(Exception):self.db.execute(sql)
        raw=self.db.execute('SELECT ciphertext FROM application_credential_revisions').fetchone()[0]
        self.assertNotIn(b'private-app-secret',raw)
        self.assertNotIn('password',json.dumps(credentials.list_credentials(self.db,self.principal,'app','development')))
        with self.assertRaises(ValueError):self.save('different',request_id='app-create-1')
        receipt=self.db.execute('SELECT payload FROM database_save_requests').fetchone()[0]
        self.assertNotIn(b'private-app-secret',receipt)
    def test_ciphertext_identity_binding_rejects_other_id_and_purpose(self):
        first=self.save();other=self.save('other-secret')
        raw=self.db.execute('SELECT ciphertext FROM application_credential_revisions WHERE id=?',(first['id'],)).fetchone()[0]
        self.db.execute('INSERT INTO application_credential_revisions VALUES(?,?,?,?)',(other['id'],2,raw,0))
        self.db.execute('UPDATE application_credentials SET revision=2 WHERE id=?',(other['id'],))
        with self.assertRaises(ValueError):self.runtime(other,2)
        inspector=self.f.secret()
        raw=self.db.execute('SELECT ciphertext FROM database_credentials WHERE id=?',(inspector['id'],)).fetchone()[0]
        self.db.execute('INSERT INTO application_credential_revisions VALUES(?,?,?,?)',(first['id'],2,raw,0))
        self.db.execute('UPDATE application_credentials SET revision=2 WHERE id=?',(first['id'],))
        with self.assertRaises(ValueError):self.runtime(first,2)

    def test_replay_is_historical_metadata_and_revocation_persists(self):
        first=self.save(request_id='receipt-original')
        credentials.revoke_credential(self.db,self.principal,'app','development',first['id'],expected_revision=1)
        self.assertEqual(self.save(request_id='receipt-original'),first)
        credentials.initialize(self.db)
        self.assertFalse(credentials.get_credential(self.db,self.principal,'app','development',first['id'])['enabled'])
        with self.assertRaises(ValueError):self.runtime(first,1)
        with self.assertRaises(Exception):
            self.db.execute('INSERT OR REPLACE INTO application_credential_revisions SELECT id,revision,ciphertext,created FROM application_credential_revisions')

    def test_scope_and_other_vault_secrets_rejected(self):
        first=self.save();inspector=self.f.secret()
        with self.assertRaises(ValueError):credentials.credential_for_selection(self.db,'app','production',first['id'],1,self.key)
        with self.assertRaises(ValueError):self.runtime(inspector,1)
        with self.assertRaises(ValueError):bindings.credential_for_use(self.db,'app','development',first['id'],1,self.key)
        denied=dataclasses.replace(self.principal,scopes=frozenset({'databases:read'}))
        with self.assertRaises(ValueError):credentials.list_credentials(self.db,denied,'app','development')
    def test_runtime_authority_required_and_rechecked_after_decryption(self):
        first=self.save();calls=[]
        with self.assertRaises(ValueError):self.runtime(first,1,None)
        def guard():
            calls.append(True)
            if len(calls)==2:raise ValueError('revoked')
        with self.assertRaises(ValueError):self.runtime(first,1,guard)
        self.assertEqual(len(calls),2)
    def test_changed_head_during_runtime_resolution_rejected(self):
        first=self.save();calls=[]
        def guard():
            calls.append(True)
            if len(calls)==2:credentials.revoke_credential(self.db,self.principal,'app','development',first['id'],expected_revision=1)
        with self.assertRaises(ValueError):self.runtime(first,1,guard)
    def test_mutation_rechecks_current_authority_atomically(self):
        import application_operations as ops
        def check(db,principal):
            if db.in_transaction:raise ops.OperationError('forbidden','Revoked',403)
        with patch.object(ops,'recheck_credential',side_effect=check),self.assertRaises(ValueError):self.save()
        self.assertEqual(self.db.execute('SELECT count(*) FROM application_credentials').fetchone()[0],0)

if __name__=='__main__':unittest.main()
