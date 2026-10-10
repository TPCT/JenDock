import copy
import hashlib
import json
import unittest
from unittest.mock import Mock

from compose_reconciliation import reconcile
from provenance import matches, validate, validate_proof, revalidate, ProvenanceError


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.config = {'services': {'web': {'image': 'app:v1', 'network_mode': 'none'}}}
        self.recipe = {'compose_project': 'app', 'compose_files': ['/managed/base.yml', '/managed/current.yml'], 'services': ['web']}
        self.proof = reconcile('web', self.config, self.config, ['/managed/old.yml'], self.recipe['compose_files'],
                               compose_project='app', environment_context_digest='a' * 64)['proof']
        self.recipe['service_provenance'] = {'web': self.proof}

    def test_registered_original_and_canonical_match_but_unknown_order_does_not(self):
        for files in (self.proof['observed_files'], self.proof['canonical_files']):
            self.assertTrue(matches(self.recipe, 'app', 'web', files))
        for files in (self.proof['canonical_files'][::-1], ['/managed/other.yml'], ['/managed/old.yml'] * 2):
            self.assertFalse(matches(self.recipe, 'app', 'web', files))
        self.assertFalse(matches(self.recipe, 'other', 'web', self.proof['observed_files']))
        self.assertFalse(matches(self.recipe, 'app', 'db', self.proof['observed_files']))

    def test_no_provenance_retains_exact_canonical_behavior_and_owned_allowlist(self):
        del self.recipe['service_provenance']
        self.assertIsNone(validate(self.recipe, 'web'))
        self.assertTrue(matches(self.recipe, 'app', 'web', self.recipe['compose_files']))
        self.assertFalse(matches(self.recipe, 'app', 'web', ['/managed/old.yml']))
        self.recipe['owned_services'] = ['web', 'db']
        self.assertTrue(matches(self.recipe, 'app', 'db', self.recipe['compose_files']))
        callback = Mock(); self.assertIsNone(revalidate(self.recipe, 'web', callback)); callback.assert_not_called()

    def test_corrupt_present_proof_blocks_even_canonical_matching(self):
        for field, value in [('service', 'other'), ('compose_project', 'other'), ('semantic_digest', 'b' * 64),
                             ('schema_version', True), ('fingerprint', 'bad'), ('canonical_files', ['/wrong.yml']),
                             ('observed_files', ['/managed/old.yml'] * 2)]:
            recipe = copy.deepcopy(self.recipe); recipe['service_provenance']['web'][field] = value
            with self.subTest(field=field), self.assertRaises(ProvenanceError): validate(recipe, 'web')
            self.assertFalse(matches(recipe, 'app', 'web', self.recipe['compose_files']))
        self.recipe['service_provenance'] = None
        self.assertFalse(matches(self.recipe, 'app', 'web', self.recipe['compose_files']))

    def test_revalidation_always_resolves_original_files_and_returns_private_copy(self):
        resolver = Mock(return_value={'equivalent': True, 'proof': self.proof})
        result = revalidate(self.recipe, 'web', resolver)
        self.assertEqual(resolver.call_args.args[2], ['/managed/old.yml'])
        self.assertEqual(result, self.proof)
        result['observed_files'].append('/modified.yml')
        self.assertEqual(self.proof['observed_files'], ['/managed/old.yml'])

    def test_revalidation_rejects_valid_changed_proof_and_sanitizes_callback_errors(self):
        changed = copy.deepcopy(self.proof); changed['environment_context_digest'] = 'b' * 64
        changed['fingerprint'] = hashlib.sha256(json.dumps({k: v for k, v in changed.items() if k != 'fingerprint'}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        validate_proof(self.recipe, 'web', changed)
        for resolver in (Mock(return_value=changed), Mock(return_value={'equivalent': False, 'proof': self.proof}),
                         Mock(side_effect=RuntimeError('secret-material'))):
            with self.assertRaises(ProvenanceError) as error: revalidate(self.recipe, 'web', resolver)
            self.assertNotIn('secret-material', str(error.exception))
