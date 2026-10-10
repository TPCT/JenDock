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

    def bound_container(self):
        import registry
        recipe = {'compose_project': 'app', 'compose_files': ['/managed/compose.yml'], 'services': ['web']}
        registry.import_legacy(self.db, {'app': {'repository': 'TPCT/app', 'repository_id': 1, 'branch': 'main', 'recipe': recipe}}, [])
        registry.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        return {'Id': 'a' * 64, 'Name': '/app-web', 'Config': {'Labels': {
            'com.docker.compose.project': 'app', 'com.docker.compose.service': 'web',
            'com.docker.compose.project.config_files': '/managed/compose.yml'}}}

    def configured_budget(self):
        doc = resource_policy.read(self.db, 'app')['document']
        doc['budget'] = {'memory_mib': 256, 'cpu': 1}
        doc['environments']['production']['budget'] = dict(doc['budget'])
        resource_policy.save(self.db, 'app', 0, doc)
        return doc

    def test_verified_owner_direct_save_creates_then_increases_reservation(self):
        module = self.module(); actual = self.bound_container(); self.configured_budget()
        self.assertTrue(module.context(self.db, 'app', 'app-web', actual)['editable'])
        for index, memory in enumerate((128, 192)):
            body = dict(self.body(), request_id='reservation-operation-' + str(index), policy_revision=index + 1,
                        limits={'memory_mib': memory, 'cpu': 0.5})
            transition = module.admit(self.db, body, actual)
            self.assertEqual(resource_policy.read(self.db, 'app')['revision'], index + 1)
            module.commit(self.db, transition); module.commit(self.db, transition)
            record = resource_policy.read(self.db, 'app')
            self.assertEqual(record['document']['environments']['production']['services']['app-web'], body['limits'])
            self.db.execute("UPDATE control_runs SET status='succeeded' WHERE request_id=?", (body['request_id'],))
        with self.assertRaises(ValueError): module.check(self.db, 'app', 'app-web', {'memory_mib': 300, 'cpu': 0.5}, actual)
        module.rollback(self.db, transition); module.rollback(self.db, transition)
        self.assertEqual(resource_policy.read(self.db, 'app')['document']['environments']['production']['services']['app-web']['memory_mib'], 128)

    def test_unassigned_and_stale_review_cannot_create_reservation(self):
        module = self.module(); actual = self.bound_container(); self.configured_budget()
        with self.assertRaises(ValueError): module.admit(self.db, self.body(), actual)
        body = dict(self.body(), policy_revision=1)
        wrong = copy.deepcopy(actual); wrong['Config']['Labels']['com.docker.compose.project'] = 'foreign'
        with self.assertRaises(ValueError): module.admit(self.db, body, wrong)
        self.assertEqual(self.db.execute('SELECT count(*) FROM control_runs').fetchone()[0], 0)
        self.assertEqual(resource_policy.read(self.db, 'app')['document']['environments']['production']['services'], {})

    def test_sibling_reservations_bound_available_capacity_and_wrong_env_record_blocks(self):
        module = self.module(); actual = self.bound_container(); doc = self.configured_budget()
        doc['environments']['production']['services']['app-peer'] = {'memory_mib': 192, 'cpu': 0.5}
        resource_policy.save(self.db, 'app', 1, doc)
        self.assertEqual(module.context(self.db, 'app', 'app-web', actual)['limits_ceiling']['memory_mib'], 64)
        with self.assertRaises(ValueError): module.check(self.db, 'app', 'app-web', self.body()['limits'], actual)

    def test_policy_transition_commit_rejects_changed_owner_and_rollback_preserves_concurrent_policy(self):
        module = self.module(); actual = self.bound_container(); self.configured_budget()
        transition = module.admit(self.db, dict(self.body(), policy_revision=1), actual)
        self.db.execute("UPDATE registry_environments SET revision=revision+1 WHERE project='app' AND name='production'")
        with self.assertRaises(ValueError): module.commit(self.db, transition)
        self.assertEqual(resource_policy.read(self.db, 'app')['revision'], 1)

    def test_failure_after_policy_commit_restores_runtime_policy_and_replay(self):
        import provenance_store
        for failure in ('proof', 'journal'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); path = root / 'db'; db = connect(path)
                import registry
                recipe = {'compose_project': 'app', 'compose_files': [str(root / 'compose.yml')], 'services': ['web']}
                registry.import_legacy(db, {'app': {'repository': 'TPCT/app', 'repository_id': 1, 'branch': 'main', 'recipe': recipe}}, [])
                registry.update_project(db, 'app', 1, {'legacy_binding': 'production'})
                policy = resource_policy.read(db, 'app')['document']; policy['budget'] = {'memory_mib': 256, 'cpu': 1}
                policy['environments']['production']['budget'] = dict(policy['budget']); resource_policy.save(db, 'app', 0, policy)
                file = root / 'compose.yml'; original = {'services': {'web': {'image': 'app:v1'}}}; file.write_text(yaml.safe_dump(original))
                container = {'Id': 'a' * 64, 'Name': '/app-web', 'Image': 'image', 'State': {'Running': False},
                             'HostConfig': {}, 'Config': {'Env': [], 'Labels': {'com.docker.compose.project': 'app',
                             'com.docker.compose.service': 'web', 'com.docker.compose.project.config_files': str(file)}}}
                body = dict(self.body(), container_id=container['Id'], policy_revision=1, revision=control.revision([file], container, original))
                after = control.mutate_resources(original, 'web', body['limits']); updated = copy.deepcopy(container)
                updated['HostConfig'] = {'Memory': 128 * 1048576, 'MemorySwap': 128 * 1048576, 'NanoCpus': 500000000}
                journal = root / 'journal'; unlink = Path.unlink; failures = []
                def fail_once(target, *args, **kwargs):
                    if target == journal and failure == 'journal' and not failures:
                        failures.append(True); raise OSError('fixture unlink failed')
                    return unlink(target, *args, **kwargs)
                def proof_step(*args, **kwargs):
                    self.assertIn('app-web', resource_policy.read(db, 'app')['document']['environments']['production']['services'])
                    if failure == 'proof': raise ValueError('fixture proof failed')
                    return None
                with patch.object(control, 'DATABASE', path), patch.object(control, 'ROOT', root), patch.object(control, 'JOURNAL', journal), \
                     patch.object(control, 'inspect_target', return_value=(container, 'web', [file], ['compose'])), \
                     patch.object(control, 'assert_image'), patch.object(control, 'configuration', side_effect=[original, original, original, after, original]), \
                     patch.object(control, 'recreate') as recreate, patch.object(control, 'wait_runtime', side_effect=[updated, container]), \
                     patch.object(provenance_store, 'prepare', side_effect=proof_step), patch.object(Path, 'unlink', fail_once):
                    self.assertEqual(control.execute(body)['status'], 'rolled-back')
                    self.assertEqual(control.execute(body)['status'], 'rolled-back')
                    self.assertEqual(recreate.call_count, 2)
                self.assertEqual(resource_policy.read(db, 'app')['document'], policy)
                self.assertEqual(yaml.safe_load(file.read_text()), original)
                self.assertFalse(journal.exists()); db.close()


if __name__ == '__main__': unittest.main()
