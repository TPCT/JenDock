"""Credential revocation fences durable work without discarding runtime evidence."""
import time
import unittest
import dataclasses
import application_operations as ops
import managed_services as services
import mcp_credentials
from test_managed_services import ManagedServiceTests

class McpExecutionFenceTests(unittest.TestCase):
    setUp = ManagedServiceTests.setUp
    tearDown = ManagedServiceTests.tearDown

    def credential(self):
        import job_target
        from store import enqueue
        if not getattr(self, 'scoped_artifact', None):
            target=job_target.capture('app','development','TPCT/app',12,'development','c'*40,'validate',1,self.recipe)
            job,_=enqueue(self.db,'mcp-scope-artifact','app','c'*40,True,'development','validate',target_factory=lambda db:target)
            self.db.execute("UPDATE jobs SET status='validated' WHERE id=?",(job,))
            self.scoped_artifact=services.publish_artifact(self.db,job,'application-v1','sha256:'+'c'*64)
            self.values=dict(self.values,artifact_id=self.scoped_artifact['id'])
        self.owner = dataclasses.replace(self.principal, scopes=frozenset({'projects:read','projects:create','services:read','services:create','services:start','operations:read'}))
        result = mcp_credentials.create(self.db, self.owner, {
            'label':'Worker fence test','scopes':list(self.owner.scopes),
            'allowed_projects':['app'],'allowed_repository_ids':[12],
            'allowed_environments':[['app','development']], 'expires_at':time.time()+3600})
        return result, mcp_credentials.authenticate(self.db,result['token'])

    def test_revocation_after_authentication_before_acceptance(self):
        issued, principal = self.credential()
        mcp_credentials.revoke(self.db,self.owner,issued['credential']['id'])
        with self.assertRaises(ops.OperationError):
            ops.create_service(self.db,principal,ops.service_input('app','development',self.values),'mcp-revoked-acceptance')
        self.assertEqual(self.db.execute('SELECT count(*) FROM managed_services').fetchone()[0],0)

    def test_worker_rechecks_revocation_and_preserves_journaled_service(self):
        issued, principal = self.credential()
        result=ops.create_service(self.db,principal,ops.service_input('app','development',self.values),'mcp-worker-fence-test')
        claim=services.claim_next(self.db,'worker')
        self.assertEqual(services.authorize_claim(self.db,claim['operation_id'],'worker')['project'],'app')
        mcp_credentials.revoke(self.db,self.owner,issued['credential']['id'])
        with self.assertRaises(ValueError): services.authorize_claim(self.db,claim['operation_id'],'worker')
        self.assertEqual(services.get(self.db,result['service']['id'])['state'],'provisioning')
        self.assertIsNone(services.get(self.db,result['service']['id'])['binding'])

    def test_project_read_scope_and_cursor(self):
        _, principal=self.credential()
        result=ops.list_projects(self.db,principal,limit=1)
        self.assertEqual([p['id'] for p in result['projects']],['app'])
        self.assertEqual(set(result['projects'][0]['environments']),{'development'})
        self.assertNotIn('legacy_settings',result['projects'][0])
        self.assertNotIn('recipe',repr(result['projects'][0]).replace('recipe_revision',''))
        self.assertIsNone(result['next_cursor'])
        with self.assertRaises(ops.OperationError): ops.get_project(self.db,principal,'other')
        for limit in (0,101,True):
            with self.assertRaises(ops.OperationError): ops.list_projects(self.db,principal,limit=limit)

    def test_console_can_recover_revoked_credential_operation_with_audited_authority(self):
        issued, principal = self.credential()
        result = ops.create_service(self.db, principal, ops.service_input('app', 'development', self.values), 'mcp-recovery-authority')
        operation_id = result['operation']['operation_id']
        services.claim_next(self.db, 'worker')
        mcp_credentials.revoke(self.db, self.owner, issued['credential']['id'])
        services.fail(self.db, operation_id, 'worker', recovery_required=True)
        resumed = ops.resume_service(self.db, self.owner, operation_id)
        self.assertEqual(resumed['operation']['status'], 'queued')
        row = self.db.execute('SELECT credential_id FROM application_operations WHERE id=?', (operation_id,)).fetchone()
        self.assertIsNone(row['credential_id'])
        events = [row['event'] for row in self.db.execute('SELECT event FROM application_operation_events WHERE operation_id=?', (operation_id,))]
        self.assertIn('recovery-authority:' + issued['credential']['id'] + '->console', events)
        services.claim_next(self.db, 'next-worker')
        self.assertEqual(services.authorize_claim(self.db, operation_id, 'next-worker')['project'], 'app')

    def test_recovery_cannot_borrow_original_grants_or_change_owner(self):
        issued, principal = self.credential()
        values = dict(self.values, desired_state='running')
        result = ops.create_service(self.db, principal, ops.service_input('app', 'development', values), 'mcp-recovery-no-borrow')
        operation_id = result['operation']['operation_id']
        services.claim_next(self.db, 'worker')
        services.fail(self.db, operation_id, 'worker', recovery_required=True)
        mcp_credentials.revoke(self.db, self.owner, issued['credential']['id'])
        for caller in (dataclasses.replace(self.owner, subject_id='another-owner'),
                       dataclasses.replace(self.owner, scopes=self.owner.scopes - {'services:start'}),
                       dataclasses.replace(self.owner, allowed_environments=frozenset()), principal):
            with self.subTest(caller=caller), self.assertRaises(ops.OperationError):
                ops.resume_service(self.db, caller, operation_id)
        row = self.db.execute('SELECT credential_id FROM application_operations WHERE id=?', (operation_id,)).fetchone()
        self.assertEqual(row['credential_id'], issued['credential']['id'])
        self.assertEqual(services.get(self.db, result['service']['id'])['state'], 'recovery-required')

    def production_source_values(self):
        import job_target
        from store import enqueue
        target = job_target.capture('app', 'production', 'TPCT/app', 12, 'main', 'd'*40,
                                    'validate', 1, self.recipe)
        job, _ = enqueue(self.db, 'mcp-production-source-artifact', 'app', 'd'*40, True,
                         'main', 'validate', target_factory=lambda db: target)
        self.db.execute("UPDATE jobs SET status='validated' WHERE id=?", (job,))
        artifact = services.publish_artifact(self.db, job, 'application-v1', 'sha256:'+'d'*64)
        return dict(self.values, artifact_id=artifact['id'])

    def test_guessed_artifact_from_forbidden_source_environment_cannot_be_accepted(self):
        _, principal = self.credential()
        values = self.production_source_values()
        with self.assertRaises(ops.OperationError) as failure:
            ops.create_service(self.db, principal, ops.service_input('app', 'development', values),
                               'mcp-forbidden-source-create')
        self.assertEqual(failure.exception.code, 'forbidden')
        self.assertEqual(self.db.execute('SELECT count(*) FROM managed_services').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM service_reservations').fetchone()[0], 0)

    def test_replay_cannot_borrow_console_artifact_source_permission(self):
        _, principal = self.credential()
        values = self.production_source_values()
        key = 'mcp-forbidden-source-replay'
        ops.create_service(self.db, self.owner, ops.service_input('app', 'development', values), key)
        with self.assertRaises(ops.OperationError):
            ops.create_service(self.db, principal, ops.service_input('app', 'development', values), key)

    def test_worker_rechecks_artifact_source_for_previously_accepted_work(self):
        issued, _ = self.credential()
        values = self.production_source_values()
        result = ops.create_service(self.db, self.owner, ops.service_input('app', 'development', values),
                                    'mcp-forbidden-source-worker')
        operation_id = result['operation']['operation_id']
        # Represent a durable request accepted before source-environment fencing existed.
        self.db.execute('UPDATE application_operations SET credential_id=? WHERE id=?',
                        (issued['credential']['id'], operation_id))
        services.claim_next(self.db, 'source-worker')
        with self.assertRaises(ops.OperationError) as failure:
            services.authorize_claim(self.db, operation_id, 'source-worker')
        self.assertEqual(failure.exception.code, 'forbidden')
        self.assertIsNone(services.get(self.db, result['service']['id'])['binding'])

    def test_recovery_cannot_assume_unauthorized_source_environment(self):
        _, principal = self.credential()
        values = self.production_source_values()
        result = ops.create_service(self.db, self.owner, ops.service_input('app', 'development', values),
                                    'mcp-forbidden-source-recovery')
        operation_id = result['operation']['operation_id']
        services.claim_next(self.db, 'source-worker')
        services.fail(self.db, operation_id, 'source-worker', recovery_required=True)
        with self.assertRaises(ops.OperationError): ops.resume_service(self.db, principal, operation_id)
        row = self.db.execute('SELECT credential_id FROM application_operations WHERE id=?', (operation_id,)).fetchone()
        self.assertIsNone(row['credential_id'])
        self.assertEqual(services.get(self.db, result['service']['id'])['state'], 'recovery-required')
