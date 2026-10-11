import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import control
import ownership
import registry
from store import connect


class RegisteredOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = connect(self.root / 'db')
        self.addCleanup(self.db.close)
        registry.create_project(self.db, 'example', {'repository': 'TPCT/example'})
        self.files = {}
        for env in registry.ENVIRONMENTS:
            path = self.root / (env + '.yml')
            path.write_text('services: {}')
            self.files[env] = str(path)

    def bind(self, env, *, project='example', service='app', files=None, **extra):
        recipe = dict(compose_project='example', compose_files=files or [self.files[env]], services=[service], **extra)
        document = registry.canonical(recipe)
        result = self.db.execute('INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,0)',
                                (project, hashlib.sha256(document.encode()).hexdigest(), document))
        self.db.execute('UPDATE registry_environments SET recipe_revision=? WHERE project=? AND name=?',
                        (result.lastrowid, project, env))
        return recipe

    def resolve(self, env='development', project='example'):
        return ownership.resolve(self.db, project, 'example', 'app', [self.files[env]])

    def test_both_environments_select_independent_recipe_without_legacy_binding_or_prefix(self):
        for env in registry.ENVIRONMENTS: self.bind(env)
        for env in registry.ENVIRONMENTS:
            actual = [{'Id': 'a' * 64, 'Name': '/example-app-1', 'Config': {'Labels': {
                'com.docker.compose.project': 'example', 'com.docker.compose.service': 'app',
                'com.docker.compose.project.config_files': self.files[env]}}}]
            with patch.object(control, 'DATABASE', str(self.root / 'db')), patch.object(control, 'ROOT', self.root), \
                 patch.object(control, 'effective_projects', return_value={'example': {'container_prefixes': []}}), \
                 patch.object(control, 'command', return_value=json.dumps(actual)):
                result, service, paths, args = control.inspect_target('example', 'example-app-1')
                self.assertEqual(result['_monitor_environment'], env)
                self.assertEqual(paths, [Path(self.files[env])])
                self.assertIn(self.files[env], args)
        self.assertIsNone(registry.get_project(self.db, 'example')['legacy_binding'])

    def test_foreign_exact_owner_rejected_even_with_matching_legacy_project(self):
        self.bind('development')
        with self.assertRaisesRegex(ValueError, 'another registered project'):
            self.resolve(project='pretend')

    def test_multiple_exact_environment_claims_rejected(self):
        self.bind('development')
        self.bind('production', files=[self.files['development']], owned_services=['app'])
        with self.assertRaisesRegex(ValueError, 'ambiguous'): self.resolve()

    def test_registered_service_changed_files_cannot_fall_back_to_legacy(self):
        self.bind('development')
        with self.assertRaisesRegex(ValueError, 'configuration does not match'):
            self.resolve('production')

    def test_unregistered_service_from_claimed_files_cannot_fall_back(self):
        self.bind('development', service='allowed')
        with self.assertRaisesRegex(ValueError, 'configuration does not match'): self.resolve()

    def test_invalid_proof_rejected_even_with_exact_canonical_files(self):
        self.bind('development', service_provenance={'app': {}})
        with self.assertRaisesRegex(ValueError, 'proof is invalid'): self.resolve()

    def test_missing_foreign_recipe_reference_is_integrity_failure(self):
        self.db.execute("UPDATE registry_environments SET recipe_revision=123 WHERE name='development'")
        with self.assertRaisesRegex(ValueError, 'integrity'): self.resolve()

    def test_digest_mismatch_cannot_authorize_or_fall_back(self):
        recipe = {'compose_project': 'example', 'compose_files': [self.files['development']], 'services': ['app']}
        row = self.db.execute('INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,0)',
                              ('example', '0' * 64, registry.canonical(recipe)))
        self.db.execute("UPDATE registry_environments SET recipe_revision=? WHERE name='development'", (row.lastrowid,))
        with self.assertRaisesRegex(ValueError, 'integrity'): self.resolve()

    def test_invalid_claim_group_does_not_become_unclaimed_legacy(self):
        row = self.db.execute('INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,0)',
                              ('example', hashlib.sha256(b'{}').hexdigest(), '{}'))
        self.db.execute("UPDATE registry_environments SET recipe_revision=? WHERE name='development'", (row.lastrowid,))
        with self.assertRaisesRegex(ValueError, 'Compose ownership is invalid'): self.resolve()

    def test_no_registered_claim_returns_legacy_fallback(self):
        self.assertIsNone(self.resolve())
