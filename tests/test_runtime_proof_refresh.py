import json
import unittest
from unittest.mock import patch
import runtime_proof_refresh as refresh
import service_runtime_operations as operations
import test_service_runtime_operations as fixtures
import managed_services
import time


class RuntimeProofRefreshTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RuntimeOperationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db
    def complete(self):
        self.f.accept();item=operations.load(self.db,'runtime-operation-01')['item']
        binding=dict(self.f.f.binding,container_id='f'*64,definition_digest=managed_services.digest(item['next_definition']),image_id=item['artifact']['image'],observed=time.time())
        with patch('managed_rollout.rollout',return_value=binding):
            operations.apply(self.db,'runtime-operation-01',docker='docker',compose='compose',authorize=lambda:None)
    def test_independent_terminal_runtime_authority_and_binding_drift(self):
        self.complete();plan=refresh.authority(self.db,'runtime-operation-01',{})
        self.assertEqual(plan['operation_id'],'runtime-operation-01')
        self.assertNotIn('job_id',plan)
        self.db.execute('UPDATE service_bindings SET document=? WHERE service_id=?',(json.dumps(self.f.f.binding),self.f.sid))
        with self.assertRaises(ValueError):refresh.authority(self.db,'runtime-operation-01',{})
    def test_uncommitted_runtime_cannot_authorize_refresh(self):
        self.f.accept()
        with self.assertRaises(ValueError):refresh.authority(self.db,'runtime-operation-01',{})
        with self.assertRaises(ValueError):refresh.enqueue(self.db,'runtime-operation-01')
    def test_missing_old_schema_is_no_work(self):
        self.assertFalse(refresh.process_once(self.db,root='/tmp',docker='docker'))
