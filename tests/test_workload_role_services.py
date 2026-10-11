import copy
import json
import sqlite3
import unittest
from unittest.mock import patch
import build_profiles
import managed_services as services
import artifact_publication
import managed_delivery
import job_target
import registry
from store import enqueue
import test_managed_services as fixtures
import test_managed_delivery as delivery_fixtures
from test_workload_role_profiles import role_values


class WorkloadRoleServicesTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ManagedServiceTests();self.f.setUp();self.addCleanup(self.f.tearDown);self.db=self.f.db
    def publish(self,role='scheduler'):
        template='laravel-scheduler-v1' if role=='scheduler' else 'laravel-database-queue-v1'
        recipe=copy.deepcopy(self.f.recipe)
        recipe['service_profiles'][template]=build_profiles.role_profile(template,role_values(role),'app','65534:65534')
        target=job_target.capture('app','legacy-unclassified','TPCT/app',12,'main','e'*40,'validate',1,recipe,project_revision=1)
        job,_=enqueue(self.db,'role-'+role,'app','e'*40,True,'main','validate',target_factory=lambda db:target)
        artifact_publication.record(self.db,job,recipe,{'app':'build:immutable'},status='validated')
        self.assertTrue(artifact_publication.process(self.db,lambda ref:'sha256:'+'e'*64,job))
        artifact=next(services.artifact_for_service(self.db,'app',row['id']) for row in self.db.execute('SELECT id FROM service_artifacts WHERE job_id=?',(job,)) if services.artifact_for_service(self.db,'app',row['id'])['template_id']==template)
        return artifact
    def definition(self,artifact,environment='development',name='scheduler'):
        if not services.role_capability(self.db,artifact)['artifact_verified']:
            services.verify_role_artifact(self.db,'app',artifact['id'],verify=lambda value:services._role_evidence(value))
        return services.validate_definition(self.db,'app',environment,dict(self.f.values,name=name,
            artifact_id=artifact['id'],template_id=artifact['template_id'],resources={'memory_mib':32,'cpu':0.1}))
    def test_publication_and_catalog_preserve_role_without_fake_readiness(self):
        artifact=self.publish('queue')
        self.assertEqual(artifact['workload']['role'],'queue')
        catalog=next(row for row in services.catalog(self.db,'app') if row['id']==artifact['template_id'])
        self.assertEqual(catalog['workload'],artifact['workload'])
        self.assertFalse(catalog['role_capability']['operational'])
        definition=self.definition(artifact,name='queue')
        self.assertEqual(definition['artifact']['workload'],artifact['workload'])
    def test_declared_package_is_not_capability_evidence(self):
        artifact=self.publish()
        values=dict(self.f.values,artifact_id=artifact['id'],template_id=artifact['template_id'])
        with self.assertRaises(ValueError):services.validate_definition(self.db,'app','development',values)
        expected=services._role_evidence(artifact)
        for change in [{'image':'sha256:'+'f'*64},{'framework_version':'12.0.0'},{'protocol_version':True},{'package_digest':'b'*64},{'role':'queue'}]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                services.verify_role_artifact(self.db,'app',artifact['id'],verify=lambda value:dict(expected,**change))
        services.verify_role_artifact(self.db,'app',artifact['id'],verify=lambda value:expected)
        self.assertTrue(services.role_capability(self.db,artifact)['artifact_verified'])
        self.assertFalse(services.role_capability(self.db,artifact)['operational'])
        for statement in ['DELETE FROM service_role_verifications',"UPDATE service_role_verifications SET document='{}'",'INSERT OR REPLACE INTO service_role_verifications SELECT * FROM service_role_verifications']:
            with self.assertRaises(sqlite3.IntegrityError):self.db.execute(statement)

    def test_scheduler_reservation_atomic_and_scoped_even_when_stopped(self):
        artifact=self.publish();first=self.definition(artifact)
        with registry.transaction(self.db):accepted=services.accept(self.db,'app','development',first,'role-create-1')
        self.assertEqual(accepted['desired_state'],'stopped')
        with self.assertRaises(ValueError),registry.transaction(self.db):
            services.accept(self.db,'app','development',self.definition(artifact,name='second'),'role-create-2')
        with registry.transaction(self.db):services.accept(self.db,'app','production',self.definition(artifact,'production'),'role-create-3')
        self.assertEqual(self.db.execute('SELECT count(*) FROM service_scheduler_scopes').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM managed_services').fetchone()[0],2)
        for statement in ["DELETE FROM service_scheduler_scopes","UPDATE service_scheduler_scopes SET scope='other'","INSERT OR REPLACE INTO service_scheduler_scopes SELECT * FROM service_scheduler_scopes"]:
            with self.subTest(statement=statement),self.assertRaises(sqlite3.IntegrityError):self.db.execute(statement)
        self.assertEqual(services.get(self.db,accepted['id'])['definition']['artifact']['workload'],artifact['workload'])
    def test_failed_acceptance_does_not_leak_scheduler_reservation(self):
        artifact=self.publish();definition=self.definition(artifact)
        self.db.execute("CREATE TRIGGER reject_role BEFORE INSERT ON managed_services BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(Exception),registry.transaction(self.db):services.accept(self.db,'app','development',definition,'role-create-1')
        self.assertEqual(self.db.execute('SELECT count(*) FROM service_scheduler_scopes').fetchone()[0],0)


class WorkloadRoleDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.f=delivery_fixtures.ManagedDeliveryTests();self.f.setUp();self.addCleanup(self.f.tearDown)
    def test_plain_application_cannot_be_replaced_by_role_on_same_template(self):
        # The fixture patches artifact lookup; preserve its complete original result.
        artifact=services.artifact_for_service(self.f.db,'app',self.f.fixture.artifact['id'])
        artifact['workload']=build_profiles.role_profile('laravel-database-queue-v1',role_values(),'app','65534:65534')['workload']
        with patch('managed_delivery.services.artifact_for_service',return_value=artifact),self.assertRaises(ValueError):
            managed_delivery.prepare(self.f.db,self.f.job)
