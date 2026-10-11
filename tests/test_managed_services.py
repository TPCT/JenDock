import copy
import dataclasses
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import application_operations as ops
import job_target
import managed_services as services
import registry
import resource_policy
from store import connect, enqueue


class ManagedServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name) / 'db'; self.db = connect(self.path)
        self.recipe = {'compose_project': 'app', 'compose_files': ['/managed/app.yml'], 'services': ['web'],
                       'images': [{'name': 'app'}], 'service_profiles': {'application-v1': {
                           'revision': 1, 'image_name': 'app', 'command': ['app', 'serve'],
                           'healthcheck': ['app', 'health'], 'user': '65534:65534'}}}
        registry.import_legacy(self.db, {'app': {'repository': 'TPCT/app', 'repository_id': 12, 'branch': 'main', 'recipe': self.recipe}}, [])
        target = job_target.capture('app', 'legacy-unclassified', 'TPCT/app', 12, 'main', 'b' * 40, 'validate', 1, self.recipe, project_revision=1)
        self.job, _ = enqueue(self.db, 'artifact-fixture', 'app', 'b' * 40, True, 'main', 'validate', target_factory=lambda db: target)
        self.db.execute("UPDATE jobs SET status='validated' WHERE id=?", (self.job,))
        self.artifact = services.publish_artifact(self.db, self.job, 'application-v1', 'sha256:' + 'a' * 64)
        policy = resource_policy.read(self.db, 'app')['document']; policy['budget'] = {'memory_mib': 512, 'cpu': 2}
        for name in policy['environments']: policy['environments'][name]['budget'] = {'memory_mib': 256, 'cpu': 1}
        resource_policy.save(self.db, 'app', 0, policy)
        self.db.execute('INSERT INTO telemetry VALUES(?,?)', (time.time(), json.dumps({'memory_total': 1073741824, 'memory_used': 0, 'cpu_count': 4})))
        self.principal = ops.Principal('owner', 'console', frozenset({'services:create', 'services:read', 'operations:read'}))
        self.values = {'name': 'web', 'artifact_id': self.artifact['id'], 'template_id': 'application-v1', 'template_revision': 1,
                       'environment_revision': 1, 'policy_revision': 1, 'resources': {'memory_mib': 160, 'cpu': 0.5}}
        ops.initialize(self.db)

    def tearDown(self): self.db.close(); self.tmp.cleanup()

    def create(self, values=None, environment='development', key='service-create-request-123', principal=None):
        return ops.create_service(self.db, principal or self.principal, ops.service_input('app', environment, values or self.values), key)

    def test_queued_replay_reconnect_and_truthful_catalog(self):
        result = self.create(); self.assertEqual(result['operation']['status'], 'queued')
        self.assertEqual(result['service']['desired_state'], 'stopped'); self.assertIsNone(result['service']['container'])
        self.db.close(); self.db = connect(self.path)
        self.assertEqual(self.create(), result)
        self.assertEqual(len(ops.list_services(self.db, self.principal, 'app', 'development')), 1)
        self.assertEqual(services.catalog(self.db, 'app')[0]['artifact_id'], self.artifact['id'])
        self.assertEqual(services.protected_images(self.db), {self.artifact['image']})
        with self.assertRaises(ops.OperationError): self.create(dict(self.values, name='other'))

    def test_permissions_and_closed_input_schema(self):
        restricted = dataclasses.replace(self.principal, allowed_environments=frozenset({('app', 'production')}))
        with self.assertRaises(ops.OperationError): self.create(principal=restricted)
        for change in ({'image': 'arbitrary'}, {'command': ['sh']}, {'resources': {'memory_mib': 128, 'cpu': 1, 'disk': 3}},
                       {'environment_revision': 99}, {'policy_revision': 99}, {'template_revision': 99}, {'artifact_id': 'foreign'}):
            with self.subTest(change=change), self.assertRaises(ops.OperationError): self.create(dict(self.values, **change))
        self.assertEqual(self.db.execute('SELECT count(*) FROM managed_services').fetchone()[0], 0)

    def test_concurrent_capacity_and_same_key_atomicity(self):
        def call(name, key):
            db = connect(self.path)
            try:
                try: return ops.create_service(db, self.principal, ops.service_input('app', 'development', dict(self.values, name=name)), key)
                except ops.OperationError: return None
            finally: db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call, name, 'concurrent-service-key-' + name) for name in ('one', 'two')]
            results = [future.result() for future in futures]
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(services.reservation_totals(self.db, 'app', 'development')['memory_mib'], 160)
        with self.assertRaises(ValueError):
            policy = resource_policy.read(self.db, 'app')['document']; policy['environments']['development']['budget']['memory_mib'] = 128
            services.validate_capacity(self.db, 'app', policy)

    def test_worker_binding_moves_reservation_once_and_status_is_authorized(self):
        result = self.create(); claim = services.claim_next(self.db, 'worker')
        self.assertEqual(claim['definition']['service_id'], result['service']['id'])
        services.authorize_claim(self.db, claim['operation_id'], 'worker')
        binding = {'container_id': 'c' * 64, 'container_name': 'app-development-web-1', 'compose_project': 'app',
                   'compose_service': 'development-web', 'compose_files': ['/managed/app/development/compose.json'], 'state': 'stopped'}
        finished = services.finish(self.db, claim['operation_id'], 'worker', binding, 'stopped')
        self.assertEqual(finished['state'], 'stopped')
        self.assertEqual(services.reservation_totals(self.db, 'app', 'development')['memory_mib'], 0)
        policy = resource_policy.read(self.db, 'app')
        self.assertEqual(policy['document']['environments']['development']['services'][binding['container_name']], self.values['resources'])
        self.assertEqual(ops.get_operation(self.db, self.principal, claim['operation_id'])['status'], 'stopped')
        services.validate_capacity(self.db, 'app', policy['document'])
        with self.assertRaises(ops.OperationError):
            ops.get_operation(self.db, dataclasses.replace(self.principal, allowed_environments=frozenset({('app', 'production')})), claim['operation_id'])

    def test_expired_lease_never_blindly_reexecutes_and_retains_capacity(self):
        self.create(); claim = services.claim_next(self.db, 'worker')
        self.db.execute('UPDATE service_operation_leases SET expires=0')
        self.assertIsNone(services.claim_next(self.db, 'replacement'))
        self.assertEqual(ops.get_operation(self.db, self.principal, claim['operation_id'])['status'], 'recovery-required')
        self.assertEqual(services.reservation_totals(self.db, 'app', 'development')['memory_mib'], 160)

    def test_publication_requires_immutable_reviewed_profile_and_verified_job(self):
        self.db.execute("UPDATE jobs SET status='failed' WHERE id=?", (self.job,))
        with self.assertRaises(ValueError): services.publish_artifact(self.db, self.job, 'application-v1', 'sha256:' + 'd' * 64)
        self.assertEqual(services.catalog(self.db, 'app'), [])
        with self.assertRaises(ops.OperationError): self.create()

    def test_stale_host_metrics_and_bound_legacy_environment_rejected(self):
        self.db.execute('DELETE FROM telemetry')
        with self.assertRaises(ops.OperationError): self.create()
        self.db.execute('INSERT INTO telemetry VALUES(?,?)', (time.time(), json.dumps({'memory_total': 1073741824, 'memory_used': 0, 'cpu_count': 4})))
        record = registry.update_project(self.db, 'app', 1, {'legacy_binding': 'production'})
        with self.assertRaises(ops.OperationError): self.create(dict(self.values, environment_revision=record['environments']['production']['revision']), 'production')

    def test_running_requires_start_scope_even_for_existing_request_replay(self):
        values = dict(self.values, desired_state='running')
        with self.assertRaises(ops.OperationError): self.create(values)
        authorized = dataclasses.replace(self.principal, scopes=self.principal.scopes | {'services:start'})
        accepted = self.create(values, principal=authorized)
        with self.assertRaises(ops.OperationError): self.create(values)
        claim = services.claim_next(self.db, 'worker')
        services.fail(self.db, claim['operation_id'], 'worker')
        with self.assertRaises(ops.OperationError): ops.resume_service(self.db, self.principal, accepted['operation']['operation_id'])
        resumed = ops.resume_service(self.db, authorized, accepted['operation']['operation_id'])
        self.assertEqual(resumed['operation']['status'], 'queued')
        self.assertEqual(resumed['service']['id'], accepted['service']['id'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM service_definitions').fetchone()[0], 1)
        self.assertEqual(ops.resume_service(self.db, authorized, accepted['operation']['operation_id']), resumed)

    def test_malformed_artifact_fields_have_stable_validation_errors(self):
        for field in ('artifact_id', 'template_id'):
            for value in ({}, [], 12, None):
                with self.subTest(field=field,value=value), self.assertRaises(ops.OperationError):
                    self.create(dict(self.values, **{field:value}))

    def test_latest_artifact_retention_keeps_accepted_old_image_only(self):
        old = self.artifact
        accepted = self.create()
        target = job_target.capture('app', 'legacy-unclassified', 'TPCT/app', 12, 'main', 'c'*40, 'validate', 1, self.recipe, project_revision=1)
        newer, _ = enqueue(self.db, 'newer-artifact-fixture', 'app', 'c'*40, True, 'main', 'validate', target_factory=lambda db: target)
        self.db.execute("UPDATE jobs SET status='validated' WHERE id=?", (newer,))
        new = services.publish_artifact(self.db, newer, 'application-v1', 'sha256:' + 'f' * 64)
        services.publish_artifact(self.db, self.job, 'application-v1', 'sha256:' + 'e' * 64)
        self.assertEqual([row['artifact_id'] for row in services.catalog(self.db, 'app')], [new['id']])
        self.assertEqual(services.protected_images(self.db), {old['image'], new['image']})
        with self.assertRaises(ops.OperationError): self.create(dict(self.values,name='another'), environment='production',key='new-service-key-12345')
        self.assertEqual(services.artifact_for_service(self.db,'app',accepted['service']['definition']['artifact_id'])['image'],old['image'])

    def test_concurrent_same_request_returns_one_definition_reservation_event(self):
        def call():
            db=connect(self.path)
            try:return ops.create_service(db,self.principal,ops.service_input('app','development',self.values),'same-service-request-key')
            finally:db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(call) for _ in range(2)]
            results=[future.result() for future in futures]
        self.assertEqual(results[0],results[1])
        self.assertEqual(self.db.execute('SELECT count(*) FROM service_definitions').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM service_reservations').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM application_operation_events').fetchone()[0],1)

    def test_binding_refresh_is_cas_and_preserves_ownership_and_effective_limits(self):
        result=self.create(); claim=services.claim_next(self.db,'worker')
        binding={'container_id':'c'*64,'container_name':'app-development-web-1','compose_project':'app',
                 'compose_service':'development-web','compose_files':['/managed/app/development/compose.json'],
                 'state':'stopped','observed':time.time(),'image_id':self.artifact['image'],'network_ids':['network-id']}
        services.finish(self.db,claim['operation_id'],'worker',binding,'stopped')
        policy=resource_policy.read(self.db,'app'); policy['document']['environments']['development']['services'][binding['container_name']]={'memory_mib':192,'cpu':0.75}
        resource_policy.save(self.db,'app',policy['revision'],policy['document'])
        row=services.environment_definitions(self.db,'app','development')[0]
        self.assertEqual(row['definition']['resources']['memory_mib'],160)
        self.assertEqual(row['effective_resources']['memory_mib'],192)
        replacement=dict(binding,container_id='d'*64,observed=time.time())
        self.assertEqual(services.refresh_binding(self.db,result['service']['id'],'c'*64,replacement),replacement)
        self.assertEqual(services.refresh_binding(self.db,result['service']['id'],'c'*64,replacement),replacement)
        with self.assertRaises(ValueError): services.refresh_binding(self.db,result['service']['id'],'c'*64,dict(replacement,container_id='e'*64))
        with self.assertRaises(ValueError): services.refresh_binding(self.db,result['service']['id'],'d'*64,dict(replacement,image_id='sha256:'+'f'*64))
        self.assertEqual(services.get(self.db,result['service']['id'])['binding'],replacement)

    def test_adoption_after_acceptance_blocks_worker_authorization_before_mutation(self):
        result=self.create()
        registry.update_project(self.db,'app',1,{'legacy_binding':'development'})
        claim=services.claim_next(self.db,'worker')
        self.assertEqual(claim['service_id'],result['service']['id'])
        with self.assertRaises(ValueError): services.authorize_claim(self.db,claim['operation_id'],'worker')
        binding={'container_id':'c'*64,'container_name':'app-development-web-1','compose_project':'app',
                 'compose_service':'development-web','compose_files':['/managed/app/development/compose.json'],'state':'stopped'}
        with self.assertRaises(ValueError): services.finish(self.db,claim['operation_id'],'worker',binding,'stopped')
        self.assertEqual(self.db.execute('SELECT count(*) FROM service_bindings').fetchone()[0],0)

    def test_admission_caps_all_accepted_services_and_internal_read_is_unpaginated(self):
        first = self.create()
        original = self.db.execute('SELECT * FROM managed_services').fetchone()
        # Historical database fixture beyond the new admission bound: internal
        # manifests must still include every definition, not a public page.
        for index in range(100):
            self.db.execute('INSERT INTO managed_services VALUES(?,?,?,?,?,?,?,?,?)',
                ('historical-'+str(index), 'app', 'development', 'old-'+str(index),
                 original['definition_id'], 'old-operation-'+str(index), 'recovery-required', index, index))
        self.assertEqual(len(services.list_services(self.db, 'app', 'development')), 100)
        self.assertEqual(len(services.environment_definitions(self.db, 'app', 'development')), 101)
        with self.assertRaisesRegex(ops.OperationError, 'maximum 100'):
            self.create(dict(self.values, name='overflow'), key='overflow-service-request')
        self.assertEqual(self.db.execute('SELECT count(*) FROM service_definitions').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM service_reservations').fetchone()[0], 1)

    def test_peer_current_state_refresh_preserves_creation_outcome_and_mixed_environment(self):
        principal = dataclasses.replace(self.principal, scopes=self.principal.scopes | {'services:start'})
        values = dict(self.values, desired_state='running', resources={'memory_mib': 64, 'cpu': 0.2})
        first = self.create(values, principal=principal)
        claim = services.claim_next(self.db, 'worker')
        binding = {'container_id': 'c'*64, 'container_name': 'app-development-web-1', 'compose_project': 'app',
                   'compose_service': 'development-web', 'compose_files': ['/managed/app/development/compose.json'],
                   'state': 'running', 'observed': time.time()}
        services.finish(self.db, claim['operation_id'], 'worker', binding, 'running')
        services.refresh_binding(self.db, first['service']['id'], 'c'*64, dict(binding, state='stopped', observed=time.time()))
        original_result = json.loads(self.db.execute('SELECT result FROM application_operations WHERE id=?', (claim['operation_id'],)).fetchone()[0])
        self.assertEqual(original_result['operation']['status'], 'running')
        self.assertEqual(services.get(self.db, first['service']['id'])['state'], 'stopped')
        second_values = dict(values, name='second', environment_revision=registry.get_environment(self.db,'app','development')['revision'],
                             policy_revision=resource_policy.read(self.db,'app')['revision'])
        self.create(second_values, key='second-running-service', principal=principal)
        second_claim = services.claim_next(self.db,'worker')
        services.finish(self.db, second_claim['operation_id'], 'worker',
                        dict(binding, container_id='d'*64, container_name='app-development-second-1', compose_service='development-second'), 'running')
        self.assertEqual(registry.get_environment(self.db,'app','development')['state'], 'degraded')
