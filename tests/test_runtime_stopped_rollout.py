import copy
import json
from pathlib import Path
import managed_services
import service_provisioning
import unittest
from unittest.mock import patch
import managed_rollout
import test_managed_rollout as fixture


class StoppedRuntimeRolloutTests(unittest.TestCase):
    def test_settings_replacement_preserves_stopped_state_without_start(self):
        f=fixture.RolloutTests();f.setUp();self.addCleanup(f.doCleanups)
        f.old_definition['artifact']['id']='artifact-id'
        f.container['Config']['Labels'][service_provisioning.PREFIX+'definition']=managed_services.digest(f.old_definition)
        path=Path(f.item['binding']['compose_files'][0]);manifest=json.loads(path.read_text())
        manifest['services']['development-app']=service_provisioning.service_config(f.old_definition,'operation-id')
        path.write_text(json.dumps(manifest))
        f.item['runtime_change']=True
        f.item['binding']['state']='stopped'
        f.container['State']={'Running':False,'StartedAt':'2026-01-01T00:00:00Z'}
        f.peers[0]['binding']['state']='stopped'
        f.new_definition=copy.deepcopy(f.old_definition)
        f.item['next_definition']=f.new_definition;f.item['artifact']=f.new_definition['artifact']
        with patch('managed_services.artifact_for_service',return_value=f.new_definition['artifact']):
            result=f.deliver()
        self.assertEqual(result['state'],'stopped')
        self.assertFalse(f.container['State']['Running'])
        self.assertFalse(any(call[1]=='start' for call in f.calls))
        self.assertNotEqual(result['container_id'],f.item['binding']['container_id'])
