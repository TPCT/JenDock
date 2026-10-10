import copy
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite'
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def registry(self):
        self.assertIsNotNone(importlib.util.find_spec('registry'), 'Registry implementation is required')
        import registry
        return registry

    def legacy(self):
        return {'app': {'repository': 'TPCT/app', 'repository_id': 12, 'label': 'Application',
                        'branch': 'main', 'environment': 'production', 'automatic': True,
                        'container_prefixes': ['app-'], 'recipe': {'services': ['web'],
                        'compose_project': 'app', 'env_file': '/managed/app/.env'}}}

    def test_import_is_durable_one_time_and_does_not_classify_legacy_runtime(self):
        r = self.registry()
        result = r.import_legacy(self.db, self.legacy(), [{'project': 'app', 'branch': 'saved', 'automatic': 0}])
        self.assertEqual(result['imported'], ['app'])
        project = r.get_project(self.db, 'app')
        self.assertIsNone(project['legacy_binding'])
        self.assertEqual(project['legacy_settings']['branch'], 'saved')
        self.assertFalse(project['legacy_settings']['automatic'])
        self.assertEqual(set(project['environments']), {'development', 'production'})
        for name, env in project['environments'].items():
            self.assertEqual(env['branch'], name)
            self.assertEqual(env['state'], 'not-provisioned')
            self.assertIsNone(env['recipe_revision'])
        self.db.close()
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        changed = self.legacy(); changed['app']['branch'] = 'replacement'
        changed['later'] = copy.deepcopy(changed['app'])
        self.assertTrue(r.import_legacy(self.db, changed, [])['completed'])
        self.assertEqual(list(r.list_projects(self.db)), ['app'])
        self.assertEqual(r.legacy_projects(self.db)['app']['branch'], 'saved')

    def test_create_and_optimistic_updates_preserve_other_environment(self):
        r = self.registry()
        p = r.create_project(self.db, 'new-app', {'repository': 'OtherOrg/new-app', 'repository_id': 42, 'label': 'New App'})
        self.assertEqual(p['revision'], 1)
        development = r.update_environment(self.db, 'new-app', 'development', 1, {'branch': 'feature/testing', 'automatic': False})
        self.assertEqual(development['revision'], 2)
        other = sqlite3.connect(self.path, isolation_level=None); other.row_factory = sqlite3.Row
        try:
            with self.assertRaises(ValueError):
                r.update_environment(other, 'new-app', 'development', 1, {'branch': 'stale'})
        finally:
            other.close()
        self.assertEqual(r.get_project(self.db, 'new-app')['environments']['production']['branch'], 'production')
        r.update_project(self.db, 'new-app', 1, {'label': 'Updated'})
        with self.assertRaises(ValueError):
            r.update_project(self.db, 'new-app', 1, {'label': 'Stale'})

    def test_invalid_fields_and_bindings_fail_without_changes(self):
        r = self.registry(); r.import_legacy(self.db, self.legacy(), [])
        for changes in ({'legacy_binding': 'staging'}, {'token': 'do-not-store'}, {'repository': 'https://user:pass@example.test/repo'}, {'repository_id': True}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                r.update_project(self.db, 'app', 1, changes)
        for changes in ({'automatic': 1}, {'branch': '--bad'}, {'state': 'running'}, {'recipe_revision': 999}, {'disk_gib': 1}, {'recipe': {'command': 'anything'}}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                r.update_environment(self.db, 'app', 'development', 1, changes)
        with self.assertRaises(ValueError):
            r.update_environment(self.db, 'app', 'staging', 1, {'branch': 'staging'})
        self.assertEqual(r.get_project(self.db, 'app')['revision'], 1)

    def test_explicit_adoption_moves_legacy_recipe_to_only_selected_environment(self):
        r = self.registry(); r.import_legacy(self.db, self.legacy(), [])
        p = r.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        self.assertEqual(p['environments']['production']['branch'], 'main')
        self.assertEqual(p['environments']['production']['state'], 'legacy-bound')
        self.assertIsNotNone(p['environments']['production']['recipe_revision'])
        self.assertIsNone(p['environments']['development']['recipe_revision'])
        with self.assertRaises(ValueError):
            r.update_project(self.db, 'app', 2, {'legacy_binding': 'development'})
        public = json.dumps(p)
        self.assertNotIn('/managed/', public)
        self.assertNotIn('compose_project', public)

    def test_duplicate_create_and_bad_revisions(self):
        r = self.registry(); values = {'repository': 'TPCT/app', 'repository_id': 1}
        r.create_project(self.db, 'app', values)
        with self.assertRaises(ValueError): r.create_project(self.db, 'app', values)
        for revision in (True, 1.0, None, '1', -1):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                r.update_project(self.db, 'app', revision, {'label': 'No'})

    def test_compatibility_settings_persist_and_new_project_stays_inactive(self):
        r = self.registry()
        self.assertFalse(r.imported(self.db))
        r.import_legacy(self.db, self.legacy(), [])
        self.assertTrue(r.imported(self.db))
        r.save_legacy_settings(self.db, 'app', 'chosen', False)
        self.assertEqual(r.legacy_projects(self.db)['app']['branch'], 'chosen')
        self.assertFalse(r.get_project(self.db, 'app')['legacy_settings']['automatic'])
        r.create_project(self.db, 'other', {'repository': 'TPCT/other'})
        other = r.legacy_projects(self.db)['other']
        self.assertEqual(other['branch'], 'development')
        self.assertFalse(other['automatic'])
        self.assertNotIn('recipe', other)
        self.assertTrue(r.get_project(self.db, 'other')['environments']['development']['automatic'])

    def test_recipe_snapshot_is_canonical_immutable_and_not_public(self):
        r = self.registry(); r.import_legacy(self.db, self.legacy(), [])
        recipe = self.db.execute('SELECT * FROM registry_recipes').fetchone()
        self.assertEqual(recipe['document'], json.dumps(self.legacy()['app']['recipe'], sort_keys=True, separators=(',', ':')))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE registry_recipes SET document=?', ('{}',))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM registry_recipes')
        self.assertNotIn('env_file', json.dumps(r.list_projects(self.db)))

    def test_live_repository_identity_cannot_be_silently_replaced(self):
        r = self.registry(); r.import_legacy(self.db, self.legacy(), [])
        for changes in ({'repository': 'TPCT/unrelated'}, {'repository_id': 555}):
            with self.assertRaises(ValueError):
                r.update_project(self.db, 'app', 1, changes)

    def test_import_conflicts_recorded(self):
        r = self.registry(); r.create_project(self.db, 'app', {'repository': 'TPCT/kept'})
        result = r.import_legacy(self.db, self.legacy(), [])
        self.assertEqual(result['skipped'], ['app'])
        self.assertEqual(r.get_project(self.db, 'app')['repository'], 'TPCT/kept')

    def test_invalid_import_rolls_back_all_partial_records(self):
        r = self.registry()
        data = self.legacy()
        data['invalid'] = {'repository': 'bad url', 'branch': 'main'}
        with self.assertRaises(ValueError):
            r.import_legacy(self.db, data, [])
        self.assertFalse(r.imported(self.db))
        self.assertEqual(r.list_projects(self.db), {})

    def test_save_returns_own_revision_even_if_next_writer_commits(self):
        r = self.registry(); r.create_project(self.db, 'app', {'repository': 'TPCT/app'})
        original = self.db
        path = self.path
        class CommitRace:
            @property
            def in_transaction(self): return original.in_transaction
            def execute(self, statement, parameters=()):
                result = original.execute(statement, parameters)
                if statement == 'COMMIT':
                    other = sqlite3.connect(path, isolation_level=None); other.row_factory = sqlite3.Row
                    try: r.update_project(other, 'app', 2, {'label': 'Later'})
                    finally: other.close()
                return result
        saved = r.update_project(CommitRace(), 'app', 1, {'label': 'Mine'})
        self.assertEqual((saved['revision'], saved['label']), (2, 'Mine'))
        self.assertEqual(r.get_project(self.db, 'app')['revision'], 3)

    def test_hostile_input_shapes_are_validation_errors(self):
        r = self.registry(); r.import_legacy(self.db, self.legacy(), [])
        for bad in ([], {}, True, 0, 1.0):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                r.update_project(self.db, 'app', 1, {'legacy_binding': bad})
        for bad in (True, 1.0, [], {}):
            with self.subTest(recipe=bad), self.assertRaises(ValueError):
                r.update_environment(self.db, 'app', 'development', 1, {'recipe_revision': bad})


    def test_bound_projection_uses_newly_pinned_owned_services_recipe(self):
        r = self.registry(); r.import_legacy(self.db, self.legacy(), [])
        r.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        import hashlib
        pinned = dict(self.legacy()['app']['recipe'], owned_services=['web', 'database'])
        document = r.canonical(pinned)
        recipe_id = self.db.execute('INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,?)',
            ('app', hashlib.sha256(document.encode()).hexdigest(), document, 1)).lastrowid
        self.db.execute("UPDATE registry_environments SET recipe_revision=? WHERE project='app' AND name='production'", (recipe_id,))
        projected = r.legacy_projects(self.db)['app']
        self.assertEqual(projected['recipe'], pinned)
        self.assertEqual(projected['recipe']['owned_services'], ['web', 'database'])
        original = json.loads(self.db.execute("SELECT legacy_document FROM registry_projects WHERE id='app'").fetchone()[0])
        self.assertNotIn('owned_services', original['recipe'])
        self.assertNotIn('owned_services', json.dumps(r.get_project(self.db, 'app')))

    def test_bound_projection_rejects_missing_or_cross_project_recipe(self):
        r = self.registry(); r.import_legacy(self.db, self.legacy(), [])
        r.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        r.create_project(self.db, 'other', {'repository': 'TPCT/other'})
        row = self.db.execute("SELECT digest,document FROM registry_recipes WHERE project='app'").fetchone()
        foreign = self.db.execute('INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,?)',
            ('other', row['digest'], row['document'], 1)).lastrowid
        for missing in (None, 99999, foreign):
            with self.subTest(recipe=missing):
                self.db.execute("UPDATE registry_environments SET recipe_revision=? WHERE project='app' AND name='production'", (missing,))
                with self.assertRaisesRegex(ValueError, 'missing or belongs'):
                    r.legacy_projects(self.db)
        self.db.execute("DELETE FROM registry_environments WHERE project='app' AND name='production'")
        with self.assertRaises(ValueError):
            r.legacy_projects(self.db)

    def test_bound_projection_rejects_corrupt_recipe_even_with_legacy_fallback_available(self):
        import hashlib
        r = self.registry(); r.import_legacy(self.db, self.legacy(), [])
        r.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        for index, document in enumerate(('{broken', '{"services":["hijacked"]}', '[]', '{}', '{"value":NaN}')):
            digest = str(index) * 64 if index < 2 else hashlib.sha256(document.encode()).hexdigest()
            recipe_id = self.db.execute('INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,?)',
                ('app', digest, document, 1)).lastrowid
            self.db.execute("UPDATE registry_environments SET recipe_revision=? WHERE project='app' AND name='production'", (recipe_id,))
            with self.subTest(document=document), self.assertRaisesRegex(ValueError, 'bound environment recipe'):
                r.legacy_projects(self.db)


if __name__ == '__main__':
    unittest.main()
