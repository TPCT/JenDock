import copy
import json
import unittest
from unittest.mock import patch

import test_service_provisioning as fixture
import managed_rollout
import managed_services
import service_provisioning as provision


class RolloutTests(unittest.TestCase):
    command = fixture.ProvisioningTests.command
    execute = fixture.ProvisioningTests.execute
    # Reuse the deterministic Docker fixture, not the provisioning test cases.
    def setUp(self):
        fixture.ProvisioningTests.setUp(self)
        self.definition['desired_state'] = 'running'
        binding = self.execute()
        self.old_definition = copy.deepcopy(self.definition)
        self.old_container = copy.deepcopy(self.container)
        self.new_definition = copy.deepcopy(self.definition)
        self.new_definition['artifact']['image'] = 'sha256:' + 'e' * 64
        self.new_definition['artifact']['id'] = 'new-artifact'
        self.new_definition['artifact_id'] = 'new-artifact'
        self.item = {'definition': self.old_definition, 'binding': binding,
                     'resources': {'memory_mib':64, 'cpu':.25}, 'next_definition':self.new_definition,
                     'artifact':self.new_definition['artifact'], 'operation_id':'operation-id'}
        self.peers = [{'id':'service-id', 'definition':self.old_definition, 'binding':binding,
                       'effective_resources':self.item['resources'], 'operation_id':'operation-id'}]
        self.calls.clear()
        self.replacement_count = 0
        self.unhealthy = False
        self.uncertain = False
        self.extra = [patch.object(managed_services, 'environment_definitions', side_effect=lambda *a:copy.deepcopy(self.peers)),
                      patch.object(managed_services, 'artifact_for_service', return_value=self.new_definition['artifact']),
                      patch.object(provision, 'command', side_effect=self.rollout_command)]
        for p in self.extra: p.start(); self.addCleanup(p.stop)

    def rollout_command(self, args, **kwargs):
        if args[1:3] == ['image','inspect']:
            return json.dumps([{'Id':args[-1], 'Config':{}}])
        if args[0] == 'compose':
            self.replacement_count += 1
            self.definition = self.new_definition if self.replacement_count == 1 else self.old_definition
            result = self.command(args, **kwargs)
            self.container['Id'] = ('d' if self.replacement_count == 1 else 'f') * 64
            if self.uncertain: raise provision.ProvisioningError('uncertain')
            return result
        result = self.command(args, **kwargs)
        if args[1] == 'start' and self.unhealthy and self.replacement_count == 1:
            self.container['State']['Health']['Status'] = 'unhealthy'
        return result

    def deliver(self):
        return managed_rollout.rollout(self.db, 'delivery-1', self.item, docker='docker', compose='compose', authorize=lambda:None, health_timeout=0)

    def test_rollout_verified_new_image_and_exact_start(self):
        result = self.deliver()
        self.assertEqual(result['image_id'], self.new_definition['artifact']['image'])
        self.assertEqual(result['container_id'], 'd'*64)
        self.assertIn(['docker','start','d'*64], self.calls)
        self.assertEqual(self.replacement_count, 1)
        with self.assertRaisesRegex(ValueError,'reconciliation'): self.deliver()
        self.assertEqual(self.replacement_count, 1)

    def test_stopped_after_build_never_wakes(self):
        self.container['State']['Running'] = False
        with self.assertRaisesRegex(ValueError,'runtime state changed'): self.deliver()
        self.assertEqual(self.replacement_count,0)

    def test_stopped_selection_remains_untouched(self):
        self.container['State']['Running'] = False
        self.item['binding']['state'] = 'stopped'
        result = self.deliver()
        self.assertEqual(result['state'],'stopped')
        self.assertEqual(self.replacement_count,0)

    def test_health_failure_restores_verified_old_image(self):
        self.unhealthy = True
        with self.assertRaises(managed_rollout.RolloutError) as error: self.deliver()
        self.assertEqual(error.exception.restored_binding['image_id'], self.old_definition['artifact']['image'])
        self.assertEqual(self.replacement_count,2)
        self.assertEqual(self.container['State']['Health']['Status'],'healthy')

    def test_ambiguous_create_never_retries_or_rolls_back(self):
        self.uncertain = True
        with self.assertRaisesRegex(ValueError,'uncertain'): self.deliver()
        with self.assertRaisesRegex(ValueError,'reconciliation'): self.deliver()
        self.assertEqual(self.replacement_count,1)

    def test_exact_old_id_mismatch_blocks_before_mutation(self):
        self.container['Id'] = '9'*64
        with self.assertRaisesRegex(ValueError,'identity changed'): self.deliver()
        self.assertEqual(self.replacement_count,0)

    def test_manifest_drift_blocks_before_mutation(self):
        path = self.root/'example/development/compose.json'
        document=json.loads(path.read_text()); document['services']['development-app']['privileged']=True
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError,'manifest drifted'): self.deliver()
        self.assertEqual(self.replacement_count,0)

