import copy
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
import workload_operations as w

class RetryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=self.tmp.name+'/state.db';self.db=self.connect();w.initialize(self.db)
  self.request=dict(request_id='request-1234567890',project='example',environment='development',service_id=str(uuid.uuid4()),container_id='a'*64,definition_digest='b'*64,adapter_revision=1,connection_id='database',queue='default',failed_job_id=str(uuid.uuid4()),failure_revision='c'*64)
  self.revoked=False;self.calls=[]
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def connect(self):
  db=sqlite3.connect(self.path,isolation_level=None,timeout=5);db.row_factory=sqlite3.Row;return db
 def auth(self,actor,request,action):
  if self.revoked or actor!='owner' or request['environment']!='development':raise ValueError('private denied reason')
 def accept(self):return w.accept(self.db,'owner',self.request,authorize=self.auth)
 def transport(self,r,guard):
  guard();self.calls.append(r['operation']);return dict(protocol=1,status='ok',scope=r['scope'],observed_at='2026-10-11T00:00:00+00:00',result=dict(request_id=r['request_id'],status='requeued',failed_job_id=self.request['failed_job_id'],queue='default',queue_job_id='42',committed_at='2026-10-11T00:00:00+00:00'))
 def test_replay_conflict_and_concurrent_acceptance(self):
  def run(_):
   db=self.connect()
   try:return w.accept(db,'owner',self.request,authorize=self.auth)['operation_id']
   finally:db.close()
  with ThreadPoolExecutor(max_workers=2) as pool:ids=list(pool.map(run,range(2)))
  self.assertEqual(ids[0],ids[1]);changed=dict(self.request,queue='other')
  with self.assertRaises(w.WorkloadError):w.accept(self.db,'owner',changed,authorize=self.auth)
 def test_revoked_before_dispatch_and_foreign_owner(self):
  op=self.accept()['operation_id'];self.revoked=True
  with self.assertRaises(w.WorkloadError):w.dispatch(self.db,'owner',op,authorize=self.auth,transport=self.transport)
  self.assertEqual(self.calls,[]);self.revoked=False
  with self.assertRaises(w.WorkloadError):w.get(self.db,'stranger',op,authorize=self.auth)
  with self.assertRaises(w.WorkloadError):w.accept(self.db,'owner',dict(self.request,environment='production'),authorize=self.auth)
 def test_lost_reply_receipt_only(self):
  op=self.accept()['operation_id']
  def lost(r,g):self.transport(r,g);raise TimeoutError('SECRET')
  result=w.dispatch(self.db,'owner',op,authorize=self.auth,transport=lost)
  self.assertEqual(result['status'],'recovery-required');self.assertNotIn('SECRET',str(result))
  result=w.dispatch(self.db,'owner',op,authorize=self.auth,transport=self.transport)
  self.assertEqual(result['status'],'requeued');self.assertEqual(self.calls,['queue.retry','queue.receipt'])
  w.dispatch(self.db,'owner',op,authorize=self.auth,transport=self.transport);self.assertEqual(len(self.calls),2)
 def test_absent_receipt_never_redispatches(self):
  op=self.accept()['operation_id']
  def lost(r,g):raise TimeoutError()
  w.dispatch(self.db,'owner',op,authorize=self.auth,transport=lost)
  def absent(r,g):
   self.assertEqual(r['operation'],'queue.receipt');return dict(protocol=1,status='ok',scope=r['scope'],observed_at='2026-10-11T00:00:00Z',result=dict(request_id=r['request_id'],status='absent'))
  self.assertEqual(w.reconcile(self.db,'owner',op,authorize=self.auth,transport=absent)['status'],'recovery-required')
 def test_original_target_projection_and_package_field_bounds(self):
  op=self.accept();self.assertEqual(op['target'],{k:v for k,v in self.request.items() if k!='request_id'})
  for change in ({'queue':'with.dot'},{'queue':'q'*81},{'connection_id':'not-valid'},{'connection_id':'1name'}):
   with self.assertRaises(w.WorkloadError):w.accept(self.db,'owner',dict(self.request,**change),authorize=self.auth)
 def test_replay_does_not_require_deleted_failure(self):
  original=self.accept();actions=[]
  def auth(actor,request,action):
   actions.append(action)
   if action=='accept':raise ValueError('failure deleted')
  self.assertEqual(w.accept(self.db,'owner',self.request,authorize=auth)['operation_id'],original['operation_id'])
  self.assertEqual(actions,['read','replay'])
 def test_reauthorization_after_effect_cannot_publish_success(self):
  op=self.accept()['operation_id']
  def revoke(r,g):
   response=self.transport(r,g);self.revoked=True;return response
  with self.assertRaises(w.WorkloadError):w.dispatch(self.db,'owner',op,authorize=self.auth,transport=revoke)
  self.assertEqual(self.db.execute('SELECT state FROM workload_retry_operations').fetchone()[0],'dispatching')
  self.revoked=False
  self.assertEqual(w.reconcile(self.db,'owner',op,authorize=self.auth,transport=self.transport)['status'],'requeued')
 def test_crash_after_intent_reconciles_only(self):
  op=self.accept()['operation_id'];self.db.execute("UPDATE workload_retry_operations SET state='dispatching' WHERE id=?",(op,))
  self.assertEqual(w.dispatch(self.db,'owner',op,authorize=self.auth,transport=self.transport)['status'],'requeued')
  self.assertEqual(self.calls,['queue.receipt'])
 def test_scope_and_extra_receipt_data_rejected(self):
  op=self.accept()['operation_id']
  def forged(r,g):
   response=self.transport(r,g);response['result']['payload']='SECRET';return response
  self.assertEqual(w.dispatch(self.db,'owner',op,authorize=self.auth,transport=forged)['status'],'recovery-required')
  self.assertNotIn('SECRET',str(self.db.execute('SELECT * FROM workload_retry_operations').fetchall()))
if __name__=='__main__':unittest.main()
