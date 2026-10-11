import json
import unittest
import service_runtime as runtime
import test_database_bindings as fixtures


class ServiceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DatabaseBindingsTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db,self.key=self.f.db,self.f.key
        self.scope=('app','development','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
    def save(self,values=None,expected=None):
        return runtime.save(self.db,*self.scope,values or {'TOKEN':'private$secret'},expected=expected,key_path=self.key,authorize=lambda:None)
    def resolve(self,ref,scope=None):
        return runtime.resolve(self.db,*(scope or self.scope),ref,key_path=self.key,authorize=lambda:None)
    def test_encrypted_immutable_and_historical_revision(self):
        first=self.save();second=self.save({'TOKEN':'new-secret'},first)
        self.assertEqual(self.resolve(first),{'TOKEN':'private$secret'})
        self.assertEqual(self.resolve(second),{'TOKEN':'new-secret'})
        self.assertNotIn('private$secret',json.dumps(first))
        raw=self.db.execute('SELECT ciphertext FROM service_runtime_revisions').fetchone()[0]
        self.assertNotIn(b'private$secret',raw)
        for sql in ('DELETE FROM service_runtime_revisions','UPDATE service_runtime_revisions SET ciphertext=x\'00\'', 'INSERT OR REPLACE INTO service_runtime_revisions SELECT * FROM service_runtime_revisions'):
            with self.assertRaises(Exception):self.db.execute(sql)
    def test_desired_applied_cas_and_scope(self):
        first=self.save();self.assertEqual(runtime.status(self.db,*self.scope),{'desired':first,'applied':None})
        runtime.mark_applied(self.db,*self.scope,first,expected_applied=None,authorize=lambda:None)
        second=self.save({'TOKEN':'second'},first)
        self.assertEqual(runtime.status(self.db,*self.scope),{'desired':second,'applied':first})
        with self.assertRaises(ValueError):self.save({'TOKEN':'stale'},first)
        with self.assertRaises(ValueError):self.resolve(first,('app','production','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'))
        with self.assertRaises(ValueError):runtime.mark_applied(self.db,*self.scope,second,expected_applied=None,authorize=lambda:None)
        runtime.mark_applied(self.db,*self.scope,second,expected_applied=first,authorize=lambda:None)
    def test_authority_is_required_and_rechecked_and_bad_values_rejected(self):
        with self.assertRaises(ValueError):runtime.save(self.db,*self.scope,{'X':'secret'},expected=None,key_path=self.key,authorize=None)
        for values in ({'BAD-KEY':'x'},{'X':None},{'X':'\x00'}, {'X':'a'*32769}):
            with self.assertRaises(ValueError):runtime.save(self.db,*self.scope,values,expected=None,key_path=self.key,authorize=lambda:None)
        ref=self.save();calls=[]
        def deny():
            calls.append(1)
            if len(calls)>1:raise ValueError('revoked')
        with self.assertRaises(ValueError):runtime.resolve(self.db,*self.scope,ref,key_path=self.key,authorize=deny)
        self.assertEqual(len(calls),2)
    def test_digest_and_ciphertext_identity_are_checked(self):
        ref=self.save()
        with self.assertRaises(ValueError):self.resolve({**ref,'digest':'0'*64})
        with self.assertRaises(ValueError):self.resolve({**ref,'revision':True})
    def test_manifest_placeholders_contain_no_values_and_are_service_specific(self):
        ref=self.save()
        manifest,private=runtime.compose_environment(self.db,*self.scope,ref,key_path=self.key,authorize=lambda:None)
        self.assertNotIn('private$secret',json.dumps(manifest))
        self.assertEqual(len(private),1)
        self.assertEqual(list(private.values()),['private$secret'])
        variable=next(iter(private));self.assertEqual(manifest,{'TOKEN':'${'+variable+':?Managed runtime value is unavailable}'})

    def test_context_manifest_and_actual_image_defaults(self):
        import copy
        import service_provisioning
        import test_service_provisioning
        f=test_service_provisioning.ProvisioningTests();f.setUp();self.addCleanup(f.doCleanups)
        f.execute()
        definition=copy.deepcopy(f.definition);definition.update(project='app',service_id=self.scope[2])
        definition['runtime']={'environment':self.save()}
        context=runtime.Context(self.db,lambda:None,self.key)
        spec=service_provisioning.service_config(definition,'operation',runtime_context=context)
        self.assertNotIn('private$secret',json.dumps(spec))
        context.verify(definition,{'Config':{'Env':['PATH=/bin','TOKEN=private$secret']}},['PATH=/bin'])
        with self.assertRaises(ValueError):context.verify(definition,{'Config':{'Env':['PATH=/wrong','TOKEN=private$secret']}},['PATH=/bin'])
        with self.assertRaises(ValueError):context.verify(definition,{'Config':{'Env':['PATH=/bin','TOKEN=private$secret','EXTRA=bad']}},['PATH=/bin'])
        with self.assertRaises(ValueError):service_provisioning.service_config(definition,'operation')
