import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path

import registry
import job_target
from store import connect, enqueue, snapshot


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')
        self.recipe = {'images': [{'name': 'app'}], 'tests': [{'command': ['true']}], 'http_checks': [{'url': 'http://example.test'}], 'compose_project': 'owned-group', 'compose_files': ['/managed/compose.yml'], 'services': ['web']}
        registry.import_legacy(self.db, {'app': {'repository': 'TPCT/app', 'repository_id': 12, 'branch': 'main', 'container_prefixes': ['app-'], 'recipe': self.recipe}}, [])
        self.container = {'ID': 'a' * 64, 'Names': 'app-web', 'Image': 'image:latest', 'State': 'running',
                          'Status': 'Up', 'ComposeProject': 'owned-group', 'ComposeService': 'web',
                          'ComposeConfigFiles': '/managed/compose.yml'}
        snapshot(self.db, 'containers', [self.container])
        snapshot(self.db, 'worker', {'state': 'running'})

    def tearDown(self): self.db.close(); self.tmp.cleanup()

    def read(self, environment='development'):
        self.assertIsNotNone(importlib.util.find_spec('workspace'), 'Workspace read model is required')
        import workspace
        return workspace.read(self.db, 'app', environment)

    def test_unclassified_legacy_is_separate_from_both_environments(self):
        for environment in ('development', 'production'):
            result = self.read(environment)
            self.assertEqual(result['binding']['state'], 'unassigned')
            self.assertEqual(result['services'], [])
            self.assertEqual([row['ID'] for row in result['unassigned_services']], ['a' * 64])
            self.assertFalse(result['capabilities']['deploy']['enabled'])
        self.assertNotIn('/managed/', json.dumps(result))

    def test_explicit_recipe_and_observed_identity_are_required_for_membership(self):
        registry.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        result = self.read('production')
        self.assertEqual(result['binding']['state'], 'verified')
        self.assertEqual(len(result['services']), 1)
        self.assertEqual(self.read('development')['services'], [])
        self.assertEqual(self.read('development')['unassigned_services'], [])
        self.assertNotIn('ComposeConfigFiles', result['services'][0])

    def test_name_prefix_or_resource_allocations_never_assign_membership(self):
        registry.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        wrong = dict(self.container, ComposeProject='other-group')
        snapshot(self.db, 'containers', [wrong])
        self.assertEqual(self.read('production')['services'], [])
        self.assertEqual(self.read('production')['unassigned_services'], [])

    def test_missing_ownership_observation_cannot_claim_membership(self):
        registry.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        snapshot(self.db, 'containers', [{key: value for key, value in self.container.items() if not key.startswith('Compose')}])
        result = self.read('production')
        self.assertEqual(result['services'], [])
        self.assertEqual(result['binding']['state'], 'observation-incomplete')
        self.assertFalse(result['observation']['ownership_complete'])

    def test_runs_filter_using_immutable_targets_not_current_branch(self):
        target = job_target.capture('app', 'production', 'TPCT/app', 12, 'main', 'b' * 40,
                                    'deploy', 1, self.recipe, project_revision=1, environment_revision=1)
        job, _ = enqueue(self.db, 'bound-run', 'app', 'b' * 40, True, 'main', target_factory=lambda db: target)
        legacy, _ = enqueue(self.db, 'legacy-run', 'app', 'c' * 40, True, 'production')
        production = self.read('production')
        self.assertEqual([row['id'] for row in production['runs']], [job])
        self.assertEqual([row['id'] for row in production['unassigned_runs']], [legacy])
        self.assertEqual(self.read('development')['runs'], [])
        self.assertNotIn('compose_files', json.dumps(production))

    def test_stale_observation_disables_runtime_actions(self):
        registry.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        self.db.execute("UPDATE snapshots SET updated=? WHERE name='containers'", (time.time() - 1000,))
        result = self.read('production')
        self.assertTrue(result['observation']['stale'])
        self.assertFalse(result['services'][0]['capabilities']['manage'])

    def test_invalid_target_and_corrupt_pinned_run_fail_closed(self):
        with self.assertRaises(ValueError): self.read('staging')
        job, _ = enqueue(self.db, 'bad-target', 'app', 'b' * 40, True, 'main')
        self.db.execute('INSERT INTO job_targets VALUES(?,?,?,?)', (job, 'invalid', '{}', time.time()))
        result = self.read('production')
        self.assertEqual(result['runs'], [])
        self.assertEqual(result['unassigned_runs'][0]['target_state'], 'invalid')

    def test_ambiguous_registered_owners_disable_management(self):
        registry.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        # A bad legacy adoption/configuration must be shown as a conflict,
        # rather than silently selecting the first matching environment.
        self.db.execute("UPDATE registry_environments SET recipe_revision=1 WHERE project='app' AND name='development'")
        result = self.read('production')
        self.assertEqual(result['binding']['state'], 'conflict')
        self.assertEqual(result['services'], [])
        self.assertFalse(result['unassigned_services'][0]['capabilities']['manage'])

    def test_config_path_parent_segments_are_not_normalized_into_ownership(self):
        registry.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        row = dict(self.container, ComposeConfigFiles='/managed/symlink/../compose.yml')
        snapshot(self.db, 'containers', [row])
        self.assertEqual(self.read('production')['services'], [])

    def test_server_action_reads_registry_without_mutating_bindings(self):
        from server import Application
        app = object.__new__(Application)
        app.database = str(Path(self.tmp.name) / 'state.sqlite')
        app.config = str(Path(self.tmp.name) / 'missing-legacy-file.json')
        before = registry.get_project(self.db, 'app')
        code, response = app.action({'username': 'operator'}, {'action': 'environment-workspace', 'project': 'app', 'environment': 'development'})
        self.assertEqual(code, 200)
        self.assertEqual(response['environment']['name'], 'development')
        self.assertEqual(registry.get_project(self.db, 'app'), before)

    def test_stale_worker_heartbeat_disables_runtime_actions(self):
        registry.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        self.db.execute("UPDATE snapshots SET updated=? WHERE name='worker'", (time.time() - 1000,))
        self.assertTrue(self.read('production')['observation']['stale'])

    def test_incomplete_recipe_cannot_claim_verified_binding(self):
        self.db.execute('INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,?)',
                        ('app', 'fixture-invalid', json.dumps({'compose_project': 'owned-group', 'compose_files': ['/managed/compose.yml']}), time.time()))
        invalid_id = self.db.execute('SELECT max(id) FROM registry_recipes').fetchone()[0]
        self.db.execute("UPDATE registry_environments SET recipe_revision=? WHERE project='app' AND name='production'", (invalid_id,))
        result = self.read('production')
        self.assertNotEqual(result['binding']['state'], 'verified')

    def test_unrelated_incomplete_container_does_not_disable_verified_deployment(self):
        registry.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        snapshot(self.db, 'containers', [self.container, {'ID': 'b' * 12, 'Names': 'unrelated-host-tool', 'State': 'running'}, None])
        result = self.read('production')
        self.assertTrue(result['observation']['ownership_complete'])
        self.assertEqual(result['binding']['state'], 'verified')
        self.assertTrue(result['capabilities']['deploy']['enabled'])
        self.assertEqual([row['ID'] for row in result['services']], [self.container['ID']])

    def test_incomplete_additional_candidate_disables_deploy_without_assigning_it(self):
        registry.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        for candidate in [dict(self.container, ID='b' * 12, Names='different-name'),
                          {'ID': 'b' * 64, 'Names': 'app-extra', 'State': 'running'}]:
            with self.subTest(candidate=candidate):
                snapshot(self.db, 'containers', [self.container, candidate])
                result = self.read('production')
                self.assertFalse(result['observation']['ownership_complete'])
                self.assertEqual(result['binding']['state'], 'observation-incomplete')
                self.assertFalse(result['capabilities']['deploy']['enabled'])
                self.assertEqual([row['ID'] for row in result['services']], [self.container['ID']])

    def test_file_order_and_duplicates_never_match_bound_recipe(self):
        import workspace
        recipe = dict(self.recipe, compose_files=['/managed/compose.yml', '/managed/override.yml'])
        for files in ['/managed/override.yml,/managed/compose.yml',
                      '/managed/compose.yml,/managed/override.yml,/managed/compose.yml']:
            row = dict(self.container, ComposeConfigFiles=files)
            self.assertFalse(workspace._recipe_matches(recipe, workspace._identity(row)))
        self.assertTrue(workspace._recipe_matches(recipe, workspace._identity(dict(
            self.container, ComposeConfigFiles='/managed/compose.yml,/managed/override.yml'))))

