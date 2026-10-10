import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
import control
import resource_policy
from store import connect


class DirectLimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.path = self.root / 'db'; self.db = connect(self.path)
    def tearDown(self): self.db.close(); self.tmp.cleanup()

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('direct_limits'), 'Direct limits workflow is required')
        import direct_limits
        return direct_limits

    def body(self):
        return {'action': 'container-limits-save', 'project': 'app', 'container': 'app-web', 'container_id': 'immutable-id',
                'revision': 'config-revision', 'request_id': 'direct-limit-request-123', 'actor': 'owner',
                'limits': {'memory_mib': 128, 'cpu': 0.5}}

    def test_absent_policy_allows_direct_limits_without_creating_ownership(self):
        module = self.module()
        result = module.check(self.db, 'app', 'app-web', self.body()['limits'])
        self.assertFalse(result['configured'])
        self.assertIsNone(result['limits_ceiling'])
        self.assertEqual(resource_policy.read(self.db, 'app')['revision'], 0)

    def test_existing_allocations_are_ceilings_not_bypassed(self):
        module = self.module(); doc = resource_policy.read(self.db, 'app')['document']
        doc['budget'] = {'memory_mib': 256, 'cpu': 1}
        doc['environments']['production'] = {'budget': {'memory_mib': 256, 'cpu': 1}, 'services': {'app-web': {'memory_mib': 128, 'cpu': 0.5}}}
        resource_policy.save(self.db, 'app', 0, doc)
        module.check(self.db, 'app', 'app-web', {'memory_mib': 64, 'cpu': 0.25})
        with self.assertRaises(ValueError): module.check(self.db, 'app', 'app-web', {'memory_mib': 512, 'cpu': 1})
        with self.assertRaises(ValueError): module.check(self.db, 'app', 'other', {'memory_mib': 64, 'cpu': 0.25})

    def test_reservation_serializes_policy_changes_and_replay_payload(self):
        module = self.module(); body = self.body()
        module.admit(self.db, body)
        with self.assertRaises(ValueError): resource_policy.save(self.db, 'app', 0, resource_policy.read(self.db, 'app')['document'])
        self.assertEqual(module.replay(self.db, body)['status'], 'running')
        with self.assertRaises(ValueError): module.replay(self.db, dict(body, limits={'memory_mib': 256, 'cpu': 0.5}))

    def test_direct_apply_preserves_running_and_stopped_state_and_persists(self):
        self.module()
        for running in (True, False):
            with self.subTest(running=running):
                file = self.root / 'compose.yml'
                original = {'services': {'web': {'image': 'app:v1'}, 'sibling': {'image': 'other:v1'}}}
                file.write_text(yaml.safe_dump(original))
                container = {'Id': 'immutable-id', 'Image': 'sha256:image', 'Config': {'Env': []}, 'State': {'Running': running}, 'HostConfig': {}}
                body = self.body(); body['request_id'] += str(running)
                body['revision'] = control.revision([file], container, original)
                after = control.mutate_resources(original, 'web', body['limits'])
                updated = copy.deepcopy(container); updated['Id'] = 'new-id'
                updated['HostConfig'] = {'Memory': 128 * 1048576, 'MemorySwap': 128 * 1048576, 'NanoCpus': 500000000}
                with patch.object(control, 'DATABASE', self.path), patch.object(control, 'ROOT', self.root), patch.object(control, 'JOURNAL', self.root / 'journal'), \
                     patch.object(control, 'inspect_target', return_value=(container, 'web', [file], ['compose'])), \
                     patch.object(control, 'assert_image'), patch.object(control, 'configuration', side_effect=[original, original, original, after]), \
                     patch.object(control, 'recreate') as recreate, patch.object(control, 'wait_runtime', return_value=updated):
                    result = control.execute(body)
                    self.assertEqual(result['status'], 'succeeded')
                    self.assertEqual(control.execute(body)['status'], 'succeeded')
                    self.assertEqual(recreate.call_count, 1)
                    self.assertEqual(recreate.call_args.args[-1], running)
                saved = yaml.safe_load(file.read_text())
                self.assertEqual(saved['services']['web']['mem_limit'], 128 * 1048576)
                self.assertEqual(saved['services']['sibling'], original['services']['sibling'])

    def test_stale_identity_or_revision_never_mutates(self):
        self.module()
        for changed in ('identity', 'revision'):
            with self.subTest(changed=changed):
                file = self.root / 'compose.yml'; config = {'services': {'web': {'image': 'app:v1'}}}
                file.write_text(yaml.safe_dump(config))
                container = {'Id': 'new-id' if changed == 'identity' else 'immutable-id', 'Image': 'image', 'State': {'Running': False}}
                with patch.object(control, 'DATABASE', self.path), patch.object(control, 'JOURNAL', self.root / 'journal'), \
                     patch.object(control, 'inspect_target', return_value=(container, 'web', [file], ['compose'])), \
                     patch.object(control, 'assert_image'), patch.object(control, 'configuration', return_value=config), patch.object(control, 'recreate') as recreate:
                    with self.assertRaises(control.ControlError): control.execute(self.body())
                    recreate.assert_not_called()

    def test_direct_apply_rolls_back_config_and_previous_limits_on_runtime_failure(self):
        self.module()
        file = self.root / 'compose.yml'; original = {'services': {'web': {'image': 'app:v1', 'mem_limit': 67108864, 'cpus': 0.25}}}
        file.write_text(yaml.safe_dump(original)); before = file.read_text()
        container = {'Id': 'immutable-id', 'Image': 'image', 'Config': {'Env': []}, 'State': {'Running': False},
                     'HostConfig': {'Memory': 67108864, 'NanoCpus': 250000000}}
        body = self.body(); body['revision'] = control.revision([file], container, original)
        after = control.mutate_resources(original, 'web', body['limits'])
        with patch.object(control, 'DATABASE', self.path), patch.object(control, 'ROOT', self.root), patch.object(control, 'JOURNAL', self.root / 'journal'), \
             patch.object(control, 'inspect_target', return_value=(container, 'web', [file], ['compose'])), patch.object(control, 'assert_image'), \
             patch.object(control, 'configuration', side_effect=[original, original, original, after, original]), \
             patch.object(control, 'recreate') as recreate, patch.object(control, 'wait_runtime', side_effect=[control.ControlError('fixture failure'), container]):
            self.assertEqual(control.execute(body)['status'], 'rolled-back')
            self.assertEqual(recreate.call_count, 2)
            self.assertFalse(recreate.call_args.args[-1])
        self.assertEqual(file.read_text(), before)
        self.assertFalse((self.root / 'journal').exists())

    def test_invalid_limits_and_changed_actor_cannot_replay(self):
        module = self.module()
        for value in ({'memory_mib': 128, 'cpu': True}, {'memory_mib': 0, 'cpu': 1}, {'memory_mib': 128, 'cpu': float('nan')}, {'memory_mib': 128, 'cpu': 1, 'disk_gib': 4}):
            with self.subTest(value=value), self.assertRaises(ValueError): module.check(self.db, 'app', 'app-web', value)
        module.admit(self.db, self.body())
        with self.assertRaises(ValueError): module.replay(self.db, dict(self.body(), actor='different-actor'))


if __name__ == '__main__': unittest.main()
