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

    def recovery(self):
        self.plan()
        self.db.execute('UPDATE managed_deliveries SET expires=0')
        return delivery.claim_recovery(self.db,self.job,'recover')

    def old_observation(self):
        entry=delivery._load(self.db,self.job)[1]['services'][0]
        return dict(self.binding,observed=time.time(),image_id=entry['definition']['artifact']['image'],definition_digest=entry['definition_digest'])

    def test_recovery_observed_new_commits_and_fences_executor(self):
        self.recovery()
        with self.assertRaises(ValueError): delivery.authorize_claim(self.db,self.job,'recover')
        with self.assertRaises(ValueError): delivery.complete_service(self.db,self.job,'worker',self.sid,self.replacement())
        with self.assertRaises(ValueError): delivery.capture(self.db,'app','development')
        self.assertEqual(len(delivery.protected_images(self.db)),2)
        delivery.renew_recovery(self.db,self.job,'recover')
        result=delivery.finish_recovery(self.db,self.job,'recover',{self.sid:{'outcome':'new','binding':self.replacement()}})
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(services.get(self.db,self.sid)['binding']['container_id'],'e'*64)

    def test_recovery_old_keeps_definition_and_releases_environment(self):
        self.recovery(); old=services.get(self.db,self.sid)['definition_id']
        count=self.db.execute('SELECT COUNT(*) FROM service_definitions').fetchone()[0]
        result=delivery.finish_recovery(self.db,self.job,'recover',{self.sid:{'outcome':'old','binding':self.old_observation()}})
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['services'][0]['status'],'unchanged')
        self.assertEqual(services.get(self.db,self.sid)['definition_id'],old)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM service_definitions').fetchone()[0],count)
        delivery.capture(self.db,'app','development')

    def test_unknown_recovery_keeps_protection_and_durable_backoff(self):
        self.recovery()
        self.assertEqual(delivery.finish_recovery(self.db,self.job,'recover',{})['status'],'recovery-required')
        self.assertIsNone(delivery.claim_recovery(self.db,self.job,'second'))
        with self.assertRaises(ValueError): delivery.capture(self.db,'app','development')
        self.assertEqual(len(delivery.protected_images(self.db)),2)
        self.db.execute('UPDATE managed_deliveries SET recovery_attempted=0')
        self.assertIsNotNone(delivery.claim_recovery(self.db,self.job,'second'))
        with self.assertRaises(ValueError): delivery.finish_recovery(self.db,self.job,'recover',{})

    def test_active_and_expired_recovery_leases_cannot_be_stolen_or_commit(self):
        self.plan()
        self.assertIsNone(delivery.claim_recovery(self.db,self.job,'recover'))
        self.db.execute('UPDATE managed_deliveries SET expires=0')
        delivery.claim_recovery(self.db,self.job,'recover')
        self.assertIsNone(delivery.claim_recovery(self.db,self.job,'second'))
        self.db.execute('UPDATE managed_deliveries SET expires=0')
        with self.assertRaises(ValueError): delivery.finish_recovery(self.db,self.job,'recover',{})

    def test_recovery_rejects_stale_or_foreign_evidence_and_metadata_drift(self):
        self.recovery()
        for binding in (dict(self.replacement(),observed=0),dict(self.replacement(),compose_project='other')):
            with self.assertRaises(ValueError):
                delivery.finish_recovery(self.db,self.job,'recover',{self.sid:{'outcome':'new','binding':binding}})
        self.db.execute("UPDATE resource_policies SET revision=revision+1 WHERE project='app'")
        with self.assertRaises(ValueError): delivery.finish_recovery(self.db,self.job,'recover',{})

    def add_peer(self, stopped=False):
        import resource_policy
        revision=resource_policy.read(self.db,'app')['revision']
        values=dict(self.fixture.values,name='peer',environment_revision=services.ensure_environment_eligible(self.db,'app','development')['revision'],policy_revision=revision,resources={'memory_mib':64,'cpu':0.25},desired_state='stopped' if stopped else 'running')
        result=self.fixture.create(values,key='create-peer-for-recovery')
        claimed=services.claim_next(self.db,'provision-peer')
        sid=result['service']['id']
        binding=dict(self.binding,container_id='f'*64,container_name='app-development-peer-1',compose_service='development-peer',state='stopped' if stopped else 'running')
        services.finish(self.db,claimed['operation_id'],'provision-peer',binding,binding['state'])
        self.target['recipe']['managed_target']=delivery.capture(self.db,'app','development')
        self.target['environment_revision']=self.target['recipe']['managed_target']['environment_revision']
        return sid,binding

    def test_partial_recovery_rechecks_known_peers_before_unlocking(self):
        peer,_=self.add_peer()
        self.recovery()
        entry=next(e for e in delivery._load(self.db,self.job)[1]['services'] if e['service_id']==self.sid)
        replacement=dict(self.binding,container_id='e'*64,observed=time.time(),image_id=entry['artifact']['image'],definition_digest=services.digest(entry['next_definition']))
        result=delivery.finish_recovery(self.db,self.job,'recover',{self.sid:{'outcome':'new','binding':replacement}})
        self.assertEqual(result['status'],'recovery-required')
        self.assertEqual(services.get(self.db,self.sid)['definition_id'],entry['next_definition_id'])
        self.db.execute('UPDATE managed_deliveries SET recovery_attempted=0')
        plan=delivery.claim_recovery(self.db,self.job,'second')
        old=next(e for e in plan['services'] if e['service_id']==peer)
        old_binding=dict(old['binding'],observed=time.time(),image_id=old['definition']['artifact']['image'],definition_digest=old['definition_digest'])
        result=delivery.finish_recovery(self.db,self.job,'second',{self.sid:{'outcome':'new','binding':replacement},peer:{'outcome':'old','binding':old_binding}})
        self.assertEqual(result['status'],'failed')
        delivery.capture(self.db,'app','development')

    def test_stopped_peer_must_be_observed_and_stays_stopped(self):
        peer,_=self.add_peer(stopped=True)
        self.recovery()
        plan=delivery._load(self.db,self.job)[1]
        outcomes={}
        for entry in plan['services']:
            stopped=entry['state']=='skipped-stopped'
            definition=entry['definition'] if stopped else entry['next_definition']
            outcomes[entry['service_id']]={'outcome':'old' if stopped else 'new','binding':dict(entry['binding'],observed=time.time(),image_id=definition['artifact']['image'],definition_digest=services.digest(definition))}
        result=delivery.finish_recovery(self.db,self.job,'recover',outcomes)
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(services.get(self.db,peer)['state'],'stopped')
        delivery.capture(self.db,'app','development')
