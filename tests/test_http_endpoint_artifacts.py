import copy
import json
import unittest
import artifact_publication
import job_target
import managed_services as services
import registry
from store import enqueue
import test_managed_services as fixtures


class HttpEndpointArtifactTests(unittest.TestCase):
    setUp=fixtures.ManagedServiceTests.setUp
    tearDown=fixtures.ManagedServiceTests.tearDown

    def source(self):
        recipe=copy.deepcopy(self.recipe)
        recipe['service_profiles']['application-v1'].update(revision=2,http_endpoint={'port':8080,'health_path':'/ready'})
        target=job_target.capture('app','development','TPCT/app',12,'development','e'*40,'validate',1,recipe)
        job,_=enqueue(self.db,'endpoint-v2','app','e'*40,True,'development','validate',target_factory=lambda db:target)
        self.db.execute("UPDATE jobs SET status='validated' WHERE id=?",(job,))
        return job,recipe

    def test_source_endpoint_copied_into_immutable_artifact_and_definition(self):
        job,recipe=self.source()
        artifact=services.publish_artifact(self.db,job,'application-v1','sha256:'+'e'*64)
        endpoint=recipe['service_profiles']['application-v1']['http_endpoint']
        self.assertEqual(artifact['http_endpoint'],endpoint)
        self.assertEqual(services.artifact_for_service(self.db,'app',artifact['id']),artifact)
        values=dict(self.values,artifact_id=artifact['id'],template_revision=2)
        definition=services.validate_definition(self.db,'app','development',values)
        self.assertEqual(definition['artifact']['http_endpoint'],endpoint)
        self.assertEqual(services.catalog(self.db,'app')[0]['http_endpoint'],endpoint)
        self.assertNotIn('http_endpoint',self.artifact)
        old=services.artifact_for_service(self.db,'app',self.artifact['id'])
        self.assertEqual(old['digest'],self.artifact['digest'])

    def test_publication_retry_preserves_endpoint(self):
        job,recipe=self.source()
        artifact_publication.record(self.db,job,recipe,{'app':'app:verified'})
        self.assertTrue(artifact_publication.process(self.db,lambda image:'sha256:'+'e'*64,job))
        artifact=services.catalog(self.db,'app')[0]
        self.assertEqual(artifact['http_endpoint'],{'port':8080,'health_path':'/ready'})
        before=self.db.execute('SELECT document,digest FROM service_artifacts WHERE job_id=?',(job,)).fetchone()
        self.assertTrue(artifact_publication.process(self.db,lambda image:self.fail('must not inspect again'),job))
        after=self.db.execute('SELECT document,digest FROM service_artifacts WHERE job_id=?',(job,)).fetchone()
        self.assertEqual(tuple(before),tuple(after))

    def test_rehashed_endpoint_tamper_is_rejected_against_source(self):
        job,_=self.source();artifact=services.publish_artifact(self.db,job,'application-v1','sha256:'+'e'*64)
        self.db.execute('DROP TRIGGER service_artifacts_immutable_update')
        row=self.db.execute('SELECT document FROM service_artifacts WHERE id=?',(artifact['id'],)).fetchone()
        document=json.loads(row['document']);document['http_endpoint']['port']=9090
        self.db.execute('UPDATE service_artifacts SET document=?,digest=? WHERE id=?',
                        (registry.canonical(document),services.digest(document),artifact['id']))
        with self.assertRaises(ValueError):services.artifact_for_service(self.db,'app',artifact['id'])

    def test_v1_endpoint_injection_and_unknown_profile_revision_rejected(self):
        target={'recipe':copy.deepcopy(self.recipe)}
        profile=target['recipe']['service_profiles']['application-v1']
        profile['http_endpoint']={'port':80,'health_path':'/'}
        with self.assertRaises(ValueError):services._profile(target,'application-v1')
        profile['revision']=3
        with self.assertRaises(ValueError):services._profile(target,'application-v1')

    def test_generated_v2_build_recipe_reaches_persisted_service_definition(self):
        import application_operations as operations
        import build_profiles
        values={'dockerfile':'Dockerfile','context':'.','target':None,'test_command':['app','test'],
                'command':['app','serve'],'healthcheck':['app','health'],'user':'65534:65534',
                'http_endpoint':{'port':8080,'health_path':'/'}}
        saved=build_profiles.save(self.db,'app','development',1,1,0,values)
        source=build_profiles.get_private(self.db,'app','development')
        target=job_target.capture('app','development','TPCT/app',12,'development','f'*40,'validate',
                                  source['source_revision'],source['recipe'])
        job,_=enqueue(self.db,'generated-endpoint-v2','app','f'*40,True,'development','validate',target_factory=lambda db:target)
        self.db.execute("UPDATE jobs SET status='validated' WHERE id=?",(job,))
        artifact=services.publish_artifact(self.db,job,'application-v1','sha256:'+'f'*64)
        service_values=dict(self.values,artifact_id=artifact['id'],template_revision=2,
                            environment_revision=saved['environment_revision'])
        accepted=operations.create_service(self.db,self.principal,operations.service_input('app','development',service_values),
                                           'endpoint-v2-persisted-service')
        persisted=services.get(self.db,accepted['service']['id'])
        self.assertEqual(persisted['definition']['artifact']['http_endpoint'],values['http_endpoint'])
        self.assertEqual(persisted['definition']['artifact']['target_fingerprint'],target['fingerprint'])
        changed=copy.deepcopy(source['recipe']);changed['service_profiles']['application-v1']['http_endpoint']['port']=9090
        self.assertNotEqual(services.digest(changed),services.digest(source['recipe']))
        self.assertNotIn('http_endpoint',service_values)

    def test_legacy_revision_three_artifact_retains_pre_endpoint_digest(self):
        recipe=copy.deepcopy(self.recipe)
        profile=recipe['service_profiles']['application-v1'];profile['revision']=3
        target=job_target.capture('app','development','TPCT/app',12,'development','9'*40,'validate',1,recipe)
        job,_=enqueue(self.db,'legacy-revision-three','app','9'*40,True,'development','validate',target_factory=lambda db:target)
        self.db.execute("UPDATE jobs SET status='validated' WHERE id=?",(job,))
        # Exact pre-endpoint publication shape: the persisted digest must not change.
        document={'project':'app','source_job':job,'recipe_revision':target['recipe_revision'],
                  'source_sha':target['sha'],'source_environment':target['environment'],
                  'target_fingerprint':target['fingerprint'],'template_id':'application-v1',
                  'template_revision':3,'image_name':profile['image_name'],'image':'sha256:'+'9'*64,
                  'command':profile['command'],'healthcheck':profile['healthcheck'],'user':profile['user']}
        fingerprint=services.digest(document)
        self.db.execute('INSERT INTO service_artifacts VALUES(?,?,?,?,?,?)',
                        ('legacy-v3-artifact','app',job,registry.canonical(document),fingerprint,1))
        loaded=services.artifact_for_service(self.db,'app','legacy-v3-artifact')
        self.assertEqual(loaded,dict(document,id='legacy-v3-artifact',digest=fingerprint))
        published=services.publish_artifact(self.db,job,'application-v1','sha256:'+'9'*64)
        self.assertEqual(published,loaded)
        self.assertNotIn('http_endpoint',published)
