"""Managed control -> immutable runtime -> next delivery selection regressions.

Actual SQLite registry/control dispatch and private manifests; the Docker rollout
adapter is scripted. This is not real Docker or browser acceptance.
"""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet
import control
import managed_delivery
import managed_services
import registry
import service_provisioning
import service_runtime
import test_managed_delivery as fixtures


class ManagedEnvironmentRegressionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ManagedDeliveryTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'compose.json';self.key=self.root/'vault.key'
        self.key.write_bytes(Fernet.generate_key());self.key.chmod(0o600)
        self.db=self.f.db;self.service=managed_services.get(self.db,self.f.sid)
        self.binding=dict(self.f.binding,compose_files=[str(self.path)],image_id=self.service['definition']['artifact']['image'],
                          definition_digest=managed_services.digest(self.service['definition']),observed=time.time())
        self.db.execute('UPDATE service_bindings SET document=? WHERE service_id=?',(registry.canonical(self.binding),self.f.sid))
        self.container={'Id':self.binding['container_id'],'Image':self.binding['image_id'],'State':{'Running':True},
                        'Config':{'Labels':{'net.shiftcodes.managed.service':self.f.sid},'Env':['PATH=/bin']},
                        '_monitor_recipe':{'managed_environment':{'project':'app','environment':'development'}}}
        self.path.write_text(registry.canonical({'name':'app','services':{'development-web':{}},'networks':{}}))
    def save(self):
        body={'action':'environment-save','project':'app','container':self.binding['container_name'],
              'container_id':self.container['Id'],'request_id':'managed-env-regression-001',
              'changes':{'DB_HOST':'database-fixture'},'revision':control.revision([self.path],self.container,json.loads(self.path.read_text()))}
        def rollout(db,operation,item,**kwargs):
            context=service_runtime.Context(db,kwargs['authorize'],self.key)
            spec=service_provisioning.service_config(item['next_definition'],item['operation_id'],runtime_context=context)
            self.path.write_text(registry.canonical({'name':'app','services':{'development-web':spec},'networks':{}}))
            self.container['Id']='9'*64
            self.container['Config']['Env']=[k+'='+v for k,v in context.values(item['next_definition']).items()]
            return dict(self.binding,container_id=self.container['Id'],definition_digest=managed_services.digest(item['next_definition']),observed=time.time())
        # A second real SQLite connection is used by control, as in the worker.
        database_path=self.db.execute('PRAGMA database_list').fetchone()[2]
        with patch.object(control,'DATABASE',database_path),patch.object(control,'JOURNAL',self.root/'journal'),patch.object(control,'inspect_target',return_value=(self.container,'development-web',[self.path],['compose'])),patch.object(control,'assert_image'),patch.object(control,'effective_projects',return_value={'app':{}}),patch.dict('os.environ',{'DATABASE_INSPECTION_KEY_FILE':str(self.key)}),patch('managed_rollout.rollout',side_effect=rollout),patch.object(control,'recreate') as generic:
            result=control.execute(body)
        generic.assert_not_called();self.assertEqual(result['status'],'succeeded')
        return result
    def test_environment_save_preserves_json_definition_binding_and_next_delivery(self):
        self.save()
        self.assertNotIn('database-fixture',self.path.read_text())
        self.assertIsInstance(json.loads(self.path.read_text()),dict)
        current=managed_services.get(self.db,self.f.sid)
        self.assertEqual(current['binding']['container_id'],self.container['Id'])
        self.assertEqual(current['binding']['definition_digest'],managed_services.digest(current['definition']))
        captured=managed_delivery.capture(self.db,'app','development')
        self.assertEqual(captured['services'][0]['definition']['runtime'],current['definition']['runtime'])
        self.assertEqual(service_runtime.resolve(self.db,'app','development',self.f.sid,current['definition']['runtime']['environment'],key_path=self.key,authorize=lambda:None),{'PATH':'/bin','DB_HOST':'database-fixture'})
    def test_binding_managed_variable_cannot_report_a_silent_environment_edit(self):
        service=copy.deepcopy(self.service)
        service['definition']['runtime']={'database_attachment':{'binding_id':'a'*32,'revision':1,'digest':'b'*64}}
        with patch('application_database_bindings.recorded_environment_keys',return_value=['DB_HOST']),patch('service_runtime_operations.accept') as accept:
            with self.assertRaisesRegex(control.ControlError,'Application database'):
                control.save_managed_environment(self.db,{'changes':{'DB_HOST':'ignored-host'}},self.container,service)
        accept.assert_not_called()

    def test_applied_revision_survives_connection_reload(self):
        self.save()
        current=managed_services.get(self.db,self.f.sid)
        self.assertEqual(service_runtime.status(self.db,'app','development',self.f.sid),{'desired':current['definition']['runtime']['environment'],'applied':current['definition']['runtime']['environment']})
        self.assertIn('DB_HOST=database-fixture',self.container['Config']['Env'])
