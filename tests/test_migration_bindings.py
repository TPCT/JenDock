import unittest
import migration_bindings as bindings
import migration_credentials as credentials
import test_database_bindings as fixtures

class MigrationBindingsTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DatabaseBindingsTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db,self.principal=self.f.db,self.f.principal
        self.secret=credentials.save_credential(self.db,self.principal,'app','development',{'username':'migrator','password':'private'},expected_revision=0,key_path=self.f.key)
        self.values=dict(engine='postgresql',adapter_version=1,ownership={'kind':'adopted-compose','reference':'app','revision':1},database_name='app',database_port=5432,secret_id=self.secret['id'],secret_revision=1,framework='alembic',grammar=None,migration_paths=['alembic/versions'],ledger_schema='public')
    def save(self,**kwargs):
        return bindings.save_binding(self.db,self.principal,'app','development',self.values,**({'expected_revision':0}|kwargs))
    def test_capture_replay_and_revocation(self):
        saved=self.save(request_id='migration-binding-1')
        self.assertEqual(saved,self.save(request_id='migration-binding-1'))
        capture=bindings.capture(self.db,'app','development')
        self.assertEqual(bindings.validate_capture(self.db,capture)['id'],saved['id'])
        credentials.revoke_credential(self.db,self.principal,'app','development',self.secret['id'],expected_revision=1)
        with self.assertRaises(ValueError):bindings.validate_capture(self.db,capture)
    def test_cas_single_binding_and_disabled_capture(self):
        saved=self.save()
        with self.assertRaises(ValueError):self.save()
        capture=bindings.capture(self.db,'app','development')
        bindings.disable_binding(self.db,self.principal,'app','development',saved['id'],expected_revision=1)
        with self.assertRaises(ValueError):bindings.validate_capture(self.db,capture)
        self.assertIsNone(bindings.capture(self.db,'app','development'))
    def test_closed_configuration_paths_and_grammar(self):
        for key,value in [('migration_paths',['../migrations']),('migration_paths',['/migrations']),('migration_paths',['a','a/b']),('engine','mysql'),('ledger_schema','other'),('adapter_version',True),('grammar','arbitrary')]:
            with self.subTest(key=key,value=value):
                old=self.values[key];self.values[key]=value
                with self.assertRaises(ValueError):self.save()
                self.values[key]=old
        self.values.update(framework='laravel',grammar='laravel12-stock-postgresql-v1',migration_paths=['database/migrations'])
        self.assertEqual(self.save()['definition']['framework'],'laravel')
    def test_inspection_secret_and_cross_environment_rejected(self):
        self.values['secret_id']=self.f.secret()['id']
        with self.assertRaises(ValueError):self.save()
        self.values['secret_id']=self.secret['id']
        with self.assertRaises(ValueError):bindings.save_binding(self.db,self.principal,'app','production',self.values,expected_revision=0)
