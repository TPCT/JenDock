import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from store import connect, enqueue
from worker import atomic_json, env_values, recover, replace_env


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = self.root / 'ports.env'
        self.env.write_text('# preserve config\nAPP_IMAGE=old:123\nOTHER=keep\n')
        self.env.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def test_targeted_atomic_env_change_preserves_other_settings_and_permissions(self):
        replace_env(self.env, {'APP_IMAGE': 'new:456'})
        self.assertEqual(self.env.read_text(), '# preserve config\nAPP_IMAGE=new:456\nOTHER=keep\n')
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(env_values(self.env, ['APP_IMAGE']), {'APP_IMAGE': 'new:456'})

    def test_interrupted_deployment_recovers_from_persisted_journal(self):
        db = connect(self.root / 'state.sqlite')
        job, _ = enqueue(db, 'delivery', 'test', 'a' * 40, True)
        recipe = {'compose_project': 'test', 'compose_files': ['compose.yml'], 'env_file': str(self.env), 'services': ['app']}
        journal = self.root / 'transaction.json'
        atomic_json(journal, {'job_id': job, 'recipe': recipe, 'previous_env': {'APP_IMAGE': 'old:123'}})
        replace_env(self.env, {'APP_IMAGE': 'new:456'})
        with patch('worker.run') as run, patch('worker.verify_runtime') as verify:
            recover(db, journal)
        self.assertEqual(env_values(self.env, ['APP_IMAGE']), {'APP_IMAGE': 'old:123'})
        self.assertFalse(journal.exists())
        self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()['status'], 'rolled-back')
        self.assertIn('--no-deps', run.call_args.args[0])
        verify.assert_called_once_with(recipe)
        db.close()

    def test_version_update_preserves_configuration_symlink(self):
        alias = self.root / '.env'
        alias.symlink_to(self.env.name)
        replace_env(alias, {'APP_IMAGE': 'new:789'})
        self.assertTrue(alias.is_symlink())
        self.assertEqual(env_values(self.env, ['APP_IMAGE']), {'APP_IMAGE': 'new:789'})

    def test_failed_rollback_keeps_journal_and_does_not_claim_success(self):
        db = connect(self.root / 'state.sqlite')
        job, _ = enqueue(db, 'delivery', 'test', 'a' * 40, True)
        recipe = {'compose_project': 'test', 'compose_files': [], 'env_file': str(self.env), 'services': ['app']}
        journal = self.root / 'transaction.json'
        atomic_json(journal, {'job_id': job, 'recipe': recipe, 'previous_env': {'APP_IMAGE': 'old:123'}})
        with patch('worker.run', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError): recover(db, journal)
        self.assertTrue(journal.exists())
        self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()['status'], 'recovering')
        db.close()


if __name__ == '__main__': unittest.main()
