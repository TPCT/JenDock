import copy
import json
import unittest

from compose_reconciliation import ReconciliationError, compare_service, reconcile


class ReconciliationTests(unittest.TestCase):
    def config(self):
        return {'name': 'app', 'services': {
            'web': {'image': 'app:v1', 'environment': {'TOKEN': 'private-value'},
                    'depends_on': {'db': {'condition': 'service_started'}}, 'networks': {'default': {}},
                    'secrets': [{'source': 'password'}], 'configs': [{'source': 'settings'}]},
            'db': {'image': 'postgres:17', 'volumes': [{'type': 'volume', 'source': 'data', 'target': '/data'}]},
            'unrelated': {'image': 'unused:v1'}}, 'networks': {'default': {'name': 'app_default'}},
            'volumes': {'data': {'name': 'app_data'}}, 'secrets': {'password': {'file': '/managed/password'}},
            'configs': {'settings': {'file': '/managed/settings'}}}

    def test_equal_selected_closure_ignores_only_invocation_metadata_and_unrelated_services(self):
        left = self.config(); right = copy.deepcopy(left)
        right['name'] = 'different-invocation'; right['version'] = '3.9'
        right['services']['unrelated']['image'] = 'new:v2'
        result = compare_service(left, right, 'web')
        self.assertTrue(result['equivalent']); self.assertEqual(len(result['digest']), 64)
        self.assertNotIn('private-value', json.dumps(result))

    def test_semantic_changes_and_referenced_resources_are_rejected_without_values(self):
        for category in ('environment', 'command', 'ports', 'volumes', 'networks', 'secrets', 'configs', 'dependency', 'global'):
            left = self.config(); right = copy.deepcopy(left)
            if category == 'environment': right['services']['web']['environment']['TOKEN'] = 'new-private-value'
            elif category in ('command', 'ports'): right['services']['web'][category] = ['private-value']
            elif category == 'dependency': right['services']['db']['image'] = 'different'
            elif category == 'global': right['unknown-global'] = 'private-value'
            else: right[category][next(iter(right[category]))]['private-key'] = 'private-value'
            result = compare_service(left, right, 'web')
            self.assertFalse(result['equivalent'], category); self.assertIsNone(result['digest'])
            self.assertNotIn('private', json.dumps(result))

    def test_all_service_reference_forms_include_dependency_configuration(self):
        for field, value in [('network_mode', 'service:db'), ('ipc', 'service:db'), ('pid', 'service:db'),
                             ('links', ['db:alias']), ('volumes_from', ['db:ro'])]:
            left = self.config(); left['services']['web'].pop('depends_on')
            left['services']['web'][field] = value
            right = copy.deepcopy(left); right['services']['db']['image'] = 'changed'
            self.assertFalse(compare_service(left, right, 'web')['equivalent'], field)

    def test_missing_definitions_unresolved_mounts_and_external_references_fail_closed(self):
        for change in [lambda c: c['services'].pop('db'), lambda c: c['volumes'].clear(),
                       lambda c: c['services']['web'].update(volumes=['data:/data']),
                       lambda c: c['services']['web'].update(volumes_from=['container:foreign']),
                       lambda c: c['services']['web'].update(extends={'file': 'private-value'})]:
            config = self.config(); change(config)
            with self.assertRaises(ReconciliationError) as error: compare_service(config, self.config(), 'web')
            self.assertNotIn('private-value', str(error.exception))

    def test_proof_binds_order_context_and_semantic_digest(self):
        def proof(files, context='a' * 64):
            return reconcile('web', self.config(), self.config(), files, ['/managed/current.yml'],
                             compose_project='app', environment_context_digest=context)['proof']
        first = proof(['/managed/base.yml', '/managed/override.yml'])
        self.assertNotEqual(first['fingerprint'], proof(['/managed/override.yml', '/managed/base.yml'])['fingerprint'])
        self.assertNotEqual(first['fingerprint'], proof(['/managed/base.yml', '/managed/override.yml'], 'b' * 64)['fingerprint'])
        with self.assertRaises(ValueError): proof(['/managed/base.yml', '/managed/base.yml'])
        with self.assertRaises(ValueError): proof(['relative.yml'])
        with self.assertRaises(ValueError): proof(['/managed/base.yml'], '')

    def test_top_level_extensions_do_not_mask_expanded_service_changes(self):
        left = self.config(); right = copy.deepcopy(left)
        right['x-sibling-template'] = {'environment': {'TOKEN': 'new-private-value'}}
        unchanged = compare_service(left, right, 'web')
        self.assertTrue(unchanged['equivalent'])
        self.assertEqual(unchanged['digest'], compare_service(left, left, 'web')['digest'])
        right['services']['web']['environment']['TOKEN'] = 'new-private-value'
        changed = compare_service(left, right, 'web')
        self.assertFalse(changed['equivalent'])
        self.assertIn('services[*].environment', changed['mismatches'])
        self.assertNotIn('private-value', json.dumps(changed))

    def test_non_extension_globals_and_nested_extensions_remain_compared(self):
        for field in ('unknown-global', 'X-template', 'x_template'):
            right = self.config(); right[field] = {'value': 'hidden'}
            self.assertEqual(compare_service(self.config(), right, 'web')['mismatches'], ['global'])
        right = self.config(); right['services']['web']['x-runtime-extension'] = 'hidden'
        self.assertFalse(compare_service(self.config(), right, 'web')['equivalent'])
