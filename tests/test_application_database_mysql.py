"""Application attachment intent and lifecycle credential semantics, not live DB tests."""
import unittest
import application_database_bindings as bindings
import application_credentials as credentials
import test_application_database_bindings as fixtures


class MySQLAttachmentTests(unittest.TestCase):
    def fixture(self,engine):
        f=fixtures.AttachmentTests();f.engine=engine;f.setUp();self.addCleanup(f.doCleanups)
        return f

    def test_mysql_and_mariadb_mapping_rotation_and_revoked_stop(self):
        for engine in ('mysql','mariadb'):
            with self.subTest(engine=engine):
                f=self.fixture(engine);saved=f.save();pin=f.pin(saved)
                private=bindings.runtime_environment(f.db,'example','development',f.service,pin,f.key,authorize=lambda:None)
                self.assertEqual(private['DATABASE_URL'],f"mysql://app:private%2F%40%3Avalue@appdb-{saved['id']}:3306/app")
                self.assertEqual(saved['definition']['engine'],engine)
                self.assertNotIn('private',str(saved))
                f.register_pin(saved)
                credentials.revoke_credential(f.db,f.principal,'example','development',f.secret['id'],expected_revision=1)
                bindings.disable_binding(f.db,f.principal,'example','development',saved['id'],expected_revision=1)
                self.assertTrue(bindings.verify_stopping_environment(f.db,'example','development',f.service,pin,private,f.key,authorize=lambda:None))
                with self.assertRaises(ValueError):f.resolve(saved)
                with self.assertRaises(ValueError):bindings.resolve_stopping(f.db,'example','production',f.service,pin,authorize=lambda:None)

    def test_fields_and_foreign_endpoint_rejection(self):
        for engine in ('mysql','mariadb'):
            f=self.fixture(engine)
            mapping={'host':'DB_HOST','port':'DB_PORT','database':'DB_DATABASE','username':'DB_USERNAME','password':'DB_PASSWORD'}
            for changed in ({'database_name':'other'},{'database_port':5432},{'engine':'postgresql'}):
                with self.assertRaises(ValueError):f.save(f.values|changed)
            saved=f.save(f.values|{'environment_mapping':mapping})
            private=bindings.runtime_environment(f.db,'example','development',f.service,f.pin(saved),f.key,authorize=lambda:None)
            self.assertEqual(private['DB_PORT'],'3306')
            self.assertEqual(private['DB_DATABASE'],'app')
            self.assertEqual(private['DB_PASSWORD'],'private/@:value')
