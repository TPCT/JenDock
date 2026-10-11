"""Managed lifecycle must persist observed state for subsequent settings/deploys."""
import unittest
from unittest.mock import patch
import control
import managed_services
import managed_lifecycle
import test_managed_environment_regression as fixtures


class ManagedLifecycleRegressionTests(unittest.TestCase):
    def test_stop_updates_managed_state_and_binding_after_verified_effect(self):
        f=fixtures.ManagedEnvironmentRegressionTests();f.setUp();self.addCleanup(f.doCleanups)
        body={'action':'container-stop','project':'app','container':f.binding['container_name'],
              'container_id':f.container['Id'],'request_id':'managed-lifecycle-stop-001'}
        f.container['State']['StartedAt']='2026-10-11T01:00:00Z'
        stopped=dict(f.container,State={'Running':False,'Status':'exited','StartedAt':'2026-10-11T01:00:00Z'})
        observed=[f.container]
        def stop(*args,**kwargs):observed[0]=stopped
        database_path=f.db.execute('PRAGMA database_list').fetchone()[2]
        with patch.object(control,'DATABASE',database_path),patch.object(control,'JOURNAL',f.root/'journal'),patch.object(control,'inspect_target',return_value=(f.container,f.binding['compose_service'],[f.path],['compose'])),patch.object(control,'effective_projects',return_value={'app':{}}),patch('service_provisioning.command',side_effect=stop),patch.object(managed_lifecycle,'observe',side_effect=lambda *a,**k:observed[0]):
            result=control.execute(body)
            with patch.object(control,'inspect_target',side_effect=AssertionError('Replay must not inspect or mutate')):
                self.assertEqual(control.execute(dict(body)),result)
                for field,value in [('container_id','f'*64),('actor','different-owner'),('revision','different-revision'),('environment','production'),('action','container-start'),('project','other'),('container','replacement')]:
                    with self.subTest(field=field),self.assertRaises(control.ControlError):
                        control.execute(dict(body,**{field:value}))
        self.assertEqual(result['status'],'succeeded')
        current=managed_services.get(f.db,f.f.sid)
        self.assertEqual(current['state'],'stopped')
        self.assertEqual(current['binding']['state'],'stopped')
