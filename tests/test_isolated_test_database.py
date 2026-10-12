import unittest
from unittest.mock import patch, Mock
from isolated_test_database import database


class IsolatedDatabaseTests(unittest.TestCase):
    def test_default_has_no_network_or_database(self):
        run = Mock()
        with database({}, 'deploy-test-1-0', 'docker', run) as (network, env):
            self.assertEqual((network, env), ('none', {}))
        run.assert_not_called()

    @patch('isolated_test_database.subprocess.run')
    def test_postgres_is_internal_ephemeral_and_cleaned_after_test_failure(self, command):
        command.return_value.returncode = 0
        run = Mock()
        with self.assertRaisesRegex(RuntimeError, 'test failed'):
            with database({'database': 'postgres'}, 'deploy-test-1-0', 'docker', run) as (network, env):
                self.assertEqual(network, 'deploy-test-1-0-net')
                self.assertEqual(env['DB_DATABASE'], 'deploy_test')
                self.assertEqual(env['DB_HOST'], 'test-postgres')
                self.assertEqual(env['DATABASE_URL'], '')
                self.assertGreater(len(env['DB_PASSWORD']), 30)
                raise RuntimeError('test failed')
        args = [call.args[0] for call in run.call_args_list]
        self.assertIn('--internal', args[0])
        self.assertIn('--read-only', args[1])
        self.assertNotIn('-v', args[1])
        self.assertNotIn('-p', args[1])
        cleanup = [call.args[0] for call in command.call_args_list]
        self.assertIn(['docker', 'rm', '-f', 'deploy-test-1-0-db'], cleanup)
        self.assertIn(['docker', 'network', 'rm', 'deploy-test-1-0-net'], cleanup)

    def test_unknown_database_and_unsafe_name_are_rejected_before_mutation(self):
        for test, name in [({'database': 'mysql'}, 'deploy-test-1-0'), ({'database': 'postgres'}, 'production')]:
            run = Mock()
            with self.assertRaises(ValueError):
                with database(test, name, 'docker', run):
                    pass
            run.assert_not_called()
