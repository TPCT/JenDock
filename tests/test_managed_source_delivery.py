"""Source-to-managed integration through real persisted job targets and registry."""
import dataclasses
import time
import unittest

import artifact_publication
import build_profiles
import delivery_target
import job_target
import managed_delivery
import managed_services
import registry
import test_managed_services as fixtures
import workspace
from store import enqueue, snapshot


class ManagedSourceDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ManagedServiceTests(); self.fixture.setUp(); self.db=self.fixture.db
        self.fixture.principal=dataclasses.replace(self.fixture.principal,scopes=self.fixture.principal.scopes|{'services:start'})
        result=self.fixture.create(dict(self.fixture.values,desired_state='running'))
        claim=managed_services.claim_next(self.db,'provision')
        self.sid=result['service']['id']
        self.binding={'container_id':'c'*64,'container_name':'app-development-web-1','compose_project':'app',
                      'compose_service':'development-web','compose_files':['/managed/app/development/compose.json'],
                      'state':'running','image_id':self.fixture.artifact['image'],'network_ids':['n'*64],
                      'network_name':'app-development-managed','definition_digest':managed_services.digest(claim['definition'])}
        managed_services.finish(self.db,claim['operation_id'],'provision',self.binding,'running')
        self.values={'dockerfile':'Dockerfile','context':'.','target':None,'test_command':['app','test'],
                     'command':['app','serve'],'healthcheck':['app','health'],'user':'65534:65534'}
        self.save_profile('development')
        snapshot(self.db,'containers',[{'ID':'c'*64,'Names':self.binding['container_name'],'State':'running',
                 'ComposeProject':'app','ComposeService':'development-web','ComposeConfigFiles':self.binding['compose_files'][0]}])
        snapshot(self.db,'worker',{'state':'running'})

    def tearDown(self): self.fixture.tearDown()

    def save_profile(self,environment):
        old=build_profiles.get(self.db,'app',environment)
        return build_profiles.save(self.db,'app',environment,old['project_revision'],old['environment_revision'],old['profile_revision'],self.values)

    def enqueue_source(self,environment='development',mode='deploy',sha='d'*40):
        source=build_profiles.get(self.db,'app',environment)
        return enqueue(self.db,'integration-'+environment+'-'+sha,'app',sha,True,'main',mode,
            target_factory=lambda db: delivery_target.capture_registered_target(db,'app','main',sha,mode,
                environment=environment,project_revision=source['project_revision'],environment_revision=source['environment_revision'],
                source_revision=source['source_revision']))[0]

    def publish(self,job,image,status):
        target=job_target.load(self.db,job); name=target['recipe']['images'][0]['name']
        artifact_publication.record(self.db,job,target['recipe'],{name:'temporary-build-ref'},status=status,detail='Isolated source tests verified.')
        self.assertTrue(artifact_publication.process(self.db,lambda ref:image,job))
        return next(item for item in managed_services.catalog(self.db,'app') if item['source_job']==job)

    def test_deploy_target_immutable_selection_and_validation_remains_build_only(self):
        capability=workspace.read(self.db,'app','development')['capabilities']['deploy']
        self.assertTrue(capability['enabled'],capability); self.assertEqual(capability['kind'],'managed-build')
        job=self.enqueue_source(); target=job_target.load(self.db,job)
        self.assertEqual(target['recipe']['schema_kind'],'managed-build')
        self.assertEqual(target['recipe']['managed_target']['services'][0]['service_id'],self.sid)
        self.assertEqual(managed_delivery.load_target(self.db,job)['source_fingerprint'],target['fingerprint'])
        validate=self.enqueue_source(mode='validate',sha='e'*40)
        self.assertEqual(job_target.load(self.db,validate)['recipe']['schema_kind'],'artifact-build')
        with self.assertRaises(ValueError): managed_delivery.load_target(self.db,validate)
        public=job_target.public(self.db,job)
        self.assertNotIn('recipe',public); self.assertNotIn('managed_target',public)

    def test_published_managed_artifact_survives_runtime_failure(self):
        job=self.enqueue_source(); item=self.publish(job,'sha256:'+'d'*64,'deploying')
        self.assertEqual(self.db.execute('SELECT status FROM jobs WHERE id=?',(job,)).fetchone()[0],'deploying')
        plan=managed_delivery.prepare(self.db,job)
        self.assertEqual(plan['services'][0]['artifact']['source_job'],job)
        self.db.execute("UPDATE jobs SET status='failed' WHERE id=?",(job,))
        artifact=managed_services.artifact_for_service(self.db,'app',item['artifact_id'])
        self.assertEqual(artifact['image'],'sha256:'+'d'*64)
        self.assertEqual(artifact['target_fingerprint'],job_target.load(self.db,job)['fingerprint'])

    def test_development_catalog_does_not_hide_production_artifact(self):
        self.save_profile('production')
        production=self.enqueue_source(environment='production',mode='validate',sha='a'*40)
        prod=self.publish(production,'sha256:'+'f'*64,'validated')
        development=self.enqueue_source(mode='validate',sha='f'*40)
        dev=self.publish(development,'sha256:'+'e'*64,'validated')
        catalog=managed_services.catalog(self.db,'app')
        ids={item['artifact_id'] for item in catalog}
        self.assertIn(prod['artifact_id'],ids); self.assertIn(dev['artifact_id'],ids)

    def test_pending_rollout_blocks_only_same_environment_selection(self):
        job=self.enqueue_source(); self.publish(job,'sha256:'+'d'*64,'deploying')
        managed_delivery.prepare(self.db,job)
        with self.assertRaisesRegex(ValueError,'pending or unresolved'): managed_delivery.capture(self.db,'app','development')
        self.db.execute("UPDATE managed_deliveries SET state='recovery-required'")
        with self.assertRaisesRegex(ValueError,'pending or unresolved'): managed_delivery.capture(self.db,'app','development')
        with self.assertRaisesRegex(ValueError,'Create and verify'): managed_delivery.capture(self.db,'app','production')
        self.db.execute("UPDATE managed_deliveries SET state='failed'")
        self.assertEqual(managed_delivery.capture(self.db,'app','development')['services'][0]['service_id'],self.sid)

    def test_stale_observation_disables_deploy_without_blocking_build(self):
        self.db.execute("UPDATE snapshots SET updated=0 WHERE name='containers'")
        capabilities=workspace.read(self.db,'app','development')['capabilities']
        self.assertFalse(capabilities['deploy']['enabled'])
        self.assertTrue(capabilities['build']['enabled'])
        with self.assertRaises(ValueError): self.enqueue_source()
        job=self.enqueue_source(mode='validate')
        self.assertEqual(job_target.load(self.db,job)['mode'],'validate')
