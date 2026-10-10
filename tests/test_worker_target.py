import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import worker


class WorkerTargetTests(unittest.TestCase):
    def project(self):
        return {'environment': 'production', 'recipe': {
            'compose_project': 'app', 'compose_files': ['/managed/app.yml'], 'services': ['web'],
            'env_file': '/managed/.env', 'images': [{'name': 'app', 'dockerfile': 'Dockerfile', 'env_key': 'APP_IMAGE'}],
            'tests': [{'image': 'app', 'command': ['php', '-v']}],
            'http_checks': [{'url': 'https://example.test', 'statuses': [200]}]}}

    def observation(self):
        return {'Id': 'a' * 64, 'State': {'Running': True}, 'Config': {'Labels': {
            'com.docker.compose.project': 'app', 'com.docker.compose.service': 'web',
            'com.docker.compose.project.config_files': '/managed/app.yml'}}}

    def verify(self, actual, expected=None):
        with patch.object(worker, 'containers', return_value=[{'ID': 'a' * 12, 'Service': 'web'}]), \
             patch.object(worker, 'run', return_value=json.dumps([actual])):
            return worker.verify_target_ownership(self.project(), expected)

    def test_actual_ownership_and_full_identity_are_verified(self):
        self.assertEqual(self.verify(self.observation()), {'a' * 64})
        self.assertEqual(self.verify(self.observation(), {'a' * 64}), {'a' * 64})

    def test_stopped_replaced_or_differently_owned_targets_cannot_cut_over(self):
        stopped = self.observation(); stopped['State']['Running'] = False
        with self.assertRaisesRegex(worker.StepFailure, 'stopped'):
            self.verify(stopped)
        with self.assertRaisesRegex(worker.StepFailure, 'replaced'):
            self.verify(self.observation(), {'b' * 64})
        for key in ('com.docker.compose.project', 'com.docker.compose.service', 'com.docker.compose.project.config_files'):
            actual = self.observation(); actual['Config']['Labels'][key] = 'other'
            with self.subTest(key=key), self.assertRaisesRegex(worker.StepFailure, 'ownership'):
                self.verify(actual)

    def test_missing_service_fails_without_inspection_or_mutation(self):
        with patch.object(worker, 'containers', return_value=[]), patch.object(worker, 'run') as command:
            with self.assertRaisesRegex(worker.StepFailure, 'missing'):
                worker.verify_target_ownership(self.project())
            command.assert_not_called()

    def test_malformed_recipe_fails_before_any_subprocess(self):
        for mutation in ({'images': []}, {'tests': [{'image': 'app', 'command': 'php -v'}]},
                         {'http_checks': [{}]}, {'env_file': 'relative'},
                         {'images': [{'name': 'app', 'dockerfile': '../Dockerfile', 'env_key': 'APP_IMAGE'}]}):
            project = self.project(); project['recipe'].update(mutation)
            with self.subTest(mutation=mutation), patch.object(worker, 'run') as command:
                with self.assertRaises(worker.StepFailure):
                    worker.deploy(None, {'id': 1}, project)
                command.assert_not_called()

    def test_schema_detection_includes_sql_prisma_and_standard_migration_paths(self):
        files = ['db/patch.sql', 'prisma/schema.prisma', 'app/migrations/001.py',
                 'alembic/versions/new.py', 'db/schema.rb', 'app/style.css']
        self.assertEqual(worker.schema_change_paths('\n'.join(files)), files[:-1])

    def test_destructive_detection_is_bounded_and_never_calls_unrecognized_code_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'drop.php').write_text("Schema::dropIfExists('users');")
            (root / 'add.sql').write_text('CREATE TABLE example(id int);')
            (root / 'add.php').write_text("public function up(): void { Schema::create('new', function ($t) {}); } public function down(): void { Schema::dropIfExists('new'); }")
            (root / 'truncate.sql').write_text('TRUNCATE TABLE example;')
            result = worker.migration_intent(root, ['drop.php', 'add.sql', 'add.php', 'truncate.sql', 'deleted.sql', '../outside.sql'])
            self.assertEqual(result['destructive'], ['drop.php', 'truncate.sql'])
            self.assertEqual(result['unverified'], ['add.sql', 'add.php', 'deleted.sql', '../outside.sql'])

    def test_production_and_unclassified_never_execute_development_adapter(self):
        for env in ('production', 'legacy-unclassified', None):
            project = self.project(); project['environment'] = env
            project['recipe']['migration'] = {'framework': 'laravel', 'service': 'web', 'image': 'app'}
            with self.subTest(env=env), self.assertRaises(worker.StepFailure):
                worker.migration_policy(project)

    def test_validation_target_cannot_be_executed_as_deployment(self):
        with patch.object(worker, 'run') as command:
            with self.assertRaisesRegex(worker.StepFailure, 'execution mode'):
                worker.deploy(None, {'id': 1, 'mode': 'validate'}, self.project(), validate_only=False)
            command.assert_not_called()

    def test_reversed_or_duplicate_compose_files_rejected_before_cutover(self):
        project = self.project()
        project['recipe']['compose_files'] = ['/managed/app.yml', '/managed/override.yml']
        for files in ['/managed/override.yml,/managed/app.yml',
                      '/managed/app.yml,/managed/override.yml,/managed/app.yml']:
            actual = self.observation()
            actual['Config']['Labels']['com.docker.compose.project.config_files'] = files
            with self.subTest(files=files), patch.object(worker, 'containers', return_value=[{'ID': 'a' * 12, 'Service': 'web'}]), \
                 patch.object(worker, 'run', return_value=json.dumps([actual])):
                with self.assertRaises(worker.StepFailure): worker.verify_target_ownership(project)

