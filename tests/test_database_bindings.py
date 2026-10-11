import dataclasses
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet
import application_operations as ops
import database_bindings as bindings
import test_managed_services as fixtures

class DatabaseBindingsTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ManagedServiceTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.db=self.f.db
        self.principal=dataclasses.replace(self.f.principal,scopes=frozenset({'databases:manage','databases:read'}))
        self.key=Path(self.f.tmp.name)/'key';self.key.write_bytes(Fernet.generate_key());self.key.chmod(0o600)
        bindings.initialize(self.db)
    def secret(self,**kwargs):
        return bindings.save_credential(self.db,self.principal,'app','development',{'username':'inspector','password':'private-value'},expected_revision=kwargs.pop('expected_revision',0),key_path=self.key,**kwargs)
    def definition(self,secret):
        return {'engine':'postgresql','adapter_version':1,'ownership':{'kind':'adopted-compose','reference':'web','revision':1},
                'database_name':'app','database_port':5432,'secret_id':secret['id'],'secret_revision':secret['revision'],
                'allowed_schemas':['public'],'policy_revision':1}
    def test_ciphertext_bound_to_scope_identity_revision(self):
        secret=self.secret();raw=self.db.execute('SELECT ciphertext FROM database_credentials').fetchone()[0]
        self.assertNotIn(b'private-value',raw)
        actual=bindings.credential_for_use(self.db,'app','development',secret['id'],1,self.key)
        self.assertEqual(actual['password'],'private-value')
        other=self.secret();self.db.execute('UPDATE database_credentials SET ciphertext=? WHERE id=?',(raw,other['id']))
        with self.assertRaises(ValueError):bindings.credential_for_use(self.db,'app','development',other['id'],1,self.key)
        with self.assertRaises(ValueError):bindings.credential_for_use(self.db,'app','production',secret['id'],1,self.key)
    def test_rotation_revocation_and_cas(self):
        secret=self.secret();rotated=self.secret(credential_id=secret['id'],expected_revision=1)
        self.assertEqual(rotated['revision'],2)
        with self.assertRaises(ValueError):self.secret(credential_id=secret['id'],expected_revision=1)
        with self.assertRaises(ValueError):bindings.credential_for_use(self.db,'app','development',secret['id'],1,self.key)
        bindings.revoke_credential(self.db,self.principal,'app','development',secret['id'],expected_revision=2)
        with self.assertRaises(ValueError):bindings.credential_for_use(self.db,'app','development',secret['id'],3,self.key)
    def test_immutable_binding_unverified_and_secret_revision_changes(self):
        secret=self.secret();value=self.definition(secret)
        result=bindings.save_binding(self.db,self.principal,'app','development',value,expected_revision=0)
        self.assertEqual(result['status'],'unverified');self.assertNotIn('private-value',json.dumps(result))
        with self.assertRaises(Exception):self.db.execute("UPDATE database_binding_definitions SET digest='bad'")
        value['database_name']='other'
        changed=bindings.save_binding(self.db,self.principal,'app','development',value,expected_revision=1,binding_id=result['id'])
        self.assertEqual(changed['revision'],2);self.assertNotEqual(result['definition_id'],changed['definition_id'])
        with self.assertRaises(ValueError):bindings.save_binding(self.db,self.principal,'app','development',value,expected_revision=1,binding_id=result['id'])
        self.secret(credential_id=secret['id'],expected_revision=1)
        self.assertEqual(bindings.get_binding(self.db,self.principal,'app','development',result['id'])['status'],'credential-stale')
    def test_scopes_environment_and_transaction_recheck(self):
        restricted=dataclasses.replace(self.principal,allowed_environments=frozenset({('app','production')}))
        with self.assertRaises(ValueError):bindings.list_bindings(self.db,restricted,'app','development')
        def recheck(db,p):
            if db.in_transaction:raise ops.OperationError('revoked','Revoked.',403)
        with patch.object(ops,'recheck_credential',side_effect=recheck),self.assertRaises(ValueError):self.secret()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM database_credentials').fetchone()[0],0)
    def test_closed_schema_and_key_permissions(self):
        secret=self.secret();value=self.definition(secret)
        for extra in ({'verified':True},{'host':'127.0.0.1'},{'password':'secret'}):
            with self.assertRaises(ValueError):bindings.save_binding(self.db,self.principal,'app','development',dict(value,**extra),expected_revision=0)
        self.key.chmod(0o644)
        with self.assertRaises(ValueError):self.secret()

    def test_explicit_empty_ids_and_invalid_revisions_do_not_create(self):
        for revision in (True, -1, 9223372036854775807):
            with self.assertRaises(bindings.BindingError):self.secret(expected_revision=revision)
        with self.assertRaises(bindings.BindingError):self.secret(credential_id='')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM database_credentials').fetchone()[0],0)

    def test_persistence_disabled_binding_and_sanitized_audit(self):
        from store import connect
        secret=self.secret()
        result=bindings.save_binding(self.db,self.principal,'app','development',self.definition(secret),expected_revision=0)
        other=connect(self.f.path)
        try:
            self.assertEqual(bindings.get_binding(other,self.principal,'app','development',result['id']),result)
            self.assertEqual(bindings.credential_for_use(other,'app','development',secret['id'],1,self.key)['username'],'inspector')
        finally:other.close()
        disabled=bindings.disable_binding(self.db,self.principal,'app','development',result['id'],expected_revision=1)
        self.assertEqual(disabled['status'],'disabled');self.assertFalse(disabled['verified'])
        with self.assertRaises(bindings.BindingError):bindings.disable_binding(self.db,self.principal,'app','development',result['id'],expected_revision=1)
        audit=[dict(row) for row in self.db.execute('SELECT * FROM database_binding_events')]
        self.assertNotIn('private-value',json.dumps(audit));self.assertNotIn('inspector',json.dumps(audit))
        self.assertEqual([row['action'] for row in audit],['credential-saved','binding-saved','binding-disabled'])

    def test_wrong_key_old_ciphertext_and_missing_read_scope_fail_closed(self):
        secret=self.secret();raw=self.db.execute('SELECT ciphertext FROM database_credentials').fetchone()[0]
        self.secret(credential_id=secret['id'],expected_revision=1)
        self.db.execute('UPDATE database_credentials SET ciphertext=? WHERE id=?',(raw,secret['id']))
        with self.assertRaises(bindings.BindingError):bindings.credential_for_use(self.db,'app','development',secret['id'],2,self.key)
        other=self.secret();self.key.write_bytes(Fernet.generate_key())
        with self.assertRaises(bindings.BindingError):bindings.credential_for_use(self.db,'app','development',other['id'],1,self.key)
        denied=dataclasses.replace(self.principal,scopes=frozenset())
        with self.assertRaises(ops.OperationError):bindings.list_bindings(self.db,denied,'app','development')

    def test_row_policy_is_persisted_in_immutable_definition(self):
        secret=self.secret();values=self.definition(secret)
        values['row_columns']={'public':{'a.b':['id','name'],'a':['id']}}
        first=bindings.save_binding(self.db,self.principal,'app','development',values,expected_revision=0)
        self.assertEqual(first['definition']['row_columns'],values['row_columns'])
        values['row_columns']['public']['a.b']=['id']
        second=bindings.save_binding(self.db,self.principal,'app','development',values,expected_revision=1,binding_id=first['id'])
        self.assertNotEqual(first['definition_digest'],second['definition_digest'])
        stored=json.loads(self.db.execute('SELECT document FROM database_binding_definitions WHERE id=?',(first['definition_id'],)).fetchone()[0])
        self.assertEqual(stored['row_columns']['public']['a.b'],['id','name'])

    def test_row_policy_rejects_unscoped_and_unbounded_columns(self):
        secret=self.secret();values=self.definition(secret)
        for policy in ({'other':{'table':['id']}},{'public':{'table':['id','id']}},{'public':{'table':[]}},{'public':{'table':['x']*21}},{'public':{'table':'*'}},[]):
            with self.subTest(policy=policy),self.assertRaises(bindings.BindingError):
                bindings.save_binding(self.db,self.principal,'app','development',dict(values,row_columns=policy),expected_revision=0)
