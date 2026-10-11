"""Managed deployments stop at unsupported schema changes before image effects."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import worker
import test_worker_artifact_build as fixture

DETECT = worker.schema_change_paths


class SourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.source = Path(tmp.name)
        state = patch.object(worker, "STATE", self.source); state.start(); self.addCleanup(state.stop)
        self.git('init', '-q')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        self.write('database/migrations/001.php', '<?php // existing')
        self.old = self.commit()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.source, text=True).strip()

    def write(self, name, content):
        path = self.source/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content)

    def commit(self):
        self.git('add', '-A'); self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def check(self, sha, baseline=None):
        worker.check_managed_migration_capability(self.source, sha,
            {'services': [{'definition': {'artifact': {'source_sha': baseline}}}]},
            self.git('ls-files').splitlines())

    def test_unchanged_migrations_allow_application_only_change(self):
        self.write('app.py', 'print(1)'); self.check(self.commit(), self.old)

    def test_added_changed_deleted_and_renamed_migrations_rejected(self):
        for kind in ('added', 'changed', 'deleted', 'renamed'):
            with self.subTest(kind=kind):
                self.git('reset', '--hard', self.old)
                path = self.source/'database/migrations/001.php'
                self.write('aaa.txt', 'also changed')
                if kind == 'added': self.write('alembic/versions/new.py', 'revision=2')
                if kind == 'changed': path.write_text('<?php // modified')
                if kind == 'deleted': path.unlink()
                if kind == 'renamed': path.rename(self.source/'ordinary.php')
                with self.assertRaisesRegex(worker.StepFailure, 'runner is unsupported') as error:
                    self.check(self.commit(), self.old)
                self.assertIn('database/migrations/001.php' if kind != 'added' else 'alembic/versions/new.py', str(error.exception))

    def test_missing_baseline_current_migrations_rejected(self):
        with self.assertRaisesRegex(worker.StepFailure, 'current source'):
            self.check(self.old)

    def test_every_selected_baseline_is_checked(self):
        self.write('database/migrations/002.php', '<?php // new')
        current = self.commit()
        selection = {'services': [
            {'definition': {'artifact': {'source_sha': current}}},
            {'definition': {'artifact': {'source_sha': self.old}}}]}
        with self.assertRaisesRegex(worker.StepFailure, 'runner is unsupported'):
            worker.check_managed_migration_capability(self.source, current, selection,
                self.git('ls-files').splitlines())

    def test_no_baseline_without_schema_paths_remains_supported(self):
        worker.check_managed_migration_capability(self.source, self.old,
            {'services': [{'definition': {'artifact': {}}}]}, ['app.py', 'Dockerfile'])

    def test_untrusted_path_diagnostics_are_bounded_and_escaped(self):
        paths = ['database/migrations/' + str(i) + chr(27) + 'x'*300 + '.php' for i in range(20)]
        with self.assertRaises(worker.StepFailure) as error:
            worker.check_managed_migration_capability(self.source, self.old,
                {'services': [{'definition': {'artifact': {}}}]}, paths)
        message = str(error.exception)
        self.assertNotIn(chr(27), message)
        self.assertIn('additional paths omitted', message)
        self.assertLess(len(message), 2000)

    def test_unavailable_baseline_fails_closed(self):
        with self.assertRaisesRegex(worker.StepFailure, 'comparison failed'):
            self.check(self.old, 'f'*40)


class BuildBoundaryTests(unittest.TestCase):
    setUp = fixture.ArtifactBuildExecutionTests.setUp
    fake_run = fixture.ArtifactBuildExecutionTests.fake_run
    execute = fixture.ArtifactBuildExecutionTests.execute

    def test_managed_change_never_builds_publishes_or_cuts_over(self):
        self.job['mode'] = 'deploy'; self.recipe['schema_kind'] = 'managed-build'
        self.recipe['managed_target'] = {'pinned': True}
        self.tracked += 'prisma/schema.prisma\n'
        with patch('managed_delivery.load_target', return_value={'services': [{'definition': {'artifact': {}}}]}), patch.object(worker, 'schema_change_paths', DETECT):
            with self.assertRaisesRegex(worker.StepFailure, 'runner is unsupported'):
                self.execute(False)
        self.assertFalse(any(call[0] == worker.DOCKER for call in self.calls))
        self.mocks[3].assert_not_called()

    def test_validation_of_migration_source_still_isolated(self):
        self.tracked += 'database/migrations/new.php\n'
        self.execute(True)
        self.assertEqual(self.mocks[3].call_args.args[4], 'validated')
