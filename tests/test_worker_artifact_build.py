import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build_profiles
import worker


class ArtifactBuildExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recipe = build_profiles._recipe('example', 'development', {
            'dockerfile': 'Dockerfile', 'context': '.', 'target': None,
            'test_command': ['python', '-c', 'assert True'], 'command': ['python', 'app.py'],
            'healthcheck': ['python', 'health.py'], 'user': '65534:65534'})
        self.project = {'repository': 'TPCT/example', 'branch': 'main', 'environment': 'development', 'recipe': self.recipe}
        self.job = {'id': 12, 'project': 'example', 'sha': 'a'*40, 'branch': 'main', 'mode': 'validate'}
        self.calls = []; self.tracked = 'Dockerfile\napp.py\n'; self.head = 'a'*40; self.symlink = False; self.test_failure = False
        self.patches = [patch.object(worker, 'ARTIFACT_WORK', self.root),
            patch.object(worker, 'run', side_effect=self.fake_run), patch.object(worker, 'set_status'),
            patch.object(worker, 'complete_build')]
        for target in ('containers', 'migration_policy', 'schema_change_paths', 'verify_runtime', 'verify_target_ownership', 'env_values'):
            self.patches.append(patch.object(worker, target, side_effect=AssertionError('Runtime path called: '+target)))
        self.mocks = [patcher.start() for patcher in self.patches]
        for patcher in self.patches: self.addCleanup(patcher.stop)

    def fake_run(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ['git', 'clone']:
            source = Path(args[-1]); source.mkdir()
            if self.symlink:
                foreign = self.root/'foreign'; foreign.write_text('FROM busybox')
                (source/'Dockerfile').symlink_to(foreign)
            else: (source/'Dockerfile').write_text('FROM busybox')
        elif args[:2] == ['git', 'rev-parse']: return self.head + '\n'
        elif args[:2] == ['git', 'ls-files']: return self.tracked
        elif args[:2] == [worker.DOCKER, 'run'] and self.test_failure:
            raise worker.StepFailure('Isolated test failed')
        return ''

    def execute(self, validate_only=True):
        worker.deploy('db', self.job, self.project, validate_only=validate_only)

    def test_new_project_builds_without_any_runtime_recipe_or_observation(self):
        self.execute()
        self.assertFalse({'compose_project', 'compose_files', 'env_file', 'http_checks'} & set(self.recipe))
        builds = [args for args in self.calls if args[:2] == [worker.DOCKER, 'build']]
        self.assertEqual(len(builds), 1)
        self.assertIn('org.opencontainers.image.revision='+'a'*40, builds[0])
        tests = [args for args in self.calls if args[:2] == [worker.DOCKER, 'run']]
        self.assertEqual(tests[0][tests[0].index('--network')+1], 'none')
        self.assertNotIn('-v', tests[0]); self.assertNotIn('-e', tests[0])
        self.mocks[3].assert_called_once()
        self.assertEqual(self.mocks[3].call_args.args[4], 'validated')

    def test_deploy_mode_rejected_before_clone_or_docker(self):
        self.job['mode'] = 'deploy'
        with self.assertRaisesRegex(worker.StepFailure, 'validation builds only'): self.execute(False)
        self.assertEqual(self.calls, [])

    def test_invalid_profile_and_foreign_image_rejected_before_external_calls(self):
        self.recipe['env_file'] = '/production/.env'
        with self.assertRaisesRegex(worker.StepFailure, 'profile is invalid'): self.execute()
        self.assertEqual(self.calls, [])
        del self.recipe['env_file']
        self.project['recipe'] = build_profiles._recipe('other', 'development', {
            'dockerfile':'Dockerfile','context':'.','target':None,'test_command':['true'],
            'command':['app'],'healthcheck':['health'],'user':'1000:1000'})
        with self.assertRaisesRegex(worker.StepFailure, 'different project'): self.execute()
        self.assertEqual(self.calls, [])

    def test_changed_branch_head_never_builds_different_sha(self):
        self.head = 'b'*40; self.execute()
        self.assertFalse(any(args[0] == worker.DOCKER for args in self.calls))
        self.mocks[3].assert_not_called()
        self.assertEqual(self.mocks[2].call_args.args[2], 'superseded')

    def test_sensitive_tracked_file_blocks_before_docker(self):
        self.tracked += 'config/.env\n'
        with self.assertRaisesRegex(worker.StepFailure, 'sensitive configuration'): self.execute()
        self.assertFalse(any(args[0] == worker.DOCKER for args in self.calls))

    def test_checkout_symlink_escape_rejected_before_build(self):
        self.symlink = True
        with self.assertRaisesRegex(worker.StepFailure, 'within the pinned checkout'): self.execute()
        self.assertFalse(any(args[0] == worker.DOCKER for args in self.calls))

    def test_failed_isolated_test_never_publishes_artifact(self):
        self.test_failure = True
        with self.assertRaisesRegex(worker.StepFailure, 'Isolated test failed'): self.execute()
        self.mocks[3].assert_not_called()

    def test_missing_checkout_context_has_actionable_error_before_build(self):
        self.recipe['images'][0]['context'] = 'missing-directory'
        with self.assertRaisesRegex(worker.StepFailure, 'missing from the pinned checkout'): self.execute()
        self.assertFalse(any(args[0] == worker.DOCKER for args in self.calls))