class WorkspaceProvenanceTests(unittest.TestCase):
    setUp = WorkspaceTests.setUp
    tearDown = WorkspaceTests.tearDown
    read = WorkspaceTests.read
    def test_pinned_provenance_accepts_only_reviewed_ordered_invocation(self):
        import hashlib
        proof = {'schema_version': 1, 'service': 'web', 'compose_project': 'owned-group',
                 'observed_files': ['/managed/old.yml', '/managed/old.override.yml'],
                 'canonical_files': self.recipe['compose_files'],
                 'environment_context_digest': 'b' * 64, 'semantic_digest': 'c' * 64}
        proof['fingerprint'] = hashlib.sha256(registry.canonical(proof).encode()).hexdigest()
        recipe = dict(self.recipe, service_provenance={'web': proof})
        document = registry.canonical(recipe)
        revision = self.db.execute('INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,?)',
            ('app', hashlib.sha256(document.encode()).hexdigest(), document, time.time())).lastrowid
        registry.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        self.db.execute("UPDATE registry_environments SET recipe_revision=? WHERE project='app' AND name='production'", (revision,))
        snapshot(self.db, 'containers', [dict(self.container, ComposeConfigFiles=','.join(proof['observed_files']))])
        result = self.read('production')
        self.assertEqual(result['binding']['state'], 'verified')
        self.assertEqual(len(result['services']), 1)
        self.assertNotIn('service_provenance', json.dumps(result))
        snapshot(self.db, 'containers', [dict(self.container, ComposeConfigFiles=','.join(reversed(proof['observed_files'])))])
        self.assertEqual(self.read('production')['services'], [])

    def test_recipe_digest_corruption_cannot_grant_membership(self):
        registry.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        revision = self.db.execute('INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,?)',
            ('app', 'a' * 64, registry.canonical(self.recipe), time.time())).lastrowid
        self.db.execute("UPDATE registry_environments SET recipe_revision=? WHERE project='app' AND name='production'", (revision,))
        self.assertEqual(self.read('production')['services'], [])
