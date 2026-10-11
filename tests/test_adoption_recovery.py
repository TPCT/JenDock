import json
import threading
import unittest
from unittest.mock import patch
import adoption
import control
import test_adoption_environments as fixtures


class AdoptionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.AdoptionEnvironmentTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db=self.fixture.db
        self.proof=self.fixture.accept('development',services=['development-db'])

    def test_recovery_is_read_only_and_bound_to_actor_selection_and_integrity(self):
        before=self.db.total_changes
        result=adoption.recover(self.db,'app','development',self.proof['fingerprint'],'owner',services=['development-db'])
        self.assertEqual(result['id'],'app')
        self.assertEqual(self.db.total_changes,before)
        for kwargs in ({'actor':'other'}, {'fingerprint':'b'*64}, {'services':['production-db']}, {'services':['development-db','development-db']}, {'recipe_revision':True}, {'actor':''}):
            args={'actor':'owner','fingerprint':self.proof['fingerprint'],'services':['development-db']}|kwargs
            with self.assertRaises(ValueError):adoption.recover(self.db,'app','development',**args)
        corrupted=dict(self.proof,state='stopped')
        self.db.execute('UPDATE workload_adoptions SET evidence=?',(json.dumps(corrupted),))
        with self.assertRaisesRegex(ValueError,'evidence'):adoption.recover(self.db,'app','development',self.proof['fingerprint'],'owner',services=['development-db'])

    def test_control_recovers_accepted_receipt_during_docker_outage_and_pending_journal(self):
        journal=self.fixture.path.parent/'recovery.json';journal.write_text('{}')
        body={'action':'adoption-save','project':'app','environment':'development','actor':'owner','fingerprint':self.proof['fingerprint'],'services':['development-db']}
        with patch.object(control,'DATABASE',str(self.fixture.path)),patch.object(control,'LOCK',threading.Lock()),patch.object(control,'JOURNAL',journal),patch.object(control,'effective_projects') as projection,patch.object(adoption,'collect',side_effect=RuntimeError('Docker unavailable')) as collect,patch.object(control,'audit') as audit:
            result=control.execute(body)
            self.assertEqual(result['receipt']['fingerprint'],self.proof['fingerprint'])
            collect.assert_not_called();audit.assert_not_called();projection.assert_not_called()
            self.assertTrue(journal.exists())
            with self.assertRaises(control.ControlError):control.execute(body|{'environment':'production'})
            collect.assert_not_called()
        self.assertEqual(self.db.execute('SELECT count(*) FROM workload_adoptions').fetchone()[0],1)
