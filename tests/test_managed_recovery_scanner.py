"""Recovery worker orchestration: expired selection, observations, and fail-closed commit."""
import contextlib
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import managed_delivery
import managed_rollout
import worker


class RecoveryScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE managed_deliveries(job_id INTEGER,state TEXT,expires REAL)')
        self.db.execute('CREATE TABLE jobs(id INTEGER,status TEXT,detail TEXT)')
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(worker, 'STATE', Path(self.tmp.name)))
        self.stack.enter_context(patch.object(worker.control, 'JOURNAL', Path(self.tmp.name)/'journal.json'))
        self.stack.enter_context(patch.object(managed_delivery, 'initialize'))
        self.claim = self.stack.enter_context(patch.object(managed_delivery, 'claim_recovery', return_value={'services': [{'service_id':'a'}, {'service_id':'b'}]}))
        self.observe = self.stack.enter_context(patch.object(managed_rollout, 'reconcile', return_value={'outcome':'verified-new','binding':{'id':'verified'}}))
        self.finish = self.stack.enter_context(patch.object(managed_delivery, 'finish_recovery', return_value={'status':'succeeded'}))
        self.status = self.stack.enter_context(patch.object(worker, 'set_status'))
        self.prune = self.stack.enter_context(patch.object(worker, 'prune_managed_previous_images'))
        self.rollout = self.stack.enter_context(patch.object(managed_rollout, 'rollout', side_effect=AssertionError('Recovery must not replay mutation')))

    def add(self, identifier, state, expires=None):
        self.db.execute('INSERT INTO managed_deliveries VALUES(?,?,?)',(identifier,state,expires))
        self.db.execute('INSERT INTO jobs VALUES(?,?,?)',(identifier,'interrupted','Stopped'))

    def test_only_unresolved_and_expired_leases_are_scanned(self):
        now = time.time()
        for i, state, expiry in [(1,'recovery-required',None),(2,'running',now-10),(3,'reconciling',now-10),(4,'running',now+3600),(5,'reconciling',now+3600),(6,'succeeded',None),(7,'failed',None),(8,'queued',None)]:
            self.add(i,state,expiry)
        self.assertTrue(worker.recover_managed_deliveries(self.db))
        self.assertEqual([call.args[1] for call in self.claim.call_args_list], [1,2,3])
        self.assertEqual(self.observe.call_count, 6)
        self.rollout.assert_not_called()

    def test_observation_failure_preserves_unknown_and_cannot_publish_success(self):
        self.add(1,'recovery-required')
        self.observe.side_effect = [ValueError('identity drift'), {'outcome':'verified-old','binding':{'id':'old'}}]
        self.finish.return_value = {'status':'recovery-required'}
        worker.recover_managed_deliveries(self.db)
        outcomes = self.finish.call_args.args[3]
        self.assertEqual(outcomes['a'], {'outcome':'unknown'})
        self.assertEqual(outcomes['b']['outcome'], 'old')
        self.assertEqual(self.status.call_args.args[2], 'failed')
        self.prune.assert_not_called()
        self.rollout.assert_not_called()

    def test_failed_metadata_commit_never_marks_run_or_prunes(self):
        self.add(1,'recovery-required')
        self.finish.side_effect = ValueError('identity changed')
        self.assertFalse(worker.recover_managed_deliveries(self.db))
        self.status.assert_not_called()
        self.prune.assert_not_called()

    def test_unavailable_lease_never_inspects_or_commits(self):
        self.add(1,'recovery-required')
        self.claim.return_value = None
        self.assertFalse(worker.recover_managed_deliveries(self.db))
        self.observe.assert_not_called()
        self.finish.assert_not_called()

    def test_control_journal_blocks_recovery(self):
        self.add(1,'recovery-required')
        worker.control.JOURNAL.touch()
        self.assertFalse(worker.recover_managed_deliveries(self.db))
        self.claim.assert_not_called()
