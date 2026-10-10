import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import job_target
from store import connect


class JobTargetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temporary.name) / 'state.sqlite')
        self.values = dict(project='app', environment='production', repository='TPCT/app',
                           repository_id=12, branch='main', sha='a' * 40, mode='deploy',
                           recipe_revision=1, project_revision=2, environment_revision=3,
                           recipe={'compose_project': 'app', 'compose_files': ['/managed/app/compose.yml'],
                                   'env_file': '/managed/app/.env', 'services': ['web'],
                                   'tests': [{'command': 'private-diagnostic-token'}]})
        self.db.execute("INSERT INTO jobs(id,delivery,project,sha,branch,mode,created,updated,status) VALUES(1,'delivery','app',?,'main','deploy',1,1,'queued')", ('a' * 40,))

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def test_detached_capture_and_private_public_separation(self):
        target = job_target.capture(**self.values)
        self.values['recipe']['services'].append('later')
        response = job_target.store(self.db, 1, target)
        self.assertNotIn('/managed', json.dumps(response))
        self.assertNotIn('private-diagnostic-token', json.dumps(response))
        self.assertNotIn('recipe', response)
        self.assertEqual(job_target.public(self.db, 1), response)
        self.assertNotIn('private-diagnostic-token', json.dumps(dict(self.db.execute('SELECT * FROM jobs').fetchone())))
        loaded = job_target.load(self.db, 1)
        self.assertEqual(loaded['recipe']['services'], ['web'])
        loaded['recipe']['services'].append('mutation')
        self.assertEqual(job_target.load(self.db, 1)['recipe']['services'], ['web'])

    def test_exact_retry_is_idempotent_and_changed_recipe_conflicts(self):
        target = job_target.capture(**self.values)
        self.assertEqual(job_target.store(self.db, 1, target), job_target.store(self.db, 1, target))
        other = copy.deepcopy(self.values)
        other['recipe']['compose_project'] = 'other'
        with self.assertRaisesRegex(ValueError, 'different immutable target'):
            job_target.store(self.db, 1, job_target.capture(**other))

    def test_target_mutation_and_replace_are_blocked(self):
        target = job_target.capture(**self.values)
        job_target.store(self.db, 1, target)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE job_targets SET fingerprint='changed' WHERE job_id=1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT OR REPLACE INTO job_targets VALUES(1,'changed','{}',1)")
        self.assertEqual(job_target.load(self.db, 1), target)

    def test_caller_transaction_can_rollback_job_and_target_together(self):
        self.db.execute('BEGIN IMMEDIATE')
        self.db.execute("INSERT INTO jobs(id,delivery,project,sha,branch,mode,created,updated,status) VALUES(2,'second','app',?,'main','deploy',1,1,'queued')", ('a' * 40,))
        job_target.store(self.db, 2, job_target.capture(**self.values))
        self.assertTrue(self.db.in_transaction)
        self.db.execute('ROLLBACK')
        self.assertIsNone(self.db.execute('SELECT 1 FROM jobs WHERE id=2').fetchone())
        with self.assertRaisesRegex(ValueError, 'no pinned target'):
            job_target.load(self.db, 2)

    def test_explicit_legacy_classification_has_no_invented_environment(self):
        values = dict(self.values, environment='legacy-unclassified', recipe_revision=None, environment_revision=None)
        target = job_target.capture(**values)
        self.assertEqual(target['environment'], 'legacy-unclassified')
        self.assertIsNone(target['environment_revision'])
        with self.assertRaises(ValueError):
            job_target.capture(**dict(values, environment_revision=1))
        for environment in (None, '', 'staging', []):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                job_target.capture(**dict(values, environment=environment))

    def test_revision_and_recipe_validation_fail_closed(self):
        for field in ('recipe_revision', 'project_revision', 'environment_revision', 'repository_id'):
            for value in (True, 1.0, -1, '1', 2 ** 63):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    job_target.capture(**dict(self.values, **{field: value}))
        for recipe in (None, {}, {'compose_project': 'app', 'compose_files': ['relative.yml'], 'services': ['web']}):
            with self.subTest(recipe=recipe), self.assertRaises(ValueError):
                job_target.capture(**dict(self.values, recipe=recipe))
        with self.assertRaises(ValueError):
            job_target.capture(**dict(self.values, recipe_revision=None))

    def test_fingerprint_binds_target_and_source_and_rejects_tampering(self):
        target = job_target.capture(**self.values)
        for changes in ({'sha': 'b' * 40}, {'branch': 'development'}, {'mode': 'validate'}, {'environment': 'development'}, {'repository_id': 13}):
            self.assertFalse(job_target.matches(target, job_target.capture(**dict(self.values, **changes))))
        target['recipe']['services'].append('injected')
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            job_target.store(self.db, 1, target)

    def test_target_cannot_attach_to_wrong_or_missing_job(self):
        target = job_target.capture(**self.values)
        for job_id in (2, True, 1.0):
            with self.subTest(job_id=job_id), self.assertRaises(ValueError):
                job_target.store(self.db, job_id, target)
        with self.assertRaisesRegex(ValueError, 'identity'):
            job_target.store(self.db, 1, job_target.capture(**dict(self.values, mode='validate')))

    def test_recipe_key_order_does_not_change_fingerprint(self):
        reordered = dict(self.values, recipe=dict(reversed(list(self.values['recipe'].items()))))
        self.assertTrue(job_target.matches(job_target.capture(**self.values), job_target.capture(**reordered)))

    def test_nonfinite_private_recipe_and_future_schema_rejected(self):
        values = copy.deepcopy(self.values)
        values['recipe']['extra'] = float('nan')
        with self.assertRaises(ValueError):
            job_target.capture(**values)
        target = job_target.capture(**self.values)
        for version in (True, 2):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'schema version'):
                job_target.store(self.db, 1, dict(target, schema_version=version))
