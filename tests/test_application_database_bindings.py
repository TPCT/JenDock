import json,tempfile,unittest,uuid
from pathlib import Path
from cryptography.fernet import Fernet
import application_database_bindings as bindings
import application_credentials as credentials
import application_operations as ops
import adopted_workloads
import managed_services
import registry
import test_adopted_workloads as fixtures

class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AdoptedWorkloadTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.db=self.f.db
        with registry.transaction(self.db):adopted_workloads.retain(self.db,'example','development')
        self.principal=ops.Principal('owner','console',frozenset({'databases:manage'}))
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.key=Path(self.temp.name)/'key';self.key.write_bytes(Fernet.generate_key());self.key.chmod(0o600)
        managed_services.initialize(self.db);self.service=str(uuid.uuid4())
        definition={'service_id':self.service,'project':'example','environment':'development','desired_state':'running'}
        self.db.execute('INSERT INTO service_definitions VALUES(?,?,?,?)',(self.service,registry.canonical(definition),managed_services.digest(definition),0))
        self.db.execute('INSERT INTO managed_services VALUES(?,?,?,?,?,?,?,?,?)',(self.service,'example','development','web',self.service,'operation','running',0,0))
        self.secret=credentials.save_credential(self.db,self.principal,'example','development',{'username':'app','password':'private/@:value'},expected_revision=0,key_path=self.key)
        self.values={'service_id':self.service,'ownership':{'kind':'adopted-compose','reference':'db','revision':self.f.recipe_id},'engine':'postgresql','database_name':'app','database_port':5432,'secret_id':self.secret['id'],'secret_revision':1,'environment_mapping':{'url':'DATABASE_URL'}}
    def save(self,values=None,**kwargs):
        return bindings.save_binding(self.db,self.principal,'example','development',values or self.values,**({'expected_revision':0}|kwargs))
    def pin(self,value):return {'binding_id':value['id'],'revision':value['revision'],'digest':value['definition_digest']}
    def resolve(self,value):return bindings.resolve_runtime(self.db,'example','development',self.service,self.pin(value),authorize=lambda:None)
    def test_save_replay_scope_and_private_environment(self):
        saved=self.save(request_id='attachment-create');self.assertEqual(saved,self.save(request_id='attachment-create'))
        self.assertEqual(saved['application_status'],'not-observed');self.assertNotIn('private',json.dumps(saved))
        result=bindings.runtime_environment(self.db,'example','development',self.service,self.pin(saved),self.key,authorize=lambda:None)
        self.assertIn('private%2F%40%3Avalue',result['DATABASE_URL'])
        self.assertIn('appdb-'+saved['id'],result['DATABASE_URL'])
        with self.assertRaises(ValueError):bindings.resolve_selection(self.db,'example','production',self.service,self.pin(saved))
    def test_rotation_preserves_old_pin_topology_and_disable_fences_history(self):
        first=self.save();old=self.resolve(first)
        credentials.save_credential(self.db,self.principal,'example','development',{'username':'app','password':'rotated'},credential_id=self.secret['id'],expected_revision=1,key_path=self.key)
        with self.assertRaises(ValueError):bindings.resolve_selection(self.db,'example','development',self.service,self.pin(first))
        second=self.save(dict(self.values,secret_revision=2),binding_id=first['id'],expected_revision=1)
        self.assertEqual(self.resolve(first)['topology_digest'],self.resolve(second)['topology_digest'])
        self.assertEqual(self.resolve(first),old)
        bindings.disable_binding(self.db,self.principal,'example','development',first['id'],expected_revision=2)
        third=self.save(dict(self.values,secret_revision=2),binding_id=first['id'],expected_revision=3)
        with self.assertRaises(ValueError):self.resolve(first)
        self.resolve(third)
    def test_bad_mapping_endpoint_and_service_rejected(self):
        for changes in ({'database_name':'foreign'},{'ownership':dict(self.values['ownership'],revision=999)},{'service_id':str(uuid.uuid4())},{'environment_mapping':{'url':'LD_PRELOAD'}},{'environment_mapping':{'url':'${PASSWORD}'}},{'engine':'mysql'}):
            with self.assertRaises(ValueError):self.save(dict(self.values,**changes))
    def test_immutable_pin_and_revoked_credential(self):
        saved=self.save()
        with self.assertRaises(Exception):self.db.execute("UPDATE application_database_definitions SET digest='bad'")
        bad=self.pin(saved)|{'digest':'0'*64}
        with self.assertRaises(ValueError):bindings.resolve_selection(self.db,'example','development',self.service,bad)
        credentials.revoke_credential(self.db,self.principal,'example','development',self.secret['id'],expected_revision=1)
        with self.assertRaises(ValueError):self.resolve(saved)
    def test_field_mapping_cas_and_confirmed_receipt(self):
        values=dict(self.values,environment_mapping={'host':'DB_HOST','port':'DB_PORT','database':'DB_DATABASE','username':'DB_USERNAME','password':'DB_PASSWORD'})
        first=self.save(values,request_id='field-map-create')
        private=bindings.runtime_environment(self.db,'example','development',self.service,self.pin(first),self.key,authorize=lambda:None)
        self.assertEqual(private['DB_PASSWORD'],'private/@:value');self.assertEqual(private['DB_PORT'],'5432')
        self.assertEqual(set(private),{'DB_HOST','DB_PORT','DB_DATABASE','DB_USERNAME','DB_PASSWORD'})
        with self.assertRaises(ValueError):self.save(values,binding_id=first['id'],expected_revision=0)
        bindings.disable_binding(self.db,self.principal,'example','development',first['id'],expected_revision=1)
        self.assertEqual(first,self.save(values,request_id='field-map-create'))
        current=bindings.get_binding(self.db,self.principal,'example','development',first['id'])
        self.assertEqual(current['status'],'disabled');self.assertEqual(current['application_status'],'not-observed')

    def test_recorded_keys_remain_visible_after_revocation_without_runtime_authority(self):
        saved=self.save();pin=self.pin(saved)
        credentials.revoke_credential(self.db,self.principal,'example','development',self.secret['id'],expected_revision=1)
        bindings.disable_binding(self.db,self.principal,'example','development',saved['id'],expected_revision=1)
        self.assertEqual(bindings.recorded_environment_keys(self.db,'example','development',self.service,pin),['DATABASE_URL'])
        with self.assertRaises(ValueError):self.resolve(saved)
        with self.assertRaises(ValueError):bindings.recorded_environment_keys(self.db,'example','production',self.service,pin)
        with self.assertRaises(ValueError):bindings.recorded_environment_keys(self.db,'example','development',self.service,dict(pin,digest='0'*64))

    def register_pin(self,saved):
        definition=dict(managed_services.get(self.db,self.service)['definition'],runtime={'database_attachment':self.pin(saved)})
        identifier=str(uuid.uuid4())
        self.db.execute('INSERT INTO service_definitions VALUES(?,?,?,?)',(identifier,registry.canonical(definition),managed_services.digest(definition),0))
        self.db.execute('UPDATE managed_services SET definition_id=? WHERE id=?',(identifier,self.service))

    def test_stop_compares_revoked_secret_only_for_registered_pin(self):
        saved=self.save();pin=self.pin(saved)
        actual=bindings.runtime_environment(self.db,'example','development',self.service,pin,self.key,authorize=lambda:None)
        with self.assertRaises(ValueError):bindings.resolve_stopping(self.db,'example','development',self.service,pin,authorize=lambda:None)
        self.register_pin(saved)
        credentials.revoke_credential(self.db,self.principal,'example','development',self.secret['id'],expected_revision=1)
        bindings.disable_binding(self.db,self.principal,'example','development',saved['id'],expected_revision=1)
        self.assertTrue(bindings.verify_stopping_environment(self.db,'example','development',self.service,pin,actual,self.key,authorize=lambda:None))
        with self.assertRaises(ValueError):bindings.verify_stopping_environment(self.db,'example','development',self.service,pin,dict(actual,DATABASE_URL='foreign'),self.key,authorize=lambda:None)
        with self.assertRaises(ValueError):self.resolve(saved)

    def test_runtime_rechecks_binding_disable(self):
        saved=self.save();calls=[]
        def guard():
            calls.append(True)
            if len(calls)==2:bindings.disable_binding(self.db,self.principal,'example','development',saved['id'],expected_revision=1)
        with self.assertRaises(ValueError):bindings.resolve_runtime(self.db,'example','development',self.service,self.pin(saved),authorize=guard)

if __name__=='__main__':unittest.main()
