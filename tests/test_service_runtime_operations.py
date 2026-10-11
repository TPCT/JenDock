import json
import tempfile
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet
import managed_services
import service_runtime
import service_runtime_operations as operations
import test_managed_delivery as fixtures


class RuntimeOperationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ManagedDeliveryTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.db=self.f.db;self.sid=self.f.sid
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.key=Path(self.tmp.name)/'key';self.key.write_bytes(Fernet.generate_key());self.key.chmod(0o600)
        # Fixture delivery source artifact mock is not relevant to a same-image change.
        self.f.artifact_patch.stop()
    def accept(self):
        return operations.accept(self.db,'runtime-operation-01',self.sid,{'TOKEN':'private-secret'},expected_container_id=self.f.binding['container_id'],key_path=self.key,authorize=lambda:None)
    def test_capture_apply_atomic_and_no_secret_journal(self):
        saved=self.accept();self.assertEqual(saved['status'],'accepted')
        item=operations.load(self.db,'runtime-operation-01')['item']
        self.assertEqual(item['definition']['artifact'],item['next_definition']['artifact'])
        self.assertNotIn('private-secret',json.dumps(item))
        new=dict(self.f.binding,container_id='f'*64,definition_digest=managed_services.digest(item['next_definition']),image_id=item['artifact']['image'],observed=time.time())
        with patch('managed_rollout.rollout',return_value=new):
            result=operations.apply(self.db,'runtime-operation-01',docker='docker',compose='compose',authorize=lambda:None)
        self.assertEqual(result['status'],'succeeded')
        current=managed_services.get(self.db,self.sid)
        self.assertEqual(current['binding'],new)
        self.assertEqual(service_runtime.status(self.db,'app','development',self.sid)['applied'],current['definition']['runtime']['environment'])
        with patch('managed_rollout.rollout') as effect:
            self.assertEqual(operations.apply(self.db,'runtime-operation-01',docker='docker',compose='compose',authorize=lambda:None)['status'],'succeeded')
            effect.assert_not_called()
    def test_pending_fence_and_readonly_unknown_recovery(self):
        self.accept()
        with self.assertRaises(ValueError):self.f.selection=__import__('managed_delivery').capture(self.db,'app','development')
        with patch('managed_rollout.rollout',side_effect=ValueError('uncertain private message')):
            self.assertEqual(operations.apply(self.db,'runtime-operation-01',docker='docker',compose='compose',authorize=lambda:None)['status'],'recovery-required')
        with patch('managed_rollout.reconcile',return_value={'outcome':'unknown','reason':'secret','binding':None}),patch('managed_rollout.rollout') as effect:
            self.assertEqual(operations.reconcile(self.db,'runtime-operation-01',docker='docker',authorize=lambda:None)['status'],'recovery-required')
            effect.assert_not_called()
        self.assertNotIn('secret',json.dumps(operations.public(self.db,'runtime-operation-01')))
    def test_stale_container_rejected_before_desired_saved(self):
        with self.assertRaises(ValueError):operations.accept(self.db,'runtime-operation-01',self.sid,{'X':'v'},expected_container_id='0'*64,key_path=self.key,authorize=lambda:None)
        self.assertIsNone(service_runtime.status(self.db,'app','development',self.sid)['desired'])
    def test_acceptance_insert_failure_rolls_back_desired_and_ciphertext(self):
        operations.initialize(self.db)
        self.db.execute("CREATE TRIGGER refuse_runtime_operation BEFORE INSERT ON service_runtime_operations BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(Exception):self.accept()
        self.assertEqual(service_runtime.status(self.db,'app','development',self.sid),{'desired':None,'applied':None})
        self.assertEqual(self.db.execute('SELECT count(*) FROM service_runtime_revisions').fetchone()[0],0)
    def test_recovered_result_updates_control_receipt_without_reapplying(self):
        self.accept();item=operations.load(self.db,'runtime-operation-01')['item']
        self.db.execute("INSERT INTO control_runs(request_id,created,updated,actor,project,container,operation,status,detail) VALUES(?,0,0,'admin','app','container','environment-save','running','pending')",('runtime-operation-01',))
        binding=dict(self.f.binding,container_id='f'*64,definition_digest=managed_services.digest(item['next_definition']),image_id=item['artifact']['image'],observed=time.time())
        with patch('managed_rollout.rollout',side_effect=ValueError('lost reply')):
            operations.apply(self.db,'runtime-operation-01',docker='docker',compose='compose',authorize=lambda:None)
        with patch('managed_rollout.reconcile',return_value={'outcome':'verified-new','binding':binding}),patch('managed_rollout.rollout') as effect:
            result=operations.reconcile(self.db,'runtime-operation-01',docker='docker',authorize=lambda:None)
            effect.assert_not_called()
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(self.db.execute('SELECT status FROM control_runs WHERE request_id=?',('runtime-operation-01',)).fetchone()[0],'succeeded')
    def test_worker_resumes_only_proven_unstarted_acceptance(self):
        self.accept()
        with patch.object(operations,'apply') as apply,patch.object(operations,'reconcile') as observe:
            operations.recover_pending(self.db,docker='docker',compose='compose',authorize_project=lambda p:None)
            apply.assert_called_once();observe.assert_not_called()
        self.db.execute("UPDATE service_runtime_operations SET state='applying'")
        with patch.object(operations,'apply') as apply,patch.object(operations,'reconcile') as observe:
            operations.recover_pending(self.db,docker='docker',compose='compose',authorize_project=lambda p:None)
            apply.assert_not_called();observe.assert_called_once()
    def test_encrypted_original_request_exact_replay_and_conflicts(self):
        body={'action':'environment-save','project':'app','container':self.f.binding['container_name'],'container_id':self.f.binding['container_id'],
              'revision':'revision-before','request_id':'runtime-operation-01','changes':{'TOKEN':'private-secret'}}
        operations.accept(self.db,'runtime-operation-01',self.sid,body['changes'],expected_container_id=self.f.binding['container_id'],key_path=self.key,authorize=lambda:None,request=body)
        self.assertEqual(operations.replay_request(self.db,body,key_path=self.key)['status'],'accepted')
        for changed in ({'changes':{'TOKEN':'another-secret'}},{'container_id':'e'*64},{'revision':'new-revision'},{'database_attachment':None},{'actor':'different'}):
            with self.assertRaises(ValueError):operations.replay_request(self.db,body|changed,key_path=self.key)
        raw=self.db.execute('SELECT ciphertext FROM service_runtime_requests').fetchone()[0]
        self.assertNotIn(b'private-secret',raw)
        with self.assertRaises(Exception):self.db.execute('DELETE FROM service_runtime_requests')
