"""Worker integration boundaries for source publication and runtime completion."""
import contextlib
import tempfile
import sqlite3
import job_target
from pathlib import Path
import unittest
from unittest.mock import patch

import artifact_publication
import managed_delivery
import managed_rollout
import worker
import migration_jobs


class ManagedWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for target, value in ((worker, self.root),):
            p = patch.object(target, 'STATE', value); p.start(); self.addCleanup(p.stop)
        p = patch.object(worker.control, 'JOURNAL', self.root/'control.json'); p.start(); self.addCleanup(p.stop)

    def test_managed_source_dispatch_does_not_enter_legacy_compose_deployer(self):
        job = {'id': 4, 'mode': 'deploy'}
        project = {'recipe': {'schema_kind': 'managed-build'}}
        with patch.object(worker, 'build_artifacts') as build, patch.object(worker, 'validate_execution_recipe', side_effect=AssertionError('legacy recipe path')):
            worker.deploy('db', job, project, validate_only=False)
        build.assert_called_once_with('db', job, project, False)

    def test_pending_publication_does_not_prepare_or_mutate_runtime(self):
        with patch.object(artifact_publication, 'pending', return_value=True), patch.object(managed_delivery, 'prepare') as prepare, patch.object(managed_rollout, 'rollout') as rollout, patch.object(worker, 'set_status') as status:
            self.assertFalse(worker.process_managed_delivery('db', 4))
        prepare.assert_not_called(); rollout.assert_not_called(); status.assert_not_called()

    def test_success_is_recorded_only_after_verified_service_metadata(self):
        events = []
        item = {'service_id': 'service', 'state': 'pending'}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(artifact_publication, 'pending', return_value=False))
            stack.enter_context(patch.object(worker, 'process_managed_migration', return_value=True))
            stack.enter_context(patch.object(managed_delivery, 'prepare'))
            stack.enter_context(patch.object(managed_delivery, 'claim', return_value={'services':[item]}))
            stack.enter_context(patch.object(managed_delivery, 'public', return_value={'status':'succeeded'}))
            stack.enter_context(patch.object(managed_rollout, 'rollout', side_effect=lambda *a, **kw: events.append('runtime-verified') or {'id':'replacement'}))
            stack.enter_context(patch.object(managed_delivery, 'complete_service', side_effect=lambda *a: events.append('metadata-committed')))
            stack.enter_context(patch.object(worker, 'set_status', side_effect=lambda *a: events.append(a[2])))
            stack.enter_context(patch.object(worker, 'schedule_cleanup', side_effect=lambda *a, **kw: events.append('cleanup')))
            self.assertTrue(worker.process_managed_delivery('db', 4))
        self.assertEqual(events, ['runtime-verified', 'metadata-committed', 'succeeded', 'cleanup'])

    def test_idle_resume_recovers_only_published_managed_intents(self):
        db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.execute('CREATE TABLE jobs(id INTEGER,status TEXT)')
        db.execute('CREATE TABLE artifact_publications(job_id INTEGER,status TEXT)')
        db.executemany('INSERT INTO jobs VALUES(?,?)', [(1,'deploying'),(2,'interrupted'),(3,'deploying'),(4,'failed'),(5,'interrupted')])
        db.executemany('INSERT INTO artifact_publications VALUES(?,?)', [(1,'succeeded'),(2,'succeeded'),(3,'pending'),(4,'succeeded'),(5,'succeeded')])
        def target(_db, identifier):
            return {'recipe':{'schema_kind':'artifact-build' if identifier == 5 else 'managed-build'}}
        with patch.object(artifact_publication, 'initialize'), patch.object(job_target, 'load', side_effect=target), patch.object(worker, 'process_managed_delivery') as process:
            worker.resume_managed_deliveries(db)
        self.assertEqual([call.args[1] for call in process.call_args_list], [1, 2])

    def test_unverified_migration_blocks_first_container_replacement(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(artifact_publication, 'pending', return_value=False))
            stack.enter_context(patch.object(managed_delivery, 'prepare'))
            stack.enter_context(patch.object(managed_delivery, 'claim', return_value={'services':[{'service_id':'service','state':'pending'}]}))
            stack.enter_context(patch.object(worker, 'process_managed_migration', return_value=False))
            rollout=stack.enter_context(patch.object(managed_rollout,'rollout'))
            stack.enter_context(patch.object(managed_delivery,'fail'))
            stack.enter_context(patch.object(worker,'set_status'))
            self.assertFalse(worker.process_managed_delivery('db',4))
            rollout.assert_not_called()

    def test_metadata_cas_failure_never_claims_success(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(artifact_publication, 'pending', return_value=False))
            stack.enter_context(patch.object(worker, 'process_managed_migration', return_value=True))
            stack.enter_context(patch.object(managed_delivery, 'prepare'))
            stack.enter_context(patch.object(managed_delivery, 'claim', return_value={'services':[{'service_id':'service','state':'pending'}]}))
            stack.enter_context(patch.object(managed_rollout, 'rollout', return_value={'id':'replacement'}))
            stack.enter_context(patch.object(managed_delivery, 'complete_service', side_effect=ValueError('revision drift')))
            fail = stack.enter_context(patch.object(managed_delivery, 'fail'))
            status = stack.enter_context(patch.object(worker, 'set_status'))
            self.assertFalse(worker.process_managed_delivery('db', 4))
        self.assertTrue(fail.call_args.kwargs['recovery_required'])
        self.assertEqual(status.call_args.args[2], 'failed')



class ManagedSourceCompletionTests(unittest.TestCase):
    from test_worker_artifact_build import ArtifactBuildExecutionTests as Fixture
    setUp = Fixture.setUp
    fake_run = Fixture.fake_run
    execute = Fixture.execute

    def test_build_publishes_durable_intent_without_reentering_control_lock(self):
        self.job['mode'] = 'deploy'
        self.recipe['schema_kind'] = 'managed-build'
        self.recipe['managed_target'] = {'pinned': True}
        with patch.object(migration_jobs, 'optional', return_value=None), patch.object(managed_delivery, 'load_target', return_value={'services': [{'definition': {'artifact': {'source_sha': 'a'*40}}}]}), patch.object(worker, 'schema_change_paths', return_value=[]), patch.object(worker, 'process_managed_delivery', side_effect=AssertionError('Nested runtime dispatch')):
            self.execute(False)
        self.mocks[3].assert_called_once()
        self.assertEqual(self.mocks[3].call_args.args[4], 'deploying')
