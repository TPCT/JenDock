import copy
from unittest.mock import patch
import artifact_publication as publication
import managed_services as services
import build_profiles
from test_artifact_publication import PublicationTests
from test_workload_role_profiles import role_values

class RolePublicationTests(PublicationTests):
 def prepare(self):
  publication.record(self.db,self.job,self.recipe,self.images,status='validated')
  self.profile=build_profiles.role_profile('laravel-database-queue-v1',role_values('queue'),'app','65534:65534')
 def test_missing_or_mismatched_verifier_leaves_entire_batch_pending(self):
  self.prepare()
  with patch.object(services,'_profile',return_value=self.profile):
   self.assertFalse(publication.process(self.db,lambda ref:'sha256:'+'a'*64,self.job))
   self.assertFalse(publication.process(self.db,lambda ref:'sha256:'+'a'*64,self.job,verify_role=lambda a:dict(services._role_evidence(a),protocol_version=True)))
  self.assertEqual(self.db.execute('SELECT count(*) FROM service_artifacts').fetchone()[0],0)
  self.assertEqual(self.db.execute('SELECT count(*) FROM service_role_verifications').fetchone()[0],0)
 def test_verifier_outside_transaction_and_all_evidence_atomic(self):
  self.prepare();calls=[]
  def verify(document):
   self.assertFalse(self.db.in_transaction);self.assertEqual(self.db.execute('SELECT count(*) FROM service_artifacts').fetchone()[0],0)
   calls.append(document['template_id']);return services._role_evidence(document)
  self.db.execute("CREATE TRIGGER reject_evidence BEFORE INSERT ON service_role_verifications WHEN (SELECT count(*) FROM service_role_verifications)>0 BEGIN SELECT RAISE(ABORT,'SECRET');END")
  with patch.object(services,'_profile',return_value=self.profile):
   self.assertFalse(publication.process(self.db,lambda ref:'sha256:'+'a'*64,self.job,verify_role=verify))
   self.assertEqual(self.db.execute('SELECT count(*) FROM service_artifacts').fetchone()[0],0)
   self.assertEqual(self.db.execute('SELECT count(*) FROM service_role_verifications').fetchone()[0],0)
   self.db.execute('DROP TRIGGER reject_evidence')
   from store import connect
   from pathlib import Path
   self.db.close();self.db=connect(Path(self.tmp.name)/'db')
   self.assertTrue(publication.process(self.db,lambda ref:'sha256:'+'a'*64,self.job,verify_role=verify))
  self.assertEqual(self.db.execute('SELECT count(*) FROM service_role_verifications').fetchone()[0],2)
  publication.process(self.db,lambda ref:self.fail('reinspect'),self.job,verify_role=lambda a:self.fail('reverify'))
  self.assertEqual(calls,['first','second','first','second'])

 def test_source_change_during_verification_blocks_publication(self):
  self.prepare()
  def verify(document):
   self.db.execute("UPDATE jobs SET status='failed' WHERE id=?",(self.job,))
   return services._role_evidence(document)
  with patch.object(services,'_profile',return_value=self.profile):
   self.assertFalse(publication.process(self.db,lambda ref:'sha256:'+'a'*64,self.job,verify_role=verify))
  self.assertEqual(self.db.execute('SELECT count(*) FROM service_artifacts').fetchone()[0],0)
  self.assertEqual(self.db.execute('SELECT count(*) FROM service_role_verifications').fetchone()[0],0)
 def test_second_verification_exception_is_sanitized_and_no_partial_publish(self):
  self.prepare();calls=[]
  def verify(document):
   calls.append(document['template_id'])
   if len(calls)==2:raise RuntimeError('SECRET')
   return services._role_evidence(document)
  with patch.object(services,'_profile',return_value=self.profile):
   self.assertFalse(publication.process(self.db,lambda ref:'sha256:'+'a'*64,self.job,verify_role=verify))
  self.assertEqual(self.db.execute('SELECT count(*) FROM service_artifacts').fetchone()[0],0)
  self.assertNotIn('SECRET',self.db.execute('SELECT detail FROM artifact_publications').fetchone()[0])
