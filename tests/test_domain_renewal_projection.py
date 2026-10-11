import unittest
import domain_execution as execution
import test_domain_observation_authority as fixtures

class RenewalProjectionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ObservationAuthorityTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.db=self.f.db;self.operation=self.f.operation['id']
    def test_absent_renewal_is_not_fabricated(self):
        self.assertIsNone(execution._renewal_status(self.db,self.operation))
    def test_projection_excludes_journals_and_sanitizes_failure(self):
        self.db.execute('CREATE TABLE domain_renewals(operation_id TEXT, state TEXT, phase TEXT, next_at REAL, failures INTEGER,error_code TEXT,updated REAL,document TEXT)')
        self.db.execute('INSERT INTO domain_renewals VALUES(?,?,?,?,?,?,?,?)',(self.operation,'queued','issue',123,1,'private-provider-error',100,'private-journal'))
        result=execution._renewal_status(self.db,self.operation)
        self.assertEqual(result,{'status':'retrying','phase':'issue','next_check_at':123,'updated_at':100,'error_code':'renewal_unverified'})
        self.assertNotIn('private',str(result))
