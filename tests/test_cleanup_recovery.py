"""Desired cleanup recovery contract; Docker is completely mocked."""
import contextlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import worker


class StopLoop(BaseException):
    pass


class CleanupRecoveryTests(unittest.TestCase):
    def test_nonzero_job_image_removal_is_not_silently_successful(self):
        image='sha256:'+'a'*64
        with patch('artifact_publication.pending',return_value=False), \
             patch('managed_services.protected_images',return_value=set()), \
             patch('managed_delivery.protected_images',return_value=set()), \
             patch.object(worker,'run',return_value=image), \
             patch.object(worker.subprocess,'run',return_value=subprocess.CompletedProcess([],1)):
            with self.assertRaises(worker.StepFailure):
                worker.prune_job_images(MagicMock(),12)

    def test_nonzero_previous_image_removal_is_not_silently_successful(self):
        image='sha256:'+'a'*64
        target={'recipe':{'schema_kind':'managed-build','managed_target':{'services':[{'definition':{'artifact':{'image':image}}}]}}}
        with patch('job_target.load',return_value=target), \
             patch.object(worker,'run',return_value=image), \
             patch('managed_services.protected_images',return_value=set()), \
             patch('managed_delivery.protected_images',return_value=set()), \
             patch.object(worker.subprocess,'run',return_value=subprocess.CompletedProcess([],1)):
            with self.assertRaises(worker.StepFailure):
                worker.prune_managed_previous_images(MagicMock(),12)

    def test_cache_timeout_after_success_does_not_terminate_worker_loop(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            root=Path(temporary)
            from store import connect
            db=connect(root/'state.sqlite')
            self.addCleanup(db.close)
            db.execute("INSERT INTO jobs(id,delivery,project,sha,status,updated,created) VALUES(12,'cleanup','app',?,'queued',1,1)",('a'*40,))
            stack.enter_context(patch.object(worker,'STATE',root))
            stack.enter_context(patch.object(worker.control,'JOURNAL',root/'environment.json'))
            stack.enter_context(patch.object(worker.Path,'mkdir'))
            stack.enter_context(patch.object(worker,'connect',return_value=db))
            stack.enter_context(patch.object(worker.threading,'Thread'))
            stack.enter_context(patch.object(worker.control,'start'))
            stack.enter_context(patch('artifact_publication.process'))
            for name in ('recover_managed_deliveries','resume_managed_deliveries','process_delivery_proof_once','process_service_once','process_domain_once','deploy','prune_job_images'):
                stack.enter_context(patch.object(worker,name))
            stack.enter_context(patch.object(worker,'project_for_job',return_value={}))
            stack.enter_context(patch.object(worker.subprocess,'run',side_effect=subprocess.TimeoutExpired('docker builder prune',120)))
            stack.enter_context(patch.object(worker.time,'sleep',side_effect=StopLoop))
            with self.assertRaises(StopLoop):
                worker.main()



class DurableCleanupTests(unittest.TestCase):
    def setUp(self):
        from store import connect
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=connect(Path(self.temp.name)/'state.sqlite');self.addCleanup(self.db.close)
    def test_failure_backoff_reopen_and_success_replay_without_duplicate_effect(self):
        import image_cleanup
        image_cleanup.enqueue(self.db,3,'build-cache')
        callback=MagicMock(side_effect=TimeoutError)
        self.assertTrue(image_cleanup.process_once(self.db,{'build-cache':callback},now=100))
        self.assertEqual(self.db.execute('SELECT state FROM image_cleanup').fetchone()[0],'pending')
        self.assertFalse(image_cleanup.process_once(self.db,{'build-cache':callback},now=100))
        due=self.db.execute('SELECT next_attempt FROM image_cleanup').fetchone()[0]
        callback.side_effect=None;callback.return_value=True
        self.assertTrue(image_cleanup.process_once(self.db,{'build-cache':callback},now=due+1))
        image_cleanup.enqueue(self.db,3,'build-cache')
        self.assertFalse(image_cleanup.process_once(self.db,{'build-cache':callback},now=due+1000))
        self.assertEqual(callback.call_count,2)
    def test_expired_lease_recovers_and_live_lease_is_not_stolen(self):
        import image_cleanup
        image_cleanup.enqueue(self.db,3,'job-images')
        self.db.execute("UPDATE image_cleanup SET state='running',lease='previous',expires=200")
        callback=MagicMock(return_value=True)
        self.assertFalse(image_cleanup.process_once(self.db,{'job-images':callback},now=199))
        self.assertTrue(image_cleanup.process_once(self.db,{'job-images':callback},now=201))
        callback.assert_called_once_with(self.db,3)
    def test_publication_defers_and_protection_is_rechecked_on_retry(self):
        import image_cleanup
        image='sha256:'+'a'*64
        image_cleanup.enqueue(self.db,3,'job-images')
        callbacks={'job-images':worker.prune_job_images}
        with patch('artifact_publication.pending',return_value=True),patch.object(worker,'run') as command:
            image_cleanup.process_once(self.db,callbacks)
            command.assert_not_called()
        self.db.execute('UPDATE image_cleanup SET next_attempt=0')
        with patch('artifact_publication.pending',return_value=False),patch('managed_services.protected_images',return_value={image}),patch('managed_delivery.protected_images',return_value=set()),patch.object(worker,'run',return_value=image),patch.object(worker.subprocess,'run') as removal:
            image_cleanup.process_once(self.db,callbacks)
            removal.assert_not_called()
        self.assertEqual(self.db.execute('SELECT state FROM image_cleanup').fetchone()[0],'succeeded')
    def test_removal_batch_is_bounded_and_never_forces_or_removes_volumes(self):
        images={'sha256:'+format(n,'064x') for n in range(21)}
        with patch.object(worker.subprocess,'run',return_value=subprocess.CompletedProcess([],0)) as command:
            self.assertFalse(worker.remove_cleanup_images(images))
        self.assertEqual(command.call_count,20)
        for args,kwargs in command.call_args_list:
            self.assertEqual(args[0][:3],[worker.DOCKER,'image','rm'])
            self.assertEqual(len(args[0]),4)
            self.assertLessEqual(kwargs['timeout'],10)

    def test_terminal_success_crash_gap_restores_all_cleanup_intents(self):
        import image_cleanup
        self.db.execute("INSERT INTO jobs(id,delivery,project,sha,status,updated,created) VALUES(9,'crash','app',?,'succeeded',1,1)",('a'*40,))
        image_cleanup.enqueue(self.db,9,'job-images')
        image_cleanup.enqueue(self.db,9,'build-cache')
        self.assertEqual(image_cleanup.reconcile_terminal_jobs(self.db),1)
        self.assertEqual({row['kind'] for row in self.db.execute('SELECT kind FROM image_cleanup')},{'job-images','build-cache','previous-images'})
        self.assertEqual(image_cleanup.reconcile_terminal_jobs(self.db),0)

    def test_recovery_journal_defers_cleanup_effects(self):
        import image_cleanup
        image_cleanup.enqueue(self.db,3,'build-cache')
        journal=Path(self.temp.name)/'recovery.json';journal.write_text('{}')
        with patch.object(worker.control,'JOURNAL',journal),patch.object(worker,'prune_build_cache') as effect:
            self.assertFalse(worker.process_cleanup(self.db))
            effect.assert_not_called()

    def test_legacy_snapshot_is_immutable_and_missing_images_are_replay_safe(self):
        import image_cleanup
        image='sha256:'+'a'*64
        image_cleanup.enqueue(self.db,3,'legacy-images',images=[image])
        with self.assertRaises(ValueError):image_cleanup.enqueue(self.db,3,'legacy-images',images=['sha256:'+'b'*64])
        self.db.execute("INSERT INTO jobs(id,delivery,project,sha,status,updated,created) VALUES(3,'legacy','app',?,'succeeded',1,1)",('a'*40,))
        with patch('managed_services.protected_images',return_value=set()),patch('managed_delivery.protected_images',return_value=set()),patch.object(worker,'run',return_value=''),patch.object(worker.subprocess,'run') as effect:
            self.assertTrue(worker.prune_legacy_images(self.db,3))
            effect.assert_not_called()

    def test_historical_missing_target_is_inapplicable_but_managed_evidence_is_not(self):
        with patch.object(worker,'run') as command:
            self.assertTrue(worker.prune_managed_previous_images(self.db,123))
            command.assert_not_called()
        self.db.execute('CREATE TABLE managed_deliveries(job_id INTEGER PRIMARY KEY)')
        self.db.execute('INSERT INTO managed_deliveries VALUES(123)')
        with self.assertRaises(ValueError),patch.object(worker,'run') as command:
            worker.prune_managed_previous_images(self.db,123)
        command.assert_not_called()

    def test_present_corrupt_target_is_not_treated_as_historical(self):
        import job_target
        job_target.initialize(self.db)
        self.db.execute("INSERT INTO job_targets VALUES(123,'invalid','{}',1)")
        with self.assertRaises(ValueError),patch.object(worker,'run') as command:
            worker.prune_managed_previous_images(self.db,123)
        command.assert_not_called()

if __name__=='__main__':unittest.main()
