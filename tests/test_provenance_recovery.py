"""Crash recovery uses real SQLite bindings/journals; Docker calls are isolated."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import control
import provenance_store
import registry
import worker
from compose_reconciliation import reconcile
from store import connect, enqueue


class ProvenanceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / 'state.sqlite'
        self.db = connect(self.database)
        self.compose = self.root / 'compose.yml'; self.compose.write_text('new runtime configuration')
        self.env = self.root / '.env'; self.env.write_text('APP_IMAGE=new\n')
        self.recipe = {'compose_project': 'app', 'compose_files': [str(self.compose)],
                       'services': ['web'], 'env_file': str(self.env)}
        config = {'services': {'web': {'image': 'app:old', 'network_mode': 'none'}}}
        def proof(value):
            return reconcile('web', config, config, [str(self.root / 'original.yml')],
                self.recipe['compose_files'], compose_project='app', environment_context_digest=value * 64)['proof']
        self.recipe['service_provenance'] = {'web': proof('a')}
        registry.import_legacy(self.db, {'app': {'repository': 'TPCT/app', 'branch': 'main', 'recipe': self.recipe}}, [])
        registry.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        self.old = registry.get_environment(self.db, 'app', 'production')['recipe_revision']
        self.transition = provenance_store.prepare(self.db, 'app', self.recipe, lambda *args: proof('b'))
        self.job, _ = enqueue(self.db, 'recovery-fixture', 'app', 'a' * 40, True, 'main')
        self.journal = self.root / 'journal.json'

    def tearDown(self):
        self.db.close(); self.temp.cleanup()

    def assert_old_binding(self):
        reopened = connect(self.database)
        try:
            self.assertEqual(registry.get_environment(reopened, 'app', 'production')['recipe_revision'], self.old)
            self.assertEqual(registry.legacy_projects(reopened)['app']['recipe'], self.recipe)
        finally:
            reopened.close()

    def worker_journal(self):
        self.journal.write_text(json.dumps({'job_id': self.job, 'recipe': self.recipe,
            'previous_env': {'APP_IMAGE': 'old'}, 'provenance_transition': self.transition}))

    def control_journal(self):
        self.journal.write_text(json.dumps({'path': str(self.compose), 'before': 'old runtime configuration',
            'mode': 0o600, 'compose': ['compose'], 'service': 'web', 'container': 'app-web-1',
            'image_id': 'image-old', 'environment': {}, 'activated': True, 'was_running': False,
            'provenance_transition': self.transition}))

    def recover_worker(self, committed):
        if committed: provenance_store.commit(self.db, self.transition)
        self.worker_journal()
        with patch.object(worker, 'run'), patch.object(worker, 'verify_runtime') as verify:
            worker.recover(self.db, self.journal)
            verify.assert_called_once_with(self.recipe)
        self.assertFalse(self.journal.exists())
        self.assertIn('APP_IMAGE=old', self.env.read_text())
        self.assertEqual(self.db.execute('SELECT status FROM jobs WHERE id=?', (self.job,)).fetchone()[0], 'rolled-back')
        self.assert_old_binding()

    def recover_control(self, committed):
        if committed: provenance_store.commit(self.db, self.transition)
        self.control_journal()
        with patch.multiple(control, ROOT=self.root, DATABASE=str(self.database), JOURNAL=self.journal), \
             patch.object(control, 'configuration'), patch.object(control, 'assert_image'), \
             patch.object(control, 'recreate') as recreate, \
             patch.object(control, 'wait_runtime', return_value={'Image': 'image-old', 'Config': {'Env': []}}):
            control.restore_environment()
            recreate.assert_called_once_with(['compose'], 'web', False)
        self.assertEqual(self.compose.read_text(), 'old runtime configuration')
        self.assertFalse(self.journal.exists())
        self.assert_old_binding()

    def test_worker_crash_before_registry_commit_restores_original_binding(self): self.recover_worker(False)
    def test_worker_crash_after_registry_commit_restores_original_binding(self): self.recover_worker(True)
    def test_control_crash_before_registry_commit_restores_original_binding(self): self.recover_control(False)
    def test_control_crash_after_registry_commit_restores_original_binding(self): self.recover_control(True)

    def test_runtime_failure_keeps_new_binding_and_journal_for_review(self):
        provenance_store.commit(self.db, self.transition); self.worker_journal()
        with patch.object(worker, 'run'), patch.object(worker, 'verify_runtime', side_effect=worker.StepFailure('runtime failed')):
            with self.assertRaises(worker.StepFailure): worker.recover(self.db, self.journal)
        self.assertTrue(self.journal.exists())
        self.assertNotEqual(registry.get_environment(self.db, 'app', 'production')['recipe_revision'], self.old)
        self.assertEqual(self.db.execute('SELECT status FROM jobs WHERE id=?', (self.job,)).fetchone()[0], 'recovering')

    def test_metadata_failure_keeps_journal_and_does_not_mark_success(self):
        provenance_store.commit(self.db, self.transition); self.worker_journal()
        original = worker.set_status
        def status(db, job, state, detail):
            if state == 'rolled-back': raise RuntimeError('metadata unavailable')
            return original(db, job, state, detail)
        with patch.object(worker, 'run'), patch.object(worker, 'verify_runtime'), patch.object(worker, 'set_status', side_effect=status):
            with self.assertRaisesRegex(RuntimeError, 'metadata'): worker.recover(self.db, self.journal)
        self.assertTrue(self.journal.exists())
        self.assert_old_binding()
        self.assertEqual(self.db.execute('SELECT status FROM jobs WHERE id=?', (self.job,)).fetchone()[0], 'recovering')
        with patch.object(worker, 'run'), patch.object(worker, 'verify_runtime'):
            worker.recover(self.db, self.journal)
        self.assertFalse(self.journal.exists())
        self.assert_old_binding()

    def test_control_registry_conflict_keeps_journal_and_preserves_current_preferences(self):
        provenance_store.commit(self.db, self.transition)
        env = registry.get_environment(self.db, 'app', 'production')
        registry.update_environment(self.db, 'app', 'production', env['revision'], {'branch': 'changed'})
        self.control_journal()
        with patch.multiple(control, ROOT=self.root, DATABASE=str(self.database), JOURNAL=self.journal), \
             patch.object(control, 'configuration'), patch.object(control, 'assert_image'), patch.object(control, 'recreate'), \
             patch.object(control, 'wait_runtime', return_value={'Image': 'image-old', 'Config': {'Env': []}}):
            with self.assertRaisesRegex(ValueError, 'reconciliation'): control.restore_environment()
        self.assertTrue(self.journal.exists())
        self.assertEqual(registry.get_environment(self.db, 'app', 'production')['branch'], 'changed')
