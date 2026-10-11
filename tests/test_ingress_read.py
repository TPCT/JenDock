import copy
import dataclasses
import json
import time
import unittest
from unittest.mock import patch
import application_operations as ops
import cloudflare_dns
import ingress
import managed_services as services
import registry
import test_ingress as fixture

class DomainReadTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.IngressTests();self.fixture.setUp();self.db=self.fixture.db;self.principal=self.fixture.principal
        self.catalog=patch.object(services,'list_services',side_effect=lambda *args:[self.fixture.service]);self.catalog.start()
    def tearDown(self):self.catalog.stop();self.fixture.tearDown()
    def options(self,principal=None):return ingress.domain_options(self.db,principal or self.principal,'app','development')
    def accept(self):return self.fixture.accept()
    def test_options_return_only_public_eligible_choices_and_preview_inputs(self):
        options=self.options()
        self.assertEqual(options['services'][0]['id'],'service-one');self.assertEqual(options['zones'][0]['id'],'b'*32)
        self.assertEqual({item['dns_type'] for item in options['origins']},{'A','AAAA','CNAME'})
        self.assertEqual(options['services'][0]['runtime_verification'],'pending')
        serialized=json.dumps(options)
        for private in ('never-read','artifact_id','compose_files','healthcheck','command','recipe','container_id'):self.assertNotIn(private,serialized)
        values=dict(self.fixture.values,service_id=options['services'][0]['id'],zone_id=options['zones'][0]['id'])
        target=next(row for row in options['origins'] if row['dns_type']=='A');values.update(target)
        self.assertEqual(ingress.preview(self.db,self.principal,'app','development',values)['definition']['target'],target['target'])
    def test_stopped_and_forbidden_source_services_are_not_options(self):
        self.fixture.service['state']='stopped';self.assertEqual(self.options()['services'],[])
        self.fixture.service['state']='running';self.fixture.artifact['source_environment']='production'
        self.fixture.service['binding']['definition_digest']=services.digest(self.fixture.definition)
        restricted=dataclasses.replace(self.principal,allowed_environments=frozenset({('app','development')}))
        self.assertEqual(self.options(restricted)['services'],[])
    def test_missing_integration_and_origin_are_explicit_dependencies(self):
        self.db.execute("UPDATE integration_connections SET enabled=0 WHERE provider='cloudflare'")
        with patch.object(ingress,'_origin',side_effect=ops.OperationError('origin_unavailable','Missing origin')):
            options=self.options()
        self.assertEqual(options['zones'],[]);self.assertEqual(options['integration_status'],'unavailable')
        self.assertEqual(options['origins'],[]);self.assertEqual(options['origin_status'],'unavailable')
        self.assertEqual(len(options['services']),1)
    def test_zone_account_and_duplicate_metadata_filtered(self):
        metadata=self.fixture.metadata;metadata['zones'].append(dict(metadata['zones'][0],id='c'*32,account_id='d'*32))
        metadata['zones'].append(dict(metadata['zones'][0]))
        self.db.execute("UPDATE integration_connections SET metadata=? WHERE provider='cloudflare'",(registry.canonical(metadata),))
        self.assertEqual(self.options()['zones'],[])
    def test_scoped_route_and_separate_operation_status_not_ready(self):
        accepted=self.accept();route=ingress.get_route(self.db,self.principal,'app','development',accepted['route']['id'])
        self.assertEqual(route['dns']['status'],'unverified');self.assertFalse(route['ready'])
        operation=ingress.get_operation(self.db,self.principal,'app','development',accepted['operation']['id'])
        self.assertEqual(operation['status'],'pending-provisioning');self.assertFalse(operation['executor_available'])
        self.assertEqual(ingress.list_routes(self.db,self.principal,'app','development')['routes'],[route])
        for target in ('get_route','get_operation'):
            identifier=accepted['route']['id'] if target=='get_route' else accepted['operation']['id']
            with self.assertRaises(ops.OperationError):getattr(ingress,target)(self.db,self.principal,'app','production',identifier)
        with self.assertRaises(ops.OperationError):ingress.get_operation(self.db,self.principal,'app','development','unrelated-app-operation')
    def test_read_scope_and_source_grants_apply_to_lists_and_direct_reads(self):
        self.fixture.artifact['source_environment']='production';self.fixture.service['binding']['definition_digest']=services.digest(self.fixture.definition)
        accepted=self.accept();restricted=dataclasses.replace(self.principal,allowed_environments=frozenset({('app','development')}))
        self.assertEqual(ingress.list_routes(self.db,restricted,'app','development')['routes'],[])
        with self.assertRaises(ops.OperationError):ingress.get_route(self.db,restricted,'app','development',accepted['route']['id'])
        with self.assertRaises(ops.OperationError):self.options(dataclasses.replace(self.principal,scopes=frozenset()))
    def test_route_identity_and_operation_result_corruption_fail_closed(self):
        accepted=self.accept();route_id=accepted['route']['id'];operation_id=accepted['operation']['id']
        self.db.execute("UPDATE ingress_routes SET hostname='other.example.test' WHERE id=?",(route_id,))
        with self.assertRaises(ops.OperationError):ingress.get_route(self.db,self.principal,'app','development',route_id)
        self.db.execute("UPDATE ingress_routes SET hostname='dev.example.test' WHERE id=?",(route_id,))
        altered=copy.deepcopy(accepted);altered['operation']['id']='f'*32
        self.db.execute('UPDATE ingress_operations SET result=? WHERE id=?',(registry.canonical(altered),operation_id))
        with self.assertRaises(ops.OperationError):ingress.get_operation(self.db,self.principal,'app','development',operation_id)
    def test_recorded_dns_is_not_live_origin_or_tls_readiness(self):
        accepted=self.accept();route_id=accepted['route']['id'];cloudflare_dns.initialize(self.db)
        record={'id':'d'*32,'name':'dev.example.test','type':'A','content':'192.0.2.10','ttl':1,'proxied':True,'comment':'deploy-monitor:marker'}
        self.db.execute('INSERT INTO ingress_dns_intents VALUES(?,?,?,?,?,?,?,?)',('dns-operation',route_id,'f'*64,record['comment'],'POST',record['id'],'verified',time.time()))
        self.db.execute('INSERT INTO ingress_dns_ownership VALUES(?,?,?,?,?,?,?)',(route_id,'b'*32,record['id'],record['comment'],'dns-operation',ingress._digest(record),time.time()))
        route=ingress.get_route(self.db,self.principal,'app','development',route_id)
        self.assertEqual(route['dns']['status'],'verified');self.assertEqual(route['dns']['evidence'],'recorded');self.assertFalse(route['dns']['live_checked'])
        self.assertEqual(route['origin']['status'],'unverified');self.assertEqual(route['tls']['status'],'unverified');self.assertFalse(route['ready'])
        self.db.execute("UPDATE ingress_dns_ownership SET record_digest=?",('e'*64,))
        with self.assertRaises(ops.OperationError):ingress.get_route(self.db,self.principal,'app','development',route_id)

    def test_pagination_and_reconnect_preserve_domain_operation_identity(self):
        from store import connect
        first=self.accept()
        self.fixture.values['hostname']='second.example.test'
        second=self.fixture.accept(key='second-domain-request-123')
        page=ingress.list_routes(self.db,self.principal,'app','development',limit=1)
        self.assertEqual(len(page['routes']),1);self.assertIsNotNone(page['next_cursor'])
        next_page=ingress.list_routes(self.db,self.principal,'app','development',limit=1,cursor=page['next_cursor'])
        self.assertEqual({page['routes'][0]['id'],next_page['routes'][0]['id']},{first['route']['id'],second['route']['id']})
        other=connect(self.fixture.fixture.path)
        try:
            status=ingress.get_operation(other,self.principal,'app','development',first['operation']['id'])
            self.assertEqual(status['route_id'],first['route']['id']);self.assertFalse(status['ready'])
        finally:other.close()