class WorkerProvenanceTests(unittest.TestCase):
    project = WorkerTargetTests.project
    observation = WorkerTargetTests.observation
    def pinned(self):
        import hashlib
        project = self.project()
        recipe = project['recipe']
        recipe['owned_services'] = ['web', 'database']
        recipe['service_provenance'] = {}
        for service in recipe['owned_services']:
            proof = {'schema_version': 1, 'service': service, 'compose_project': 'app',
                     'observed_files': ['/managed/original.yml'], 'canonical_files': recipe['compose_files'],
                     'environment_context_digest': 'b' * 64, 'semantic_digest': 'c' * 64}
            proof['fingerprint'] = hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            recipe['service_provenance'][service] = proof
        return project

    def test_original_invocation_matches_after_all_service_proofs_revalidated(self):
        project = self.pinned(); observed = self.observation()
        observed['Config']['Labels']['com.docker.compose.project.config_files'] = '/managed/original.yml'
        def review(recipe, service, files, root, command, compose):
            self.assertEqual(str(root), '/opt/docker-engine/projects')
            self.assertEqual(files, ['/managed/original.yml'])
            return {'equivalent': True, 'proof': recipe['service_provenance'][service]}
        with patch.object(worker.compose_review, 'review', side_effect=review) as reviewed, \
             patch.object(worker, 'containers', return_value=[{'ID': 'a' * 12, 'Service': 'web'}]), \
             patch.object(worker, 'run', return_value=json.dumps([observed])):
            ids = worker.verify_target_ownership(project)
            self.assertEqual(worker.verify_target_ownership(project, ids), ids)
            self.assertEqual([call.args[1] for call in reviewed.call_args_list], ['web', 'database', 'web', 'database'])

    def test_missing_proof_rejects_old_invocation_and_drift_blocks_before_inspection(self):
        project = self.pinned(); observed = self.observation()
        observed['Config']['Labels']['com.docker.compose.project.config_files'] = '/managed/original.yml'
        project['recipe'].pop('service_provenance')
        with patch.object(worker, 'containers', return_value=[{'ID': 'a' * 12, 'Service': 'web'}]), \
             patch.object(worker, 'run', return_value=json.dumps([observed])):
            with self.assertRaisesRegex(worker.StepFailure, 'ownership'):
                worker.verify_target_ownership(project)
        for result in ({'equivalent': False, 'proof': None}, {'proof': {}}, None):
            with self.subTest(result=result), patch.object(worker.compose_review, 'review', return_value=result), \
                 patch.object(worker, 'containers') as inspect:
                with self.assertRaisesRegex(worker.StepFailure, 'provenance'):
                    worker.verify_target_ownership(self.pinned())
                inspect.assert_not_called()

    def test_valid_but_changed_context_proof_blocks_canonical_target(self):
        import hashlib
        def review(recipe, service, *args):
            proof = dict(recipe['service_provenance'][service], environment_context_digest='d' * 64)
            proof.pop('fingerprint')
            proof['fingerprint'] = hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            return {'equivalent': True, 'proof': proof}
        with patch.object(worker.compose_review, 'review', side_effect=review), patch.object(worker, 'containers') as inspect:
            with self.assertRaisesRegex(worker.StepFailure, 'provenance'):
                worker.verify_target_ownership(self.pinned())
            inspect.assert_not_called()
