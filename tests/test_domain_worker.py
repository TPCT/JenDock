import json
import sqlite3
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import domain_worker
import ingress_runtime
import ingress_route_generations
import test_ingress_runtime as fixtures


class DomainGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.db=sqlite3.connect(':memory:',isolation_level=None);self.db.row_factory=sqlite3.Row;self.addCleanup(self.db.close)
        ingress_runtime.initialize(self.db)
        self.definition=fixtures.GatewayConfigurationTests().definition()
        self.domain={'project':'app','environment':'development','hostname':'second.example.test','service_id':'service-id','http_endpoint':{'port':8080,'health_path':'/ready'},'policy_revision':0}
        self.client=Mock()
        self.gateway=domain_worker.Gateway(root=self.root,docker='docker',compose='compose',host_client=self.client,image=self.definition['image'],resources=self.definition['resources'])

    def test_pending_addition_reconciles_before_old_head_verification(self):
        d=self.definition
        self.db.execute('INSERT INTO ingress_runtime_intents VALUES(?,?,?,?,?,?,?,?,?)',('app','development',d['operation_id'],json.dumps(d),ingress_runtime.digest(d),str(self.root),'{}','verified',time.time()))
        events=[]
        route=dict(d['routes'][0],hostname=self.domain['hostname'])
        binding={'container_id':'d'*64,'port':19080}
        def verify(*args,**kwargs):
            self.assertIn('recover',events);events.append('verify');return binding
        with patch.object(ingress_route_generations,'operation',return_value={'phase':'reload-intent'}), \
             patch.object(ingress_runtime,'prepare_route_addition',side_effect=lambda *a,**k:events.append('prepare')), \
             patch.object(ingress_runtime,'recover_route_addition',side_effect=lambda *a,**k:events.append('recover')), \
             patch.object(ingress_runtime,'verify_gateway',side_effect=verify), \
             patch('ingress_bindings.current_routes',return_value=[route]), \
             patch.object(ingress_runtime,'_wait_http'):
            result=self.gateway(self.db,self.domain,'domain-operation-1234',authorize=lambda:None,reconcile=True)
        self.assertEqual(events[:2],['prepare','recover']);self.assertTrue(result['http_verified'])
        self.client.call.assert_not_called()

    def test_stale_host_port_evidence_never_reserves_or_creates(self):
        self.client.call.return_value={'ports':[19080],'observed':time.time()-60}
        with patch('ingress_capacity.reserve') as reserve, patch.object(ingress_runtime,'apply_gateway') as apply:
            with self.assertRaisesRegex(ValueError,'Fresh host port'):self.gateway(self.db,self.domain,'domain-operation-1234',authorize=lambda:None)
            reserve.assert_not_called();apply.assert_not_called()
        self.client.call.assert_called_once_with('available_ports',{})

    def test_host_namespace_ports_used_without_container_bind_probe(self):
        self.client.call.return_value={'ports':[19080,19082],'observed':time.time()}
        def reserve(*args,**kwargs):
            self.assertTrue(kwargs['available'](19080));self.assertFalse(kwargs['available'](19081))
        with patch('ingress_capacity.reserve'), patch('ingress_ports.reserve',side_effect=reserve), \
             patch('ingress_ports.loopback_available',side_effect=AssertionError('container namespace probe')), \
             patch.object(ingress_runtime,'prepare_gateway',return_value=self.definition), \
             patch.object(ingress_runtime,'apply_gateway',return_value={'container_id':'d'*64}) as apply:
            self.gateway(self.db,self.domain,'domain-operation-1234',authorize=lambda:None)
            self.assertEqual(apply.call_count,1)


    def test_aborted_addition_requires_explicit_reconcile_and_revalidates_request(self):
        d=self.definition
        self.db.execute('INSERT INTO ingress_runtime_intents VALUES(?,?,?,?,?,?,?,?,?)',('app','development',d['operation_id'],json.dumps(d),ingress_runtime.digest(d),str(self.root),'{}','verified',time.time()))
        route=dict(d['routes'][0],hostname=self.domain['hostname'])
        events=[];binding={'container_id':'d'*64,'port':19080}
        with patch.object(ingress_route_generations,'operation',return_value={'phase':'aborted'}), \
             patch.object(ingress_runtime,'prepare_route_addition',side_effect=lambda *a,**k:events.append('validate')), \
             patch.object(ingress_runtime,'retry_aborted_route_addition',side_effect=lambda *a,**k:events.append('retry')), \
             patch.object(ingress_runtime,'apply_route_addition',side_effect=lambda *a,**k:events.append('apply')), \
             patch.object(ingress_runtime,'verify_gateway',return_value=binding), \
             patch('ingress_bindings.current_routes',return_value=[route]), \
             patch.object(ingress_runtime,'_wait_http'):
            with self.assertRaisesRegex(ValueError,'explicit domain resume'):
                self.gateway(self.db,self.domain,'domain-operation-1234',authorize=lambda:None)
            self.assertEqual(events,[])
            result=self.gateway(self.db,self.domain,'domain-operation-1234',authorize=lambda:None,reconcile=True)
        self.assertEqual(events,['validate','retry','apply']);self.assertTrue(result['http_verified'])
