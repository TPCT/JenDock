import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import job_target
import registry
import worker
from delivery_target import capture_registered_target
from store import connect, enqueue, snapshot


class PinnedDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temporary.name) / 'state.sqlite')
        self.recipe = {'compose_project': 'app', 'compose_files': ['/managed/app/compose.yml'],
                       'images': [{'name': 'app'}], 'tests': [{'command': ['true']}],
                       'http_checks': [{'url': 'http://example.test', 'statuses': [200]}],
                       'services': ['web'], 'env_file': '/managed/app/.env',
                       'migration': {'framework': 'laravel', 'service': 'web', 'image': 'app'}}
        self.legacy = {'app': {'repository': 'TPCT/app', 'repository_id': 12, 'branch': 'main',
                               'automatic': True, 'recipe': self.recipe}}
        registry.import_legacy(self.db, self.legacy, [])

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def enqueue(self, delivery='first', target_factory=None):
        if target_factory is None:
            target_factory = lambda db: capture_registered_target(db, 'app', 'main', 'a' * 40, 'deploy')
        return enqueue(self.db, delivery, 'app', 'a' * 40, True, 'main', target_factory=target_factory)

    def job(self, job_id):
        return self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()

    def test_execution_uses_pinned_target_after_registry_changes(self):
        job_id, _ = self.enqueue()
        expected = worker.project_for_job(self.db, self.job(job_id))
        legacy = copy.deepcopy(self.legacy['app'])
        legacy['recipe']['compose_project'] = 'different-live-target'
        legacy['branch'] = 'changed'
        self.db.execute('UPDATE registry_projects SET legacy_document=? WHERE id=?', (json.dumps(legacy), 'app'))
        with patch.object(worker, 'effective_projects', side_effect=AssertionError('must not read current target')):
            self.assertEqual(worker.project_for_job(self.db, self.job(job_id)), expected)
        self.assertEqual(expected['environment'], 'legacy-unclassified')
        self.assertEqual(expected['recipe']['migration'], self.recipe['migration'])

    def test_bound_target_uses_immutable_recipe_not_legacy_projection(self):
        adopted = registry.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        changed = copy.deepcopy(self.legacy['app'])
        changed['recipe']['compose_project'] = 'untrusted-replacement'
        self.db.execute('UPDATE registry_projects SET legacy_document=? WHERE id=?', (json.dumps(changed), 'app'))
        with self.assertRaisesRegex(ValueError, 'explicit environment'):
            self.enqueue()
        snapshot(self.db, 'worker', {'state': 'running'})
        snapshot(self.db, 'containers', [{'ID': 'a' * 64, 'Names': 'app-web', 'State': 'running',
                 'ComposeProject': 'app', 'ComposeService': 'web', 'ComposeConfigFiles': '/managed/app/compose.yml'}])
        environment = adopted['environments']['production']
        job_id, _ = self.enqueue(target_factory=lambda db: capture_registered_target(
            db, 'app', 'main', 'a' * 40, 'deploy', environment='production',
            project_revision=adopted['revision'], environment_revision=environment['revision'],
            recipe_revision=environment['recipe_revision']))
        target = job_target.load(self.db, job_id)
        self.assertEqual(target['recipe']['compose_project'], 'app')
        self.assertEqual(target['environment'], 'production')
        self.assertEqual(target['environment_revision'], adopted['environments']['production']['revision'])
        self.assertEqual(target['recipe_revision'], adopted['environments']['production']['recipe_revision'])

    def test_capture_failure_rolls_back_job_and_does_not_supersede_older_queue(self):
        old_id, _ = self.enqueue()
        def fail(db):
            self.assertTrue(db.in_transaction)
            raise ValueError('invalid binding')
        with self.assertRaisesRegex(ValueError, 'invalid binding'):
            self.enqueue('failed-capture', fail)
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
        self.assertEqual(self.job(old_id)['status'], 'queued')
        self.assertEqual(self.db.execute('SELECT count(*) FROM job_targets').fetchone()[0], 1)

    def test_exact_retry_does_not_recapture(self):
        initial = self.enqueue()
        factory = Mock(side_effect=AssertionError('must not recapture exact retry'))
        self.assertEqual(self.enqueue(target_factory=factory), (initial[0], False))
        factory.assert_not_called()

    def test_old_jobs_without_targets_fail_before_runtime_or_registry_access(self):
        job_id, _ = enqueue(self.db, 'old', 'app', 'a' * 40, True, 'main')
        with patch.object(worker, 'run') as commands, patch.object(worker, 'effective_projects') as policy:
            with self.assertRaisesRegex(worker.StepFailure, 'retry as a new run'):
                worker.project_for_job(self.db, self.job(job_id))
        commands.assert_not_called()
        policy.assert_not_called()

    def test_unconfigured_project_can_record_job_but_cannot_execute(self):
        registry.create_project(self.db, 'new', {'repository': 'TPCT/new', 'repository_id': 42})
        self.assertIsNone(capture_registered_target(self.db, 'new', 'main', 'a' * 40, 'deploy'))
        job_id, _ = enqueue(self.db, 'new-no-recipe', 'new', 'a' * 40, True, 'main',
                            target_factory=lambda db: capture_registered_target(db, 'new', 'main', 'a' * 40, 'deploy'))
        with self.assertRaises(worker.StepFailure):
            worker.project_for_job(self.db, self.job(job_id))

    def test_missing_repository_id_rolls_back_enqueue(self):
        self.db.execute("UPDATE registry_projects SET repository_id=NULL WHERE id='app'")
        with self.assertRaisesRegex(ValueError, 'Repository ID'):
            self.enqueue()
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_retention_removes_orphan_private_targets(self):
        job_id, _ = self.enqueue()
        self.db.execute("UPDATE jobs SET status='succeeded',updated=? WHERE id=?", (time.time() - 31 * 86400, job_id))
        self.enqueue('next')
        self.assertIsNone(self.job(job_id))
        self.assertIsNone(self.db.execute('SELECT 1 FROM job_targets WHERE job_id=?', (job_id,)).fetchone())

    def test_changed_job_source_is_rejected(self):
        job_id, _ = self.enqueue()
        self.db.execute('UPDATE jobs SET sha=? WHERE id=?', ('b' * 40, job_id))
        with self.assertRaisesRegex(worker.StepFailure, 'differs from its pinned'):
            worker.project_for_job(self.db, self.job(job_id))
