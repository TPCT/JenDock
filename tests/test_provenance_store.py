import copy
import json
import sqlite3
import unittest
from unittest.mock import Mock

import registry
from compose_reconciliation import reconcile
from provenance import ProvenanceError
from provenance_store import prepare, commit, rollback


class ProvenanceStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None); self.db.row_factory = sqlite3.Row
        self.config = {'services': {'web': {'image': 'app:v1', 'network_mode': 'none'}}}
        self.recipe = {'compose_project': 'app', 'compose_files': ['/managed/current.yml'], 'services': ['web'],
                       'private_setting': 'never-journal-this-value'}
        self.recipe['service_provenance'] = {'web': self.proof('a')}
        registry.import_legacy(self.db, {'app': {'repository': 'TPCT/app', 'repository_id': 1, 'branch': 'main', 'recipe': self.recipe}}, [])
        registry.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        self.before = registry.get_project(self.db, 'app')

    def tearDown(self): self.db.close()

    def proof(self, char):
        return reconcile('web', self.config, self.config, ['/managed/old.yml'], self.recipe['compose_files'],
                         compose_project='app', environment_context_digest=char * 64)['proof']

    def transition(self): return prepare(self.db, 'app', self.recipe, lambda *args: self.proof('b'))

    def test_prepare_is_read_only_serializable_and_commit_rollback_are_idempotent(self):
        transition = self.transition()
        self.assertNotIn('never-journal', json.dumps(transition))
        transition = json.loads(json.dumps(transition))
        self.assertEqual(registry.get_project(self.db, 'app'), self.before)
        rollback(self.db, transition)
        commit(self.db, transition); committed = registry.get_project(self.db, 'app')
        commit(self.db, transition); self.assertEqual(registry.get_project(self.db, 'app'), committed)
        env = committed['environments']['production']
        new = json.loads(self.db.execute('SELECT document FROM registry_recipes WHERE id=?', (env['recipe_revision'],)).fetchone()[0])
        self.assertEqual(new['private_setting'], self.recipe['private_setting'])
        rollback(self.db, transition); restored = registry.get_project(self.db, 'app')
        rollback(self.db, transition); self.assertEqual(registry.get_project(self.db, 'app'), restored)
        self.assertEqual(restored['environments']['production']['recipe_revision'], self.before['environments']['production']['recipe_revision'])
        self.assertEqual(restored['revision'], self.before['revision'] + 2)
        self.assertEqual(restored['environments']['development'], self.before['environments']['development'])

    def test_concurrent_preference_change_blocks_commit_without_new_recipe(self):
        transition = self.transition(); env = self.before['environments']['production']
        registry.update_environment(self.db, 'app', 'production', env['revision'], {'automatic': False})
        count = self.db.execute('SELECT count(*) FROM registry_recipes').fetchone()[0]
        with self.assertRaises(ProvenanceError): commit(self.db, transition)
        self.assertEqual(self.db.execute('SELECT count(*) FROM registry_recipes').fetchone()[0], count)
        rollback(self.db, transition)
        self.assertFalse(registry.get_environment(self.db, 'app', 'production')['automatic'])

    def test_concurrent_preferences_after_commit_block_rollback_and_are_preserved(self):
        transition = self.transition(); commit(self.db, transition)
        env = registry.get_environment(self.db, 'app', 'production')
        registry.update_environment(self.db, 'app', 'production', env['revision'], {'branch': 'new-branch'})
        with self.assertRaises(ProvenanceError): rollback(self.db, transition)
        self.assertEqual(registry.get_environment(self.db, 'app', 'production')['branch'], 'new-branch')

    def test_resolver_failure_stale_recipe_and_identity_change_never_write(self):
        for resolver in (Mock(side_effect=ValueError('secret-value')), Mock(return_value={'equivalent': False, 'proof': self.proof('b')})):
            with self.assertRaises(ProvenanceError) as error: prepare(self.db, 'app', self.recipe, resolver)
            self.assertNotIn('secret-value', str(error.exception))
        wrong = copy.deepcopy(self.recipe); wrong['compose_files'] = ['/managed/wrong.yml']
        with self.assertRaises(ProvenanceError): prepare(self.db, 'app', wrong, Mock())
        self.assertEqual(registry.get_project(self.db, 'app'), self.before)
        self.assertIsNone(prepare(self.db, 'app', {'compose_files': ['/managed/current.yml']}, Mock()))
        self.assertIsNone(prepare(self.db, 'app', self.recipe, lambda *args: self.proof('a')))

    def test_two_prepared_transitions_cannot_both_commit(self):
        first = self.transition()
        second = prepare(self.db, 'app', self.recipe, lambda *args: self.proof('c'))
        commit(self.db, first)
        with self.assertRaises(ProvenanceError): commit(self.db, second)
        with self.assertRaises(ProvenanceError): rollback(self.db, second)

    def actual(self):
        return {'Id': 'd' * 64, 'Config': {'Labels': {
            'com.docker.compose.project': 'app', 'com.docker.compose.service': 'web',
            'com.docker.compose.project.config_files': '/managed/current.yml'}}}

    def test_verified_recreation_rebases_only_selected_original_invocation(self):
        def resolver(recipe, service, files):
            self.assertEqual(files, recipe['compose_files'])
            return reconcile(service, self.config, self.config, files, recipe['compose_files'],
                             compose_project='app', environment_context_digest='b' * 64)
        transition = prepare(self.db, 'app', self.recipe, resolver, recreated={'web': self.actual()})
        self.assertEqual(transition['rebased'], ['web'])
        self.assertEqual(transition['proofs']['web']['observed_files'], self.recipe['compose_files'])
        commit(self.db, transition)
        rollback(self.db, transition)
        self.assertEqual(registry.get_environment(self.db, 'app', 'production')['recipe_revision'], self.before['environments']['production']['recipe_revision'])

    def test_rebase_rejects_wrong_full_identity_or_compose_labels(self):
        for field, value in [('Id', 'd' * 12), ('com.docker.compose.project', 'other'),
                             ('com.docker.compose.service', 'other'),
                             ('com.docker.compose.project.config_files', '/managed/other.yml')]:
            actual = self.actual()
            if field == 'Id': actual[field] = value
            else: actual['Config']['Labels'][field] = value
            resolver = Mock()
            with self.subTest(field=field), self.assertRaises(ProvenanceError):
                prepare(self.db, 'app', self.recipe, resolver, recreated={'web': actual})
            resolver.assert_not_called()
        self.assertEqual(registry.get_project(self.db, 'app'), self.before)

    def test_unlisted_observed_invocation_change_is_rejected(self):
        changed = reconcile('web', self.config, self.config, self.recipe['compose_files'], self.recipe['compose_files'],
                            compose_project='app', environment_context_digest='b' * 64)['proof']
        with self.assertRaises(ProvenanceError):
            prepare(self.db, 'app', self.recipe, lambda *args: changed)
