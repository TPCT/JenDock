import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import worker
from store import connect, enqueue


class MigrationTests(unittest.TestCase):
    def project(self):
        return {'environment': 'development', 'recipe': {
            'compose_project': 'test', 'compose_files': ['compose.yml'],
            'services': ['app'], 'images': [{'name': 'app'}],
            'migration': {'framework': 'laravel', 'service': 'app', 'image': 'app'},
        }}

    def test_policy_is_explicitly_scoped_to_development(self):
        project = self.project()
        self.assertEqual(worker.migration_policy(project)['service'], 'app')
        project['environment'] = 'production'
        with self.assertRaises(worker.StepFailure):
            worker.migration_policy(project)

    def test_policy_cannot_target_another_project_service(self):
        project = self.project()
        project['recipe']['migration']['service'] = 'other-app'
        with self.assertRaises(worker.StepFailure):
            worker.migration_policy(project)

    def test_migration_uses_candidate_image_and_existing_compose_database_config(self):
        project = self.project()
        def execute(args, **kwargs):
            override = Path(args[args.index('run') - 1])
            self.assertEqual(json.loads(override.read_text()), {'services': {'app': {'image': 'app:candidate'}}})
            self.assertIn('--no-deps', args)
            self.assertEqual(args[-5:], ['app', 'artisan', 'migrate', '--force', '--no-interaction'])
            self.assertIn('compose.yml', args)
            self.assertNotIn('migrate:fresh', args)
        with patch('worker.run', side_effect=execute) as run:
            worker.run_migrations(project['recipe'], worker.migration_policy(project), {'app': 'app:candidate'})
        run.assert_called_once()

    def test_interrupted_migration_never_claims_database_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = connect(root / 'state.sqlite')
            job, _ = enqueue(db, 'migration', 'test', 'a' * 40, True)
            journal = root / 'transaction.json'
            worker.atomic_json(journal, {'job_id': job, 'recipe': self.project()['recipe'],
                                        'previous_env': {}, 'migration_started': True})
            with patch('worker.run') as run, self.assertRaises(worker.StepFailure):
                worker.recover(db, journal)
            run.assert_not_called()
            self.assertTrue(journal.exists())
            self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()['status'], 'needs-review')
            db.close()

    def exercise_deployment(self, validate=False, fail_build=False, fail_migration=False, target_changed=False, fail_unlink=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / '.env'
            env.write_text('APP_IMAGE=app:aaaaaaa\n')
            project = self.project()
            project.update(repository='TPCT/test', branch='development')
            if fail_unlink:
                project['recipe'].pop('migration')
            project['recipe'].update(env_file=str(env),
                images=[{'name': 'app', 'env_key': 'APP_IMAGE', 'dockerfile': 'Dockerfile'}],
                tests=[{'image': 'app', 'command': ['php', '-v']}], http_checks=[{'url': 'http://localhost', 'statuses': [200]}])
            db = connect(root / 'state.sqlite')
            job_id, _ = enqueue(db, 'flow', 'test', 'b' * 40, True, branch='development', mode='validate' if validate else 'deploy')
            job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            commands = []
            def execute(args, **kwargs):
                commands.append(args)
                if args[:2] == ['git', 'clone']:
                    Path(args[-1]).mkdir()
                if args[:2] == ['git', 'rev-parse']: return 'b' * 40
                if args[:2] == ['git', 'diff']: return '' if fail_unlink else 'database/migrations/new.php'
                if args[:2] == ['git', 'ls-files']: return 'Dockerfile\napp/Test.php'
                if args[:2] == ['git', 'ls-remote']: return 'b' * 40 + '\trefs/heads/main'
                if 'inspect' in args: return 'a' * 40
                if 'build' in args and fail_build: raise worker.StepFailure('Build failed')
                if 'artisan' in args and fail_migration: raise worker.StepFailure('Migration failed')
                return ''
            temp_factory = tempfile.TemporaryDirectory
            def temporary(*args, **kwargs):
                kwargs.pop('dir', None)
                return temp_factory(*args, **kwargs)
            original_unlink = Path.unlink
            unlink_failed = False
            def unlink(path, *args, **kwargs):
                nonlocal unlink_failed
                if fail_unlink and path == root / 'transaction.json' and not unlink_failed:
                    unlink_failed = True
                    raise OSError('Journal removal failed')
                return original_unlink(path, *args, **kwargs)
            with patch.object(Path, 'unlink', autospec=True, side_effect=unlink), patch('worker.STATE', root), patch('worker.run', side_effect=execute), \
                 patch('worker.containers', return_value=[{'Image': 'app:aaaaaaa'}]), \
                 patch('worker.verify_runtime'), patch('worker.verify_target_ownership', side_effect=[{'c' * 64}, worker.StepFailure('Target changed')] if target_changed else None, return_value={'c' * 64}), patch('worker.subprocess.run'), \
                 patch('worker.tempfile.TemporaryDirectory', side_effect=temporary):
                if fail_build or fail_migration or target_changed:
                    with self.assertRaises(worker.StepFailure):
                        worker.deploy(db, job, project, validate_only=validate)
                else:
                    worker.deploy(db, job, project, validate_only=validate)
            state = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
            result = (commands, state, env.read_text(), (root / 'transaction.json').exists())
            db.close()
            return result

    def test_development_build_tests_migrate_and_cutover_in_order(self):
        commands, state, env, journal = self.exercise_deployment()
        migration_index = next(i for i, args in enumerate(commands) if 'artisan' in args)
        test_index = next(i for i, args in enumerate(commands) if 'deploy-test-' in ' '.join(args))
        deploy_index = next(i for i, args in enumerate(commands) if 'up' in args)
        self.assertLess(test_index, migration_index)
        self.assertLess(migration_index, deploy_index)
        self.assertEqual(state['status'], 'succeeded')
        self.assertIn('bbbbbbbbbbbb', env)
        self.assertFalse(journal)

    def test_validation_builds_without_database_or_service_changes(self):
        commands, state, env, journal = self.exercise_deployment(validate=True)
        self.assertTrue(any('build' in args for args in commands))
        self.assertFalse(any('artisan' in args or 'up' in args for args in commands))
        self.assertEqual(state['status'], 'validated')
        self.assertEqual(env, 'APP_IMAGE=app:aaaaaaa\n')
        self.assertFalse(journal)

    def test_failed_build_never_starts_migration(self):
        commands, _, _, journal = self.exercise_deployment(fail_build=True)
        self.assertFalse(any('artisan' in args or 'up' in args for args in commands))
        self.assertFalse(journal)

    def test_failed_migration_keeps_live_image_and_recovery_evidence(self):
        commands, state, env, journal = self.exercise_deployment(fail_migration=True)
        self.assertFalse(any('up' in args for args in commands))
        self.assertEqual(state['status'], 'needs-review')
        self.assertEqual(env, 'APP_IMAGE=app:aaaaaaa\n')
        self.assertTrue(journal)

    def test_replacement_during_build_blocks_migration_and_cutover(self):
        commands, _, env, journal = self.exercise_deployment(target_changed=True)
        self.assertTrue(any('build' in args for args in commands))
        self.assertFalse(any('artisan' in args or 'up' in args for args in commands))
        self.assertEqual(env, 'APP_IMAGE=app:aaaaaaa\n')
        self.assertFalse(journal)

    def test_failed_journal_removal_rolls_back_without_success_status(self):
        commands, state, env, journal = self.exercise_deployment(fail_unlink=True)
        self.assertEqual(state['status'], 'rolled-back')
        self.assertEqual(env, 'APP_IMAGE=app:aaaaaaa\n')
        self.assertFalse(journal)
        self.assertEqual(sum('up' in args for args in commands), 2)
