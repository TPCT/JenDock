import unittest
from pathlib import Path
from unittest.mock import patch
import worker
import managed_services
import application_operations as ops
import test_managed_services as service_fixtures


class ServiceWorkerLoopTests(unittest.TestCase):
    setUp = service_fixtures.ManagedServiceTests.setUp
    tearDown = service_fixtures.ManagedServiceTests.tearDown
    create = service_fixtures.ManagedServiceTests.create

    def test_worker_claims_real_queue_and_commits_verified_binding(self):
        accepted = self.create()
        root = Path(self.tmp.name)
        binding = {'container_id': 'c'*64, 'container_name': 'app-development-web-1',
                   'compose_project': 'app', 'compose_service': 'development-web',
                   'compose_files': [str(root/'managed/app/development/compose.json')], 'state': 'stopped'}
        def execute(db, operation_id, definition, **options):
            self.assertEqual(operation_id, accepted['operation']['operation_id'])
            self.assertEqual(options['authorize'](), definition)
            self.assertEqual(definition['desired_state'], 'stopped')
            return binding
        with patch.object(worker.control, 'ROOT', root), patch.object(worker.control, 'JOURNAL', root/'absent'), \
             patch.object(worker, 'STATE', root), patch('service_provisioning.provision', side_effect=execute):
            self.assertTrue(worker.process_service_once(self.db))
            self.assertFalse(worker.process_service_once(self.db))
        operation = ops.get_operation(self.db, self.principal, accepted['operation']['operation_id'])
        self.assertEqual(operation['status'], 'stopped')
        self.assertEqual(managed_services.reservation_totals(self.db, 'app', 'development')['cpu'], 0)

    def test_worker_failure_retains_reservation_and_records_recovery(self):
        accepted = self.create(); root = Path(self.tmp.name)
        with patch.object(worker.control, 'ROOT', root), patch.object(worker.control, 'JOURNAL', root/'absent'), \
             patch.object(worker, 'STATE', root), patch('service_provisioning.provision', side_effect=ValueError('private diagnostic')):
            self.assertTrue(worker.process_service_once(self.db))
        operation = ops.get_operation(self.db, self.principal, accepted['operation']['operation_id'])
        self.assertEqual(operation['status'], 'recovery-required')
        self.assertNotIn('private diagnostic', str(operation))
        self.assertEqual(managed_services.reservation_totals(self.db, 'app', 'development')['cpu'], .5)

    def test_accepted_creation_blocks_adoption_before_runtime_recipe_exists(self):
        import adoption
        self.create()
        with self.assertRaisesRegex(ValueError, 'creation or recovery'):
            adoption.preview(self.db, 'app', 'development', {})
        self.assertIsNone(self.db.execute("SELECT recipe_revision FROM registry_environments WHERE project='app' AND name='development'").fetchone()[0])
