import json
import sqlite3
import unittest
import migration_status
import database_bindings

class MigrationStatusTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row;self.addCleanup(self.db.close)
    def test_absent_journal_is_not_success(self):
        self.assertIsNone(migration_status.public_status(self.db,7))
    def record(self,state='planned',proof=None):
        self.db.execute('CREATE TABLE job_migrations(job_id INTEGER PRIMARY KEY,state TEXT,proof TEXT,proof_digest TEXT,updated REAL)')
        self.db.execute('INSERT INTO job_migrations VALUES(?,?,?,?,?)',(7,state,json.dumps(proof) if proof else None,database_bindings._digest(proof) if proof else None,10))
    def test_only_pending_paths_are_public(self):
        record=['revision',[],[],[], 'database/migrations/001.php','privatehash',[]]
        self.record(proof={'candidate':{'password':'private'},'helper_proof':{'plan':{'migrations':[record]},'identity':{'user':'private'}}})
        result=migration_status.public_status(self.db,7)
        self.assertEqual(result,{'state':'planned','files':['database/migrations/001.php'],'updated':10})
        self.assertNotIn('private',json.dumps(result));self.assertIsNone(migration_status.public_status(self.db,8))
    def test_modified_valid_proof_cannot_claim_verified(self):
        self.record('verified',{'helper_proof':{'plan':{'migrations':[]}}})
        self.assertEqual(migration_status.public_status(self.db,7)['state'],'verified')
        self.db.execute("UPDATE job_migrations SET proof_digest=?",('0'*64,))
        self.assertEqual(migration_status.public_status(self.db,7)['state'],'unavailable')
    def test_unknown_or_corrupt_proof_is_unavailable_not_verified(self):
        self.record('verified',{'helper_proof':{'plan':{'migrations':[['missing']]}}})
        self.assertEqual(migration_status.public_status(self.db,7)['state'],'unavailable')
        self.db.execute("UPDATE job_migrations SET state='unexpected',proof=NULL")
        self.assertEqual(migration_status.public_status(self.db,7)['state'],'unavailable')
