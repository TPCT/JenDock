import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import ingress_runtime as runtime
import ingress_bindings as bindings
import ingress_route_generations as generations
import test_ingress_runtime as fixtures


class ProofRefreshTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.db=sqlite3.connect(':memory:',isolation_level=None);self.db.row_factory=sqlite3.Row;self.addCleanup(self.db.close)
        self.d=fixtures.GatewayConfigurationTests().definition();self.d['schema_version']=3
        self.d['routes'].append(dict(self.d['routes'][0],hostname='peer.example.test',service_id='peer-service',definition_id='peer-definition',container_id='9'*64))
        self.binding={'container_id':'e'*64,'transport_network_id':'f'*64}
        _,self.config,_=runtime._paths(self.root,self.d,create=True)
        runtime._save(self.db,self.d,self.root,{'container_id':'e'*64,'transport_id':'f'*64},'verified')
        self.config.write_text(runtime._effective_config(self.db,self.d));self.original=self.config.read_text()
        self.before=generations.head(self.db,self.d);self.operation='proof-refresh-operation-12345'
        # Represents committed application binding metadata; refresh itself may not alter it.
        row=self.db.execute("SELECT backend FROM ingress_backend_bindings WHERE service_id='service-id'").fetchone()
        backend=json.loads(row['backend']);backend.update(container_id='8'*64,definition_id='next-definition',definition_digest='7'*64)
        self.db.execute("UPDATE ingress_backend_bindings SET backend=?,generation=generation+1 WHERE service_id='service-id'",(json.dumps(backend),))
        self.bindings_before=[tuple(r) for r in self.db.execute('SELECT * FROM ingress_backend_bindings ORDER BY service_id')]
        self.commands=[];self.token=generations.token(self.d,self.before)
        def backend(db,project,environment,service,docker):
            route=next(r for r in bindings.current_routes(db,self.d) if r['service_id']==service)
            return {k:v for k,v in route.items() if k!='hostname'},{'Id':self.d['application_network_id']}
        def command(args,**kwargs):
            self.commands.append(args)
            if '-s' in args:self.token=generations.token(self.d,generations.operation(self.db,self.operation)['document']['after'])
            return ''
        self.command=command
        def probe(docker,cid,token):
            if token!=self.token:raise ValueError('unobserved generation')
        for name,options in (('_verify_gateway',{'return_value':self.binding}),('_verify_generation',{'side_effect':probe}),('_backend',{'side_effect':backend}),('_command',{'side_effect':command}),('_wait_http',{'return_value':None})):
            p=patch.object(runtime,name,**options);p.start();self.addCleanup(p.stop)
    def prepare(self):return runtime.prepare_gateway_proof_refresh(self.db,self.d,self.operation,root=self.root,docker='docker',authorize=lambda:None)
    def apply(self):return runtime.refresh_gateway_proof(self.db,self.d,self.operation,root=self.root,docker='docker',authorize=lambda:None)
    def recover(self):return runtime.recover_gateway_proof(self.db,self.d,self.operation,root=self.root,docker='docker',authorize=lambda:None)
    def signals(self):return sum('-s' in c for c in self.commands)
    def test_refresh_preserves_peer_and_binding_metadata_and_replay(self):
        self.apply();after=generations.head(self.db,self.d)
        self.assertEqual(after['generation'],2);self.assertEqual(after['routes'][1],self.before['routes'][1])
        self.assertNotEqual(runtime.route_proof_token('app','development',after['routes'][0]),runtime.route_proof_token('app','development',self.before['routes'][0]))
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT * FROM ingress_backend_bindings ORDER BY service_id')],self.bindings_before)
        self.apply();self.assertEqual(self.signals(),1)
    def test_lost_reload_recovers_without_second_signal(self):
        def lost(args,**kwargs):
            result=self.command(args,**kwargs)
            if '-s' in args:raise TimeoutError()
            return result
        with patch.object(runtime,'_command',side_effect=lost),self.assertRaises(TimeoutError):self.apply()
        self.assertEqual(generations.operation(self.db,self.operation)['phase'],'reload-intent')
        self.recover();self.assertEqual(self.signals(),1)
    def test_invalid_candidate_restores_exact_old_config(self):
        with patch.object(runtime,'_command',side_effect=ValueError('configtest')),self.assertRaises(ValueError):self.apply()
        self.assertEqual(self.config.read_text(),self.original);self.assertEqual(self.signals(),0)
        self.assertEqual(generations.head(self.db,self.d),self.before)
    def test_addition_api_cannot_consume_refresh_intent(self):
        self.prepare()
        with self.assertRaises(ValueError):runtime.apply_route_addition(self.db,self.d,self.operation,root=self.root,docker='docker',authorize=lambda:None)
        self.assertEqual(self.signals(),0)
    def test_legacy_gateway_requires_explicit_upgrade(self):
        for version in (1,2):
            d=copy.deepcopy(self.d);d['schema_version']=version
            with self.assertRaises(ValueError):runtime.refresh_gateway_proof(self.db,d,self.operation,root=self.root,docker='docker',authorize=lambda:None)
    def test_changed_backend_after_prepare_fails_before_reload(self):
        self.prepare();self.db.execute("UPDATE ingress_backend_bindings SET generation=generation+1,backend=replace(backend,?,?) WHERE service_id='service-id'",('8'*64,'6'*64))
        with self.assertRaises(ValueError):self.recover()
        self.assertEqual(self.signals(),0);self.assertEqual(self.config.read_text(),self.original)
    def test_no_change_records_noop_without_reload(self):
        self.apply();self.commands.clear();self.operation='proof-refresh-no-change-12345'
        self.apply();self.assertEqual(self.signals(),0);self.assertEqual(generations.head(self.db,self.d)['generation'],2)
        self.assertEqual(generations.operation(self.db,self.operation)['phase'],'committed')
