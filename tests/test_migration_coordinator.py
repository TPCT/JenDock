import unittest
from unittest.mock import patch
import migration_jobs as jobs
import migration_coordinator as coordinator
import test_migration_bindings as fixtures

class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.MigrationBindingsTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.save();self.db=self.f.db
        jobs.capture(self.db,1,'app','development','sha256:'+'a'*64)
        jobs.save_sources(self.db,1,{'alembic/versions/a.py':'pass'})
        self.candidate={'container_id':'b'*64,'fingerprint':'c'*64}
        self.proof={'digest':'d'*64,'plan':{}}
        self.calls=[]
        parent=self
        class Helper:
            image='sha256:'+'a'*64
            def invoke(self,container,request,*,revalidate,authorize):
                revalidate(container);authorize();parent.calls.append(request['operation'])
                if request['operation']=='inspect_plan':return {'status':'planned','proof':parent.proof,'observed_at':1}
                return {'status':'committed','proof_digest':'d'*64,'observed_at':2}
        self.helper=Helper()
    def run_job(self):
        with patch.object(coordinator,'resolve_ownership',return_value=self.candidate),patch.object(coordinator,'check_registered'):
            return coordinator.process(self.db,1,helper=self.helper,resolve_candidate=lambda *a,**k:None,key_path=self.f.f.key,authorize=lambda:None)
    def test_real_saved_authority_and_verified_helper_outcome(self):
        self.assertTrue(self.run_job());self.assertEqual(self.calls,['inspect_plan','apply','reconcile'])
        self.assertEqual(jobs.load(self.db,1)['state'],'verified')
        self.assertTrue(self.run_job());self.assertEqual(self.calls[-1],'reconcile')
    def test_lost_apply_reply_reconciles_without_reapply(self):
        invoke=self.helper.invoke
        def uncertain(container,request,**kwargs):
            if request['operation']=='apply':raise ValueError('lost reply')
            return invoke(container,request,**kwargs)
        self.helper.invoke=uncertain
        with self.assertRaises(ValueError):self.run_job()
        self.assertEqual(jobs.load(self.db,1)['state'],'recovery-required')
        self.helper.invoke=invoke
        self.assertTrue(self.run_job());self.assertEqual(self.calls,['inspect_plan','reconcile'])
    def test_false_proof_never_unlocks_cutover(self):
        invoke=self.helper.invoke
        def wrong(container,request,**kwargs):
            result=invoke(container,request,**kwargs)
            if request['operation']=='reconcile':result['proof_digest']='e'*64
            return result
        self.helper.invoke=wrong
        with self.assertRaises(ValueError):self.run_job()
        self.assertNotEqual(jobs.load(self.db,1)['state'],'verified')

    def test_worker_recovery_unstarted_or_proven_no_commit_never_applies(self):
        import worker
        jobs.record_plan(self.db,1,{'candidate':self.candidate,'helper_proof':self.proof})
        for state in ('captured','source-ready','planned','failed'):
            self.db.execute('UPDATE job_migrations SET state=? WHERE job_id=1',(state,))
            with self.subTest(state=state),patch.object(coordinator,'process',side_effect=AssertionError('recovery must not apply')):
                self.assertTrue(worker.process_managed_migration(self.db,1,lambda:None,recovery=True))
