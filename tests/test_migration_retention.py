import tempfile,time,unittest
from pathlib import Path
from store import connect,enqueue
import migration_jobs
import job_target

class MigrationRetentionTests(unittest.TestCase):
    def test_old_failed_job_with_uncertain_database_intent_keeps_parent_and_sources(self):
        with tempfile.TemporaryDirectory() as root:
            db=connect(Path(root)/'state');self.addCleanup(db.close)
            migration_jobs.initialize(db);job_target.initialize(db)
            old=time.time()-31*86400
            for i,state in enumerate(('applying','recovery-required','planned','verified'),1):
                db.execute('INSERT INTO jobs(id,delivery,project,sha,created,updated,status) VALUES(?,?,?,?,?,?,?)',(i,'old'+str(i),'app','a'*40,old,old,'failed'))
                db.execute('INSERT INTO job_targets VALUES(?,?,?,?)',(i,'fingerprint','{}',old))
                db.execute('INSERT INTO job_migration_targets VALUES(?,?,?)',(i,'{}','digest'))
                db.execute('INSERT INTO job_migrations(job_id,state,sources,updated) VALUES(?,?,?,?)',(i,state,'{}',old))
            db.execute('INSERT INTO jobs(id,delivery,project,sha,created,updated,status) VALUES(?,?,?,?,?,?,?)',(5,'missing-journal','app','a'*40,old,old,'failed'))
            db.execute('INSERT INTO job_targets VALUES(?,?,?,?)',(5,'fingerprint','{}',old))
            db.execute('INSERT INTO job_migration_targets VALUES(?,?,?)',(5,'{}','digest'))
            enqueue(db,'fresh','app','b'*40,True)
            self.assertEqual([r[0] for r in db.execute('SELECT id FROM jobs WHERE id<=5 ORDER BY id')],[1,2,3,5])
            self.assertEqual([r[0] for r in db.execute('SELECT job_id FROM job_targets ORDER BY job_id')],[1,2,3,5])
            self.assertEqual([r[0] for r in db.execute('SELECT job_id FROM job_migration_targets ORDER BY job_id')],[1,2,3,5])
            self.assertIsNone(db.execute('SELECT job_id FROM job_migration_targets WHERE job_id=4').fetchone())
            self.assertIsNone(db.execute('SELECT job_id FROM job_migrations WHERE job_id=4').fetchone())
            self.assertEqual(db.execute('SELECT sources FROM job_migrations WHERE job_id=1').fetchone()[0],'{}')

    def test_unknown_orphan_target_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as root:
            db=connect(Path(root)/'state');self.addCleanup(db.close)
            migration_jobs.initialize(db)
            db.execute('INSERT INTO job_migration_targets VALUES(?,?,?)',(99,'{}','digest'))
            with self.assertRaises(Exception):db.execute('DELETE FROM job_migration_targets WHERE job_id=99')
