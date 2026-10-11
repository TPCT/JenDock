import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from cryptography.fernet import Fernet
import application_operations as ops
import integrations
import registry
from store import connect


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.path = self.root / 'db'; self.db = connect(self.path)
        self.key = self.root / 'key'; self.key.write_bytes(Fernet.generate_key()); self.key.chmod(0o600)
        integrations.save_connection(self.db, 'github', 0, 'fixture-secret-token', self.key,
            verifier=lambda *args: {'api_access': 'verified', 'identity': 'fixture', 'repositories': [], 'clone_access': 'unverified', 'webhook_management': 'unverified'})
        self.principal = ops.Principal('owner', 'console', frozenset({'projects:create', 'operations:read'}))
        self.input = ops.ProjectInput('new-app', 'TPCT/new-app', 'New app')
        self.get = Mock(return_value={'id': 12, 'full_name': 'TPCT/new-app', 'private': True})

    def tearDown(self): self.db.close(); self.tmp.cleanup()

    def create(self, **kwargs):
        return ops.create_project(self.db, kwargs.pop('principal', self.principal), kwargs.pop('input', self.input),
                                  kwargs.pop('key', 'create-request-12345'), self.key, get=kwargs.pop('get', self.get), **kwargs)

    def test_create_atomic_reopen_replay_cross_adapter_and_no_secret_or_runtime(self):
        result = self.create()
        self.assertEqual(set(result['project']['environments']), {'development', 'production'})
        for environment in result['project']['environments'].values():
            self.assertEqual(environment['state'], 'not-provisioned'); self.assertIsNone(environment['recipe_revision']); self.assertTrue(environment['automatic'])
        self.db.close(); self.db = connect(self.path)
        integrations.disable_connection(self.db, 'github', 1)
        principal = dataclasses.replace(self.principal, actor_kind='mcp', credential_id='connection-1')
        self.assertEqual(self.create(principal=principal), result); self.assertEqual(self.get.call_count, 1)
        self.assertEqual(ops.get_operation(self.db, principal, result['operation']['operation_id']), result['operation'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM registry_projects').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM application_operation_events').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
        self.assertNotIn('fixture-secret-token', json.dumps(result))

    def test_authorization_scope_target_repository_and_operation_isolation(self):
        result = self.create()
        principals = [dataclasses.replace(self.principal, scopes=frozenset({'operations:read'})),
                      dataclasses.replace(self.principal, allowed_projects=frozenset({'other'})),
                      dataclasses.replace(self.principal, allowed_repository_ids=frozenset({99}))]
        for principal in principals:
            with self.assertRaises(ops.OperationError) as error: self.create(principal=principal)
            self.assertEqual(error.exception.status, 403)
        with self.assertRaises(ops.OperationError): ops.get_operation(self.db, dataclasses.replace(self.principal, subject_id='other'), result['operation']['operation_id'])
        with self.assertRaises(dataclasses.FrozenInstanceError): self.principal.subject_id = 'changed'

    def test_changed_payload_conflicts_and_provider_failure_does_not_write(self):
        self.create()
        with self.assertRaises(ops.OperationError) as error: self.create(input=dataclasses.replace(self.input, label='Changed'))
        self.assertEqual(error.exception.code, 'idempotency_conflict')
        self.assertEqual(self.get.call_count, 1)
        for provider in [Mock(return_value={'id': 99, 'full_name': 'other/repo'}), Mock(side_effect=RuntimeError('fixture-secret-token'))]:
            with self.assertRaises(ops.OperationError) as error:
                self.create(input=dataclasses.replace(self.input, project='second'), key='another-key-123456', get=provider)
            self.assertNotIn('fixture-secret-token', str(error.exception))
        self.assertEqual(self.db.execute('SELECT count(*) FROM registry_projects').fetchone()[0], 1)

    def test_provider_runs_outside_transaction_and_disabled_connection_race_rejected(self):
        def provider(*args):
            self.assertFalse(self.db.in_transaction)
            other = connect(self.path)
            integrations.disable_connection(other, 'github', 1); other.close()
            return {'id': 12, 'full_name': 'TPCT/new-app'}
        with self.assertRaises(ops.OperationError) as error: self.create(get=provider)
        self.assertEqual(error.exception.code, 'integration_changed')
        self.assertEqual(self.db.execute('SELECT count(*) FROM registry_projects').fetchone()[0], 0)

    def test_failure_in_atomic_insert_rolls_back_project_and_both_environments(self):
        ops.initialize(self.db)
        self.db.execute("CREATE TRIGGER fail_events BEFORE INSERT ON application_operation_events BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(Exception): self.create()
        self.assertEqual(self.db.execute('SELECT count(*) FROM registry_projects').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM registry_environments').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM application_operations').fetchone()[0], 0)

    def test_restricted_repository_permission_requires_identity_before_provider_read(self):
        principal = dataclasses.replace(self.principal, allowed_repository_ids=frozenset({12}))
        with self.assertRaises(ops.OperationError): self.create(principal=principal)
        self.get.assert_not_called()
        result = self.create(principal=principal, input=dataclasses.replace(self.input, repository_id=12))
        self.assertEqual(result['project']['repository_id'], 12)

    def test_concurrent_same_key_creates_one_project_and_one_operation(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        ops.initialize(self.db)
        barrier = threading.Barrier(2)
        def provider(*args):
            barrier.wait(timeout=5)
            return {'id': 12, 'full_name': 'TPCT/new-app'}
        def call():
            connection = connect(self.path)
            try:
                return ops.create_project(connection, self.principal, self.input, 'concurrent-request-key', self.key, get=provider)
            finally: connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.db.execute('SELECT count(*) FROM application_operations').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM registry_environments').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM application_operation_events').fetchone()[0], 1)

    def test_concurrent_different_keys_for_same_project_have_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        ops.initialize(self.db)
        barrier = threading.Barrier(2)
        def provider(*args):
            barrier.wait(timeout=5)
            return {'id': 12, 'full_name': 'TPCT/new-app'}
        def call(key):
            connection = connect(self.path)
            try:
                try:
                    ops.create_project(connection, self.principal, self.input, key, self.key, get=provider)
                    return 'succeeded'
                except ops.OperationError as error:
                    return error.code
            finally: connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call, 'distinct-create-request-' + str(index)) for index in range(2)]
            outcomes = [future.result(timeout=10) for future in futures]
        self.assertEqual(sorted(outcomes), ['project_exists', 'succeeded'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM registry_projects').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM registry_environments').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM application_operation_events').fetchone()[0], 1)
