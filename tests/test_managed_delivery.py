import copy
import dataclasses
import json
import time
import unittest
from unittest.mock import patch

import managed_delivery as delivery
import managed_services as services
import registry
import test_managed_services as fixtures


class ManagedDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagedServiceTests(); self.fixture.setUp(); self.db = self.fixture.db
        self.fixture.principal = dataclasses.replace(self.fixture.principal, scopes=self.fixture.principal.scopes | {'services:start'})
        result = self.fixture.create(dict(self.fixture.values, desired_state='running'))
        claim = services.claim_next(self.db, 'provision')
        self.sid = result['service']['id']
        self.binding = {'container_id': 'c'*64, 'container_name':'app-development-web-1',
                        'compose_project':'app','compose_service':'development-web',
                        'compose_files':['/managed/app/development/compose.json'],'state':'running'}
        services.finish(self.db, claim['operation_id'], 'provision', self.binding, 'running')
        self.selection = delivery.capture(self.db,'app','development')
        self.target = {'mode':'deploy','project':'app','environment':'development',
                       'environment_revision':self.selection['environment_revision'],
                       'fingerprint':'pinned-source','recipe':{'schema_kind':'managed-build','managed_target':self.selection}}
        self.job = self.fixture.job
        artifact = dict(self.fixture.artifact, source_job=self.job, source_environment='development', image='sha256:'+'d'*64)
        self.target_patch = patch('managed_delivery.job_target.load',return_value=self.target); self.target_patch.start()
        self.artifact_patch = patch('managed_delivery.services.artifact_for_service',return_value=artifact); self.artifact_patch.start()

    def tearDown(self):
        self.target_patch.stop(); self.artifact_patch.stop(); self.fixture.tearDown()

    def plan(self):
        delivery.prepare(self.db,self.job)
        return delivery.claim(self.db,self.job,'worker')

    def replacement(self):
        plan=delivery._load(self.db,self.job)[1]
        entry=plan['services'][0]
        return dict(self.binding,container_id='e'*64,observed=time.time(),image_id=entry['artifact']['image'],definition_digest=services.digest(entry['next_definition']))

    def test_persisted_plan_cas_complete_and_resource_preservation(self):
        plan = self.plan(); old_id=services.get(self.db,self.sid)['definition_id']
        self.assertEqual(delivery.prepare(self.db,self.job),plan)
        self.assertIsNone(delivery.claim(self.db,self.job,'second'))
        self.assertEqual(delivery.protected_images(self.db), {'sha256:'+'a'*64, 'sha256:'+'d'*64})
        result=delivery.complete_service(self.db,self.job,'worker',self.sid,self.replacement())
        self.assertEqual(result['status'],'succeeded')
        actual=services.get(self.db,self.sid)
        self.assertNotEqual(actual['definition_id'],old_id)
        self.assertEqual(actual['definition']['resources'],self.fixture.values['resources'])
        self.assertEqual(actual['operation_id'],plan['services'][0]['operation_id'])
        self.assertEqual(delivery.protected_images(self.db),set())
        self.assertIn('sha256:'+'d'*64, services.protected_images(self.db))
        self.assertNotIn('compose_files',json.dumps(result))
        with self.assertRaises(ValueError): delivery.complete_service(self.db,self.job,'worker',self.sid,self.replacement())

    def test_expiration_is_not_replay_and_cannot_commit(self):
        self.plan(); self.db.execute('UPDATE managed_deliveries SET expires=0')
        self.assertEqual(delivery.public(self.db,self.job)['status'],'recovery-required')
        self.assertIsNone(delivery.claim(self.db,self.job,'replacement'))
        with self.assertRaises(ValueError): delivery.complete_service(self.db,self.job,'worker',self.sid,self.replacement())

    def test_policy_change_fences_runtime_and_no_resource_rollback(self):
        self.plan(); self.db.execute("UPDATE resource_policies SET revision=revision+1 WHERE project='app'")
        with self.assertRaises(ValueError): delivery.authorize_claim(self.db,self.job,'worker')
        result=delivery.fail(self.db,self.job,'worker',detail='secret docker output')
        self.assertEqual(result['status'],'recovery-required'); self.assertNotIn('secret',json.dumps(result))

    def test_stopped_selection_is_never_started(self):
        self.db.execute("UPDATE managed_services SET state='stopped' WHERE id=?",(self.sid,))
        self.db.execute('UPDATE service_bindings SET document=? WHERE service_id=?',(registry.canonical(dict(self.binding,state='stopped')),self.sid))
        self.target['recipe']['managed_target']=delivery.capture(self.db,'app','development')
        plan=delivery.prepare(self.db,self.job)
        self.assertEqual(plan['services'][0]['state'],'skipped-stopped')
        self.assertEqual(delivery.public(self.db,self.job)['status'],'succeeded')
        self.assertIsNone(delivery.claim(self.db,self.job,'worker'))
        self.assertEqual(services.get(self.db,self.sid)['state'],'stopped')

    def test_verified_rollback_retains_old_definition(self):
        self.plan(); old=services.get(self.db,self.sid)['definition_id']
        delivery.rollback_service(self.db,self.job,'worker',self.sid,dict(self.binding,container_id='e'*64,observed=time.time()))
        result=delivery.fail(self.db,self.job,'worker',recovery_required=False)
        self.assertEqual(result['services'][0]['status'],'rolled-back')
        self.assertEqual(services.get(self.db,self.sid)['definition_id'],old)
        self.assertEqual(services.get(self.db,self.sid)['binding']['container_id'],'e'*64)

    def test_cross_environment_and_corrupt_selection_rejected(self):
        with self.assertRaises(ValueError): delivery.capture(self.db,'app','production',[self.sid])
        bad=copy.deepcopy(self.selection); bad['services'][0]['definition']['project']='other'
        with self.assertRaises(ValueError): delivery.validate_target(bad)
        self.target['mode']='validate'
        with self.assertRaises(ValueError): delivery.prepare(self.db,self.job)

    def test_durable_reconnect_and_binding_drift_fence(self):
        self.plan()
        from store import connect
        other=connect(self.fixture.path)
        try:
            self.assertEqual(delivery.public(other,self.job)['status'],'running')
            other.execute('UPDATE service_bindings SET document=? WHERE service_id=?',
                          (registry.canonical(dict(self.binding,container_id='f'*64)),self.sid))
            with self.assertRaises(ValueError): delivery.authorize_claim(self.db,self.job,'worker')
        finally: other.close()

    def test_closed_schema_rejects_malformed_targets(self):
        for value in (None,{},dict(self.selection,services=[{}]),dict(self.selection,environment_service_ids=None)):
            with self.subTest(value=value), self.assertRaises(ValueError): delivery.validate_target(value)

    def test_rejected_claim_can_abandon_only_unstarted_plan(self):
        delivery.prepare(self.db,self.job)
        self.db.execute("UPDATE resource_policies SET revision=revision+1 WHERE project='app'")
        with self.assertRaises(ValueError): delivery.claim(self.db,self.job,'worker')
        self.assertTrue(delivery.abandon_unstarted(self.db,self.job))
        self.assertEqual(delivery.public(self.db,self.job)['status'],'failed')
        self.assertEqual(delivery.capture(self.db,'app','development')['services'][0]['service_id'],self.sid)
        self.assertFalse(delivery.abandon_unstarted(self.db,self.job))

    def test_abandon_does_not_clear_active_expired_or_journaled_execution(self):
        self.plan()
        self.assertFalse(delivery.abandon_unstarted(self.db,self.job))
        self.db.execute('UPDATE managed_deliveries SET expires=0')
        self.assertFalse(delivery.abandon_unstarted(self.db,self.job))
        delivery.claim(self.db,self.job,'other')
        self.assertFalse(delivery.abandon_unstarted(self.db,self.job))
        self.db.execute("UPDATE managed_deliveries SET state='queued',worker=NULL,expires=NULL")
        self.db.execute('CREATE TABLE managed_rollout_journals(operation_id TEXT PRIMARY KEY)')
        self.db.execute('INSERT INTO managed_rollout_journals VALUES(?)',(str(self.job)+'-'+self.sid,))
        self.assertFalse(delivery.abandon_unstarted(self.db,self.job))
        self.assertEqual(delivery.public(self.db,self.job)['status'],'queued')
