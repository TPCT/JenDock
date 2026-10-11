"""Managed resource control uses the immutable transition, never generic YAML edits.

SQLite policy/registry/receipts are real; Docker observation/replacement is scripted.
"""
import json
import time
import unittest
from unittest.mock import patch
import control
import managed_services
import registry
import resource_policy
import service_runtime
import test_managed_environment_regression as fixtures


class ManagedResourceControlTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ManagedEnvironmentRegressionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db
        recipe=json.loads(self.db.execute('SELECT r.document FROM registry_environments e JOIN registry_recipes r ON r.id=e.recipe_revision WHERE e.project=? AND e.name=?',('app','development')).fetchone()[0])
        self.f.container['Name']='/'+self.f.binding['container_name']
        self.f.container['Config']['Labels'].update({
            'com.docker.compose.project':recipe['compose_project'],
            'com.docker.compose.service':self.f.binding['compose_service'],
            'com.docker.compose.project.config_files':','.join(recipe['compose_files'])})

    def exercise(self,rollback=False):
        f=self.f
        policy=resource_policy.read(self.db,'app')
        old=policy['document']['environments']['development']['services'][f.binding['container_name']]
        wanted={'memory_mib':old['memory_mib']-16,'cpu':old['cpu']/2}
        body={'action':'container-limits-save','project':'app','container':f.binding['container_name'],
              'container_id':f.container['Id'],'request_id':'managed-resource-regression-001',
              'limits':wanted,'policy_revision':policy['revision'],
              'revision':control.revision([f.path],f.container,json.loads(f.path.read_text()))}
        def replace(db,operation,item,**kwargs):
            self.assertEqual(item['resources'],old)
            self.assertEqual(item['next_resources'],wanted)
            if rollback:
                from managed_rollout import RolloutError
                raise RolloutError('Candidate failed',restored_binding=dict(f.binding,container_id='7'*64,observed=time.time()))
            return dict(f.binding,container_id='8'*64,
                        definition_digest=managed_services.digest(item['next_definition']),observed=time.time())
        database_path=self.db.execute('PRAGMA database_list').fetchone()[2]
        with patch.object(control,'DATABASE',database_path),patch.object(control,'JOURNAL',f.root/'journal'),patch.object(control,'inspect_target',return_value=(f.container,f.binding['compose_service'],[f.path],['compose'])),patch.object(control,'assert_image'),patch.object(control,'effective_projects',return_value={'app':{}}),patch.dict('os.environ',{'DATABASE_INSPECTION_KEY_FILE':str(f.key)}),patch('managed_rollout.rollout',side_effect=replace),patch.object(control,'recreate') as generic:
            result=control.execute(body)
            status='rolled-back' if rollback else 'succeeded'
            self.assertEqual(result['status'],status,result)
            self.assertEqual(control.execute(body),{'status':status,'message':result['message']})
        generic.assert_not_called()
        current=managed_services.get(self.db,f.f.sid)
        if rollback:
            self.assertEqual(current['binding']['container_id'],'7'*64)
            self.assertEqual(current['definition'],f.service['definition'])
            self.assertEqual(resource_policy.read(self.db,'app'),policy)
            return
        self.assertEqual(current['binding']['container_id'],'8'*64)
        self.assertEqual(current['definition']['resources'],wanted)
        self.assertEqual(resource_policy.read(self.db,'app')['document']['environments']['development']['services'][f.binding['container_name']],wanted)
        self.assertEqual(service_runtime.resolve(self.db,'app','development',f.f.sid,current['definition']['runtime']['environment'],key_path=f.key,authorize=lambda:None),{'PATH':'/bin'})
        self.assertIsInstance(json.loads(f.path.read_text()),dict)

    def test_limits_save_advances_policy_and_binding_without_generic_recreate(self):
        self.exercise()

    def test_failed_candidate_restores_old_policy_definition_and_current_identity(self):
        self.exercise(rollback=True)
