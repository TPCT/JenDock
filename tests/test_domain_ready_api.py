"""Readiness contract through public APIs with persisted gateway/backend evidence."""
import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
import domain_execution
import ingress
import ingress_runtime
import registry
import test_domain_execution_tls as tls_fixtures
import test_ingress_runtime as gateway_fixtures


class DomainReadyAPITests(unittest.TestCase):
    def setUp(self):
        self.f=tls_fixtures.DomainTLSTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.f.tls_until('origin-verified');self.f.step()
        self.db=self.f.db;self.operation=self.f.operation;self.route_id=self.f.fixture.route
        self.service=self.f.fixture.fixture.service
        now=time.time();self.now=now
        route={'hostname':'dev.example.test','service_id':self.service['id'],'definition_id':self.service['definition_id'],
            'definition_digest':self.service['binding']['definition_digest'],'container_id':self.service['binding']['container_id'],
            'alias':'development-web','http_endpoint':{'port':8080,'health_path':'/ready'}}
        gateway=gateway_fixtures.GatewayConfigurationTests().definition();gateway.update(schema_version=3,routes=[route])
        ingress_runtime._save(self.db,gateway,Path('/managed'),{'binding':{'container_id':'e'*64}},'verified')
        self.db.execute('INSERT INTO managed_services VALUES(?,?,?,?,?,?,?,?,?)',(route['service_id'],'app','development','web',route['definition_id'],self.operation,'running',now,now))
        binding=dict(self.service['binding'],compose_service='development-web')
        self.db.execute('INSERT INTO service_bindings VALUES(?,?,?)',(route['service_id'],registry.canonical(binding),now))
        journal=json.loads(domain_execution._row(self.db,self.operation)['journal'])
        journal['external_verified']={'hostname':route['hostname'],'gateway_id':'e'*64,'backend_id':route['container_id'],
            'proof_digest':hashlib.sha256(ingress_runtime.route_proof_token('app','development',route).encode()).hexdigest(),
            'tls_fingerprint':journal['certificate']['fingerprint'],'tls_digest':journal['tls_stage']['digest'],
            'observed_at':now,'dns_observed_at':now,'origin_observed_at':now,'expires_at':now+60}
        self.db.execute("UPDATE domain_executions SET state='ready',phase='external',journal=? WHERE operation_id=?",(registry.canonical(journal),self.operation))
        resolver=patch.object(domain_execution,'resolve_authority',side_effect=self.f.resolver);resolver.start();self.addCleanup(resolver.stop)
    def responses(self):
        return [ingress.get_route(self.db,self.f.principal,'app','development',self.route_id),
                ingress.list_routes(self.db,self.f.principal,'app','development')['routes'][0],
                ingress.get_operation(self.db,self.f.principal,'app','development',self.operation)]
    def test_same_ready_bundle_and_expiry_across_all_public_reads(self):
        for observed,expected in ((self.now+1,True),(self.now+60,False)):
            with patch('time.time',return_value=observed):
                values=self.responses()
                for value in values:self.assertEqual(value['ready'],expected)
                for key in ('ready','external','tls','origin','dns','status'):
                    self.assertEqual(values[0][key],values[1][key]);self.assertEqual(values[1][key],values[2][key])
    def test_revocation_and_known_stopped_service_downgrade_all_reads(self):
        self.f.revoked=True
        self.assertTrue(all(not value['ready'] for value in self.responses()))
        self.f.revoked=False
        self.db.execute("UPDATE managed_services SET state='stopped' WHERE id=?",(self.service['id'],))
        self.assertTrue(all(not value['ready'] for value in self.responses()))
