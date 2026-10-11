"""Historical artifacts remain scoped, ordered and explicitly recorded evidence."""
import dataclasses
import unittest
import test_managed_services as fixtures
import application_operations as ops
import job_target
import managed_services as services
from store import enqueue


class ServiceVersionTests(unittest.TestCase):
    setUp = fixtures.ManagedServiceTests.setUp
    tearDown = fixtures.ManagedServiceTests.tearDown
    create = fixtures.ManagedServiceTests.create
    def publish(self, suffix, environment='development', project='app'):
        target = job_target.capture(project, environment, 'TPCT/app', 12, 'main', suffix * 40,
                                    'validate', 1, self.recipe, project_revision=1, environment_revision=1)
        job, _ = enqueue(self.db, 'version-' + project + environment + suffix, project, suffix * 40,
                         True, 'main', 'validate', target_factory=lambda db: target)
        self.db.execute("UPDATE jobs SET status='validated',created=100 WHERE id=?", (job,))
        return services.publish_artifact(self.db, job, 'application-v1', 'sha256:' + suffix * 64)

    def history(self, **kwargs):
        return ops.service_versions(self.db, self.principal, 'app', 'development', **kwargs)

    def test_exact_scope_order_cursor_and_read_only(self):
        first = self.publish('c'); second = self.publish('d'); third = self.publish('e')
        self.publish('f', 'production'); self.publish('a', project='other')
        # Late publication of an older source cannot leap ahead of newer builds.
        retry = services.publish_artifact(self.db, first['source_job'], 'application-v1', 'sha256:' + 'b' * 64)
        before = self.db.total_changes
        page = self.history(limit=2)
        self.assertEqual([v['artifact_id'] for v in page['versions']], [third['id'], second['id']])
        next_page = self.history(limit=2, cursor=page['next_cursor'])
        self.assertEqual({v['artifact_id'] for v in next_page['versions']}, {first['id'], retry['id']})
        self.assertIsNone(next_page['next_cursor'])
        self.assertEqual(self.db.total_changes, before)
        with self.assertRaises(ops.OperationError):
            ops.service_versions(self.db, self.principal, 'app', 'production', cursor=page['next_cursor'])

    def test_recorded_association_tracks_current_definition_in_exact_environment(self):
        artifact = self.publish('c')
        accepted = self.create(dict(self.values, artifact_id=artifact['id']))
        item = self.history()['versions'][0]
        self.assertEqual(item['recorded_services'], [{'service_id': accepted['service']['id'], 'name': 'web',
            'environment': 'development', 'recorded_state': 'queued', 'recorded_at': self.db.execute('SELECT updated FROM managed_services WHERE id=?', (accepted['service']['id'],)).fetchone()[0], 'container_id': None}])
        self.assertEqual(self.history()['evidence'], 'recorded')
        claim = services.claim_next(self.db, 'history-worker')
        binding = {'container_id': 'c' * 64, 'container_name': 'app-development-web-1', 'compose_project': 'app',
                   'compose_service': 'development-web', 'compose_files': ['/managed/app/development/compose.json'], 'state': 'stopped'}
        services.finish(self.db, claim['operation_id'], 'history-worker', binding, 'stopped')
        recorded = self.history()['versions'][0]['recorded_services'][0]
        self.assertEqual(recorded['container_id'], 'c' * 64)
        self.assertEqual(recorded['recorded_state'], 'stopped')

        self.create(dict(self.values, artifact_id=artifact['id'], policy_revision=2), environment='production', key='production-create-key')
        self.assertEqual(len(self.history()['versions'][0]['recorded_services']), 1)
        self.assertEqual(ops.service_versions(self.db, self.principal, 'app', 'production')['versions'], [])

    def test_permission_bounds_and_invalid_proofs(self):
        self.publish('c')
        for principal in (dataclasses.replace(self.principal, scopes=frozenset()),
                          dataclasses.replace(self.principal, allowed_projects=frozenset({'other'})),
                          dataclasses.replace(self.principal, allowed_repository_ids=frozenset({99})),
                          dataclasses.replace(self.principal, allowed_environments=frozenset({('app', 'production')}))):
            with self.assertRaises(ops.OperationError) as error:
                ops.service_versions(self.db, principal, 'app', 'development')
            self.assertEqual(error.exception.status, 403)
        for arguments in ({'limit': 0}, {'limit': 101}, {'limit': True}, {'limit': '2'}, {'cursor': 'bad'}, {'cursor': {}}):
            with self.subTest(arguments=arguments), self.assertRaises(ops.OperationError): self.history(**arguments)
        self.db.execute("UPDATE jobs SET status='failed'")
        with self.assertRaises(ops.OperationError) as error: self.history()
        self.assertEqual(error.exception.code, 'history_evidence_unavailable')
        self.assertEqual(error.exception.status, 409)

    def test_malformed_project_artifact_is_controlled_evidence_failure(self):
        self.publish('c')
        self.db.execute('DROP TRIGGER service_artifacts_immutable_update')
        self.db.execute("UPDATE service_artifacts SET document='broken-json'")
        with self.assertRaises(ops.OperationError) as error: self.history()
        self.assertEqual(error.exception.code, 'history_evidence_unavailable')

    def test_foreign_environment_invalid_source_does_not_hide_valid_history(self):
        artifact = self.publish('c')
        foreign = self.publish('d', 'production')
        self.db.execute("UPDATE jobs SET status='failed' WHERE id=?", (foreign['source_job'],))
        page = self.history()
        self.assertEqual([row['artifact_id'] for row in page['versions']], [artifact['id']])
        self.assertEqual(page['versions'][0]['image_availability'], 'not_checked')

    def test_deleted_source_remains_visible_as_integrity_failure(self):
        artifact = self.publish('c')
        self.db.execute('DELETE FROM jobs WHERE id=?', (artifact['source_job'],))
        with self.assertRaises(ops.OperationError) as error: self.history()
        self.assertEqual(error.exception.code, 'history_evidence_unavailable')

    def test_malformed_recorded_binding_is_controlled_evidence_failure(self):
        artifact = self.publish('c')
        accepted = self.create(dict(self.values, artifact_id=artifact['id']))
        self.db.execute('INSERT INTO service_bindings VALUES(?,?,?)', (accepted['service']['id'], '[]', 100))
        with self.assertRaises(ops.OperationError) as error: self.history()
        self.assertEqual(error.exception.code, 'history_evidence_unavailable')

    def test_orphan_artifact_on_later_page_is_not_silently_skipped(self):
        orphan = self.publish('c')
        newer = self.publish('d')
        self.db.execute('DELETE FROM jobs WHERE id=?', (orphan['source_job'],))
        self.db.execute('UPDATE jobs SET created=? WHERE id=?', (4000000000, newer['source_job']))
        page = self.history(limit=1)
        self.assertEqual(page['versions'][0]['artifact_id'], newer['id'])
        self.assertIsNotNone(page['next_cursor'])
        with self.assertRaises(ops.OperationError) as error: self.history(limit=1, cursor=page['next_cursor'])
        self.assertEqual(error.exception.code, 'history_evidence_unavailable')

    def test_missing_or_invalid_artifact_scope_is_not_empty_history(self):
        artifact = self.publish('c')
        for document in ('{}', 'null', '[]', '{"source_environment":null}',
                         '{"source_environment":[]}', '{"source_environment":"unknown"}'):
            with self.subTest(document=document):
                self.db.execute('DROP TRIGGER IF EXISTS service_artifacts_immutable_update')
                self.db.execute('UPDATE service_artifacts SET document=? WHERE id=?', (document, artifact['id']))
                with self.assertRaises(ops.OperationError) as error: self.history()
                self.assertEqual(error.exception.code, 'history_evidence_unavailable')
                self.assertEqual(error.exception.status, 409)

    def test_missing_current_definition_is_not_empty_associations(self):
        artifact = self.publish('c')
        accepted = self.create(dict(self.values, artifact_id=artifact['id']))
        self.db.execute("UPDATE managed_services SET definition_id='absent' WHERE id=?", (accepted['service']['id'],))
        with self.assertRaises(ops.OperationError) as error: self.history()
        self.assertEqual(error.exception.code, 'history_evidence_unavailable')
        self.assertEqual(error.exception.status, 409)
