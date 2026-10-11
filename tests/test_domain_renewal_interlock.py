import unittest
import domain_execution as execution
import domain_external_flow as external
import test_domain_observation_authority as fixtures

class RenewalInterlockTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ObservationAuthorityTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.db=self.f.db;self.operation=self.f.operation['id']
        self.db.execute('CREATE TABLE domain_renewals(operation_id TEXT PRIMARY KEY,state TEXT,phase TEXT)')
        self.db.execute("INSERT INTO domain_renewals VALUES(?,'running','stage')",(self.operation,))
    def test_external_scheduler_preserves_pending_transition(self):
        for phase in ('stage','activate'):
            self.db.execute('UPDATE domain_renewals SET phase=?',(phase,))
            self.db.execute("UPDATE domain_executions SET state='ready',phase='external',journal='{}'")
            self.assertFalse(external.schedule(self.db))
            self.assertEqual(execution._row(self.db,self.operation)['state'],'ready')
    def test_queued_external_execution_cannot_claim_during_transition(self):
        self.db.execute("UPDATE domain_executions SET state='queued',phase='external',worker=NULL,lease_expires=NULL")
        self.assertIsNone(execution.claim_next(self.db,'external-worker'))
        self.db.execute("UPDATE domain_renewals SET state='complete'")
        self.assertIsNotNone(execution.claim_next(self.db,'external-worker'))
    def test_issuance_keeps_existing_external_checks_available(self):
        self.db.execute("UPDATE domain_renewals SET phase='issue'")
        self.db.execute("UPDATE domain_executions SET state='ready',phase='external',journal='{}',worker=NULL,lease_expires=NULL")
        self.assertTrue(external.schedule(self.db))
        self.assertIsNotNone(execution.claim_next(self.db,'external-worker'))
