import copy
import json
import sqlite3
import unittest
from unittest.mock import patch
import managed_lifecycle as lifecycle
import managed_services
import service_runtime_operations
import test_managed_delivery as fixtures


class ManagedLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ManagedDeliveryTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.db=self.f.db;self.sid=self.f.sid
        self.actual={'Id':self.f.binding['container_id'],'State':{'Running':True,'StartedAt':'2026-10-11T01:00:00Z','Health':{'Status':'healthy'}}}
        self.observe=patch.object(lifecycle,'observe',side_effect=lambda *a,**k:copy.deepcopy(self.actual));self.observe.start();self.addCleanup(self.observe.stop)
    def accept(self,action='stop'):
        return lifecycle.accept(self.db,'lifecycle-operation-001',self.sid,action,expected_container_id=self.f.binding['container_id'],docker='docker',authorize=lambda:None)
    def test_capture_cannot_be_replaced_deleted_or_rewritten(self):
        self.accept()
        before=dict(self.db.execute('SELECT * FROM managed_lifecycle_operations').fetchone())
        replacement=dict(before,document='{}')
        for statement,args in [
            ('INSERT OR REPLACE INTO managed_lifecycle_operations VALUES(?,?,?,?,?,?,?,?)',tuple(replacement.values())),
            ('DELETE FROM managed_lifecycle_operations WHERE id=?',(before['id'],)),
            ('UPDATE managed_lifecycle_operations SET document=? WHERE id=?',('{}',before['id'])),
        ]:
            with self.subTest(statement=statement),self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(statement,args)
            self.assertEqual(dict(self.db.execute('SELECT * FROM managed_lifecycle_operations').fetchone()),before)
        self.db.execute("UPDATE managed_lifecycle_operations SET state='applying',updated=updated+1 WHERE id=?",(before['id'],))
        after=dict(self.db.execute('SELECT * FROM managed_lifecycle_operations').fetchone())
        self.assertEqual(after['document'],before['document'])
        self.assertEqual(after['state'],'applying')
        self.assertGreater(after['updated'],before['updated'])

    def test_stop_updates_current_desire_binding_and_only_selected_environment(self):
        self.accept();old=managed_services.get(self.db,self.sid)['definition']
        production=self.db.execute("SELECT * FROM registry_environments WHERE project='app' AND name='production'").fetchone()
        def stop(*a,**k):self.actual['State']['Running']=False
        with patch('service_provisioning.command',side_effect=stop):result=lifecycle.apply(self.db,'lifecycle-operation-001',docker='docker',authorize=lambda:None)
        self.assertEqual(result['status'],'succeeded')
        current=managed_services.get(self.db,self.sid)
        self.assertEqual(current['state'],'stopped');self.assertEqual(current['binding']['state'],'stopped');self.assertEqual(current['desired_state'],'stopped')
        self.assertEqual(current['definition'],old);self.assertEqual(current['binding']['container_id'],self.actual['Id'])
        self.assertEqual(dict(self.db.execute("SELECT * FROM registry_environments WHERE project='app' AND name='production'").fetchone()),dict(production))
        selected=__import__('managed_delivery').capture(self.db,'app','development')
        self.assertEqual(selected['services'][0]['state'],'skipped-stopped')
    def test_restart_lost_response_observes_changed_start_without_replay(self):
        self.accept('restart')
        def restart(*a,**k):
            self.actual['State']['StartedAt']='2026-10-11T02:00:00Z'
            raise ValueError('lost response')
        with patch('service_provisioning.command',side_effect=restart) as command:
            self.assertEqual(lifecycle.apply(self.db,'lifecycle-operation-001',docker='docker',authorize=lambda:None)['status'],'recovery-required')
            self.assertEqual(lifecycle.reconcile(self.db,'lifecycle-operation-001',docker='docker',authorize=lambda:None)['status'],'succeeded')
            self.assertEqual(command.call_count,1)
    def test_restart_unchanged_start_is_unknown_and_pending_fences_settings(self):
        self.accept('restart')
        with patch('service_provisioning.command',side_effect=ValueError('unknown')):
            lifecycle.apply(self.db,'lifecycle-operation-001',docker='docker',authorize=lambda:None)
        with patch('service_provisioning.command') as command:
            self.assertEqual(lifecycle.reconcile(self.db,'lifecycle-operation-001',docker='docker',authorize=lambda:None)['status'],'recovery-required')
            command.assert_not_called()
        self.assertTrue(service_runtime_operations.pending(self.db,'app','development'))
    def test_stale_id_and_unauthorized_acceptance_have_no_intent(self):
        with self.assertRaises(ValueError):lifecycle.accept(self.db,'lifecycle-operation-001',self.sid,'start',expected_container_id='0'*64,docker='docker',authorize=lambda:None)
        with self.assertRaises(ValueError):lifecycle.accept(self.db,'lifecycle-operation-001',self.sid,'start',expected_container_id=self.f.binding['container_id'],docker='docker',authorize=lambda:(_ for _ in ()).throw(ValueError('denied')))
        self.assertFalse(lifecycle.pending(self.db,'app','development'))
    def test_start_preserves_id_and_updates_running_selection(self):
        binding=dict(self.f.binding,state='stopped')
        self.db.execute('UPDATE service_bindings SET document=? WHERE service_id=?',(json.dumps(binding),self.sid))
        self.db.execute("UPDATE managed_services SET state='stopped' WHERE id=?",(self.sid,))
        self.actual['State']['Running']=False
        self.accept('start')
        def start(*a,**k):self.actual['State'].update(Running=True,StartedAt='2026-10-11T02:00:00Z')
        with patch('service_provisioning.command',side_effect=start):
            self.assertEqual(lifecycle.apply(self.db,'lifecycle-operation-001',docker='docker',authorize=lambda:None)['status'],'succeeded')
        current=managed_services.get(self.db,self.sid)
        self.assertEqual(current['state'],'running');self.assertEqual(current['desired_state'],'running')
        selected=__import__('managed_delivery').capture(self.db,'app','development')
        self.assertEqual(selected['services'][0]['state'],'pending')
    def test_restart_verified_effect_and_immutable_definition(self):
        old=managed_services.get(self.db,self.sid)['definition']
        self.accept('restart')
        def restart(*a,**k):self.actual['State']['StartedAt']='2026-10-11T03:00:00Z'
        with patch('service_provisioning.command',side_effect=restart):
            self.assertEqual(lifecycle.apply(self.db,'lifecycle-operation-001',docker='docker',authorize=lambda:None)['status'],'succeeded')
        self.assertEqual(managed_services.get(self.db,self.sid)['definition'],old)
    def test_worker_only_replays_proven_unstarted_intent(self):
        self.accept('restart')
        with patch.object(lifecycle,'apply') as apply,patch.object(lifecycle,'reconcile') as reconcile:
            lifecycle.recover_pending(self.db,docker='docker',authorize_project=lambda p:None)
            apply.assert_called_once();reconcile.assert_not_called()
        self.db.execute("UPDATE managed_lifecycle_operations SET state='applying'")
        with patch.object(lifecycle,'apply') as apply,patch.object(lifecycle,'reconcile') as reconcile:
            lifecycle.recover_pending(self.db,docker='docker',authorize_project=lambda p:None)
            apply.assert_not_called();reconcile.assert_called_once()
