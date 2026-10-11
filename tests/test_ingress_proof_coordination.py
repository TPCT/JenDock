import json
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import ingress_proof_refresh as maintenance
import ingress_runtime as runtime
import ingress_route_generations as generations
import managed_delivery
import registry
import test_managed_delivery as delivery_fixtures
import test_cloudflare_dns as dns_fixtures
import test_ingress_runtime as gateway_fixtures


class ProofCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.f=delivery_fixtures.ManagedDeliveryTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.db=self.f.db;self.f.plan();managed_delivery.complete_service(self.db,self.f.job,'worker',self.f.sid,self.f.replacement())
        self.plan=managed_delivery._load(self.db,self.f.job)[1]
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.gateway=gateway_fixtures.GatewayConfigurationTests().definition();self.gateway['schema_version']=3
        runtime.initialize(self.db);generations.initialize(self.db)
        self.db.execute('INSERT INTO ingress_runtime_intents VALUES(?,?,?,?,?,?,?,?,?)',('app','development','gateway-operation-12345',registry.canonical(self.gateway),runtime.digest(self.gateway),str(self.root),'{}','verified',0))
        maintenance.initialize(self.db)
        with registry.transaction(self.db):maintenance.enqueue(self.db,self.f.job)
    def run_step(self,refresh,recover=None):
        with patch.object(maintenance,'route_authority',return_value=[]),patch.object(maintenance.ingress_bindings,'current_routes',return_value=self.gateway['routes']):
            return maintenance.process_once(self.db,root=self.root,docker='docker',refresh=refresh,recover=recover or refresh)
    def test_released_delivery_lease_does_not_block_maintenance(self):
        self.assertIsNone(managed_delivery._load(self.db,self.f.job)[0]['worker'])
        def effect(db,gateway,operation,**kw):
            self.assertFalse(db.in_transaction);kw['authorize']()
        self.run_step(effect)
        self.assertEqual(self.db.execute('SELECT state FROM ingress_delivery_refresh').fetchone()[0],'succeeded')
    def test_saved_unknown_intent_recovers_same_id_after_restart(self):
        calls=[]
        def lost(db,gateway,operation,**kw):
            kw['authorize']();calls.append(operation)
            db.execute('INSERT INTO ingress_route_operations VALUES(?,?,?,?,?)',(operation,'app','development','{}','reload-intent'))
            raise TimeoutError()
        self.run_step(lost)
        self.db.execute('UPDATE ingress_delivery_refresh SET updated=0')
        def recover(db,gateway,operation,**kw):kw['authorize']();self.assertEqual(operation,calls[0])
        self.run_step(lambda *a,**k:self.fail('Mutation replayed'),recover)
        self.assertEqual(len(calls),1)
    def test_proven_pre_signal_abort_gets_bounded_new_attempt(self):
        calls=[];head={'generation':1,'routes':self.gateway['routes']}
        _,config,_=runtime._paths(self.root,self.gateway,create=True);config.write_text('old-owned-config')
        def aborted(db,gateway,operation,**kw):
            kw['authorize']();calls.append(operation)
            document={'kind':'proof-refresh','abort_reason':'pre-signal-failure','gateway_digest':runtime.digest(gateway),'backends':gateway['routes'],'previous':'old-owned-config','before':head}
            db.execute('INSERT INTO ingress_route_operations VALUES(?,?,?,?,?)',(operation,'app','development',registry.canonical(document),'aborted'))
            raise ValueError('configtest rejected')
        with patch.object(generations,'head',return_value=head):
            for _ in range(5):
                self.db.execute('UPDATE ingress_delivery_refresh SET updated=0')
                self.run_step(aborted)
        self.assertEqual(len(calls),4);self.assertEqual(len(set(calls)),4)
        row=self.db.execute('SELECT * FROM ingress_delivery_refresh').fetchone()
        self.assertEqual(row['state'],'recovery-required');self.assertEqual(len(json.loads(row['document'])['attempts']),3)

    def test_enqueue_rolls_back_with_application_transaction(self):
        self.db.execute('DELETE FROM ingress_delivery_refresh')
        with self.assertRaises(RuntimeError):
            with registry.transaction(self.db):maintenance.enqueue(self.db,self.f.job);raise RuntimeError()
        self.assertEqual(self.db.execute('SELECT count(*) FROM ingress_delivery_refresh').fetchone()[0],0)
    def test_scan_repairs_missing_queue_for_terminal_routed_delivery(self):
        self.db.execute('DELETE FROM ingress_delivery_refresh');plan=dict(self.plan)
        plan['services'][0]['ingress_current']={'recorded':True}
        self.db.execute('UPDATE managed_deliveries SET document=?',(registry.canonical(plan),))
        maintenance.scan(self.db)
        self.assertEqual(self.db.execute('SELECT state FROM ingress_delivery_refresh').fetchone()[0],'pending')
    def test_binding_drift_blocks_independent_authority(self):
        self.db.execute("UPDATE service_bindings SET document='{}'")
        with self.assertRaises(ValueError):maintenance.authority(self.db,self.f.job,{})
    def test_failed_untouched_pending_service_requires_no_runtime_journal(self):
        entry=self.plan['services'][0];entry['state']='pending';entry.pop('verified_binding',None)
        self.db.execute('UPDATE managed_services SET definition_id=? WHERE id=?',(entry['definition_id'],entry['service_id']))
        self.db.execute('UPDATE service_bindings SET document=?',(registry.canonical(entry['binding']),))
        self.db.execute("UPDATE managed_deliveries SET state='failed',document=?",(registry.canonical(self.plan),))
        maintenance.authority(self.db,self.f.job,{})
        import managed_rollout
        managed_rollout.initialize(self.db)
        self.db.execute('INSERT INTO managed_rollout_journals VALUES(?,?,?,?)',(str(self.f.job)+'-'+entry['service_id'],'fingerprint','{}',0))
        with self.assertRaises(ValueError):maintenance.authority(self.db,self.f.job,{})

    def test_old_database_without_delivery_tables_is_noop(self):
        db=sqlite3.connect(':memory:',isolation_level=None);db.row_factory=sqlite3.Row
        try:self.assertFalse(maintenance.process_once(db,root=self.root,docker='docker'))
        finally:db.close()


class RouteAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.f=dns_fixtures.DNSOwnershipTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.gateway={'project':'app','environment':'development'}
        self.routes=[{'hostname':'dev.example.test','service_id':'service-one'}]
    def test_live_original_principal_and_revocation(self):
        with patch('domain_execution.resolve_authority',return_value=self.f.fixture.principal) as resolver:
            evidence=maintenance.route_authority(self.f.db,self.gateway,self.routes)
            self.assertEqual(len(evidence),1);self.assertEqual(resolver.call_count,1)
        with patch('domain_execution.resolve_authority',side_effect=ValueError('revoked')):
            with self.assertRaises(ValueError):maintenance.route_authority(self.f.db,self.gateway,self.routes)
    def test_missing_domain_record_fails_closed(self):
        with self.assertRaises(ValueError):maintenance.route_authority(self.f.db,self.gateway,[{'hostname':'foreign.example.test','service_id':'service-one'}])

    def test_provider_drift_blocks_original_principal(self):
        with patch('domain_execution.resolve_authority',return_value=self.f.fixture.principal):
            self.f.db.execute("UPDATE integration_connections SET revision=revision+1")
            with self.assertRaises(ValueError):maintenance.route_authority(self.f.db,self.gateway,self.routes)

    def test_original_source_grant_revocation(self):
        import dataclasses
        principal=dataclasses.replace(self.f.fixture.principal,allowed_environments=frozenset({('app','development')}))
        import domain_execution
        operation=dict(self.f.db.execute('SELECT * FROM ingress_operations').fetchone())
        _,accepted=domain_execution._definition(self.f.db,operation)
        accepted['source_environment']='staging'
        definition_patch=patch('domain_execution._definition',return_value=({},accepted));definition_patch.start();self.addCleanup(definition_patch.stop)
        with patch('domain_execution.resolve_authority',return_value=principal):
            with self.assertRaises(ValueError):maintenance.route_authority(self.f.db,self.gateway,self.routes)

    def test_every_hostname_resolves_its_original_principal(self):
        import dataclasses
        other=dataclasses.replace(self.f.fixture.principal,subject_id='second-owner')
        values=dict(self.f.fixture.values,hostname='peer.example.test')
        preview=self.f.fixture.preview(hostname='peer.example.test')
        import ingress
        values.update({key:preview[key] for key in ingress.PINS})
        self.f.fixture.accept(values,key='second-route-request-12345',principal=other)
        routes=self.routes+[dict(self.routes[0],hostname='peer.example.test')]
        def resolve(db,operation):return other if operation['subject']=='second-owner' else self.f.fixture.principal
        with patch('domain_execution.resolve_authority',side_effect=resolve) as resolver:
            result=maintenance.route_authority(self.f.db,self.gateway,routes)
            self.assertEqual(resolver.call_count,2);self.assertNotEqual(result[0]['subject'],result[1]['subject'])
        def revoked(db,operation):
            if operation['subject']=='second-owner':raise ValueError('revoked')
            return self.f.fixture.principal
        with patch('domain_execution.resolve_authority',side_effect=revoked),self.assertRaises(ValueError):maintenance.route_authority(self.f.db,self.gateway,routes)

    def test_real_authority_callback_allows_owned_pending_refresh(self):
        import test_ingress_proof_refresh as refresh_fixtures
        f=refresh_fixtures.ProofRefreshTests();f.setUp();self.addCleanup(f.doCleanups)
        # Runtime effects are injected; all domain authority reads and the durable
        # prepare/apply journal run against the same actual SQLite database.
        f.db=self.f.db
        f.d['routes']=[dict(f.d['routes'][0],hostname='dev.example.test',service_id='service-one')]
        runtime._save(f.db,f.d,f.root,{'container_id':'e'*64,'transport_id':'f'*64},'verified')
        f.before=generations.head(f.db,f.d);f.token=generations.token(f.d,f.before)
        f.config.write_text(runtime._effective_config(f.db,f.d))
        row=f.db.execute('SELECT backend FROM ingress_backend_bindings').fetchone()
        backend=json.loads(row['backend']);backend['container_id']='8'*64
        f.db.execute('UPDATE ingress_backend_bindings SET backend=?,generation=generation+1',(json.dumps(backend),))
        seen=[]
        def authorize():
            maintenance.route_authority(f.db,f.d,f.d['routes'])
            operation=generations.operation(f.db,f.operation)
            if operation:seen.append(operation['phase'])
        with patch('domain_execution.resolve_authority',return_value=self.f.fixture.principal):
            runtime.refresh_gateway_proof(f.db,f.d,f.operation,root=f.root,docker='docker',authorize=authorize)
        self.assertIn('prepared',seen);self.assertEqual(generations.operation(f.db,f.operation)['phase'],'committed');self.assertEqual(f.signals(),1)
