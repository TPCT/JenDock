import copy
import dataclasses
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import ingress_bindings
import ingress_host_grants as grants
import registry
import test_ingress as domain_fixture
from test_ingress_host import make_host

class HostGrantTests(unittest.TestCase):
    def setUp(self):
        self.domain=domain_fixture.IngressTests();self.domain.setUp();self.db=self.domain.db
        accepted=self.domain.accept();self.route=accepted['route']['id'];self.operation=accepted['operation']['id']
        self.temp=tempfile.TemporaryDirectory();self.adapter,self.executor,self.root=make_host(Path(self.temp.name))
        policy=json.loads((self.root/'policy.json').read_text());policy['routes']={};(self.root/'policy.json').write_text(json.dumps(policy))
        self.request={'route_id':self.route,'operation_id':self.operation,'expected_revision':0}
        self.gateway={'project':'app','environment':'development','operation_id':'gateway-operation-12345','port':31001,'application_network_id':'e'*64,
            'routes':[{'hostname':'dev.example.test','service_id':'service-one','definition_id':'definition-one','definition_digest':'d'*64,'container_id':'c'*64,'alias':'development-web','http_endpoint':{'port':8080,'health_path':'/ready'}}]}
        self.db.execute('CREATE TABLE ingress_runtime_intents(project,environment,definition,digest,journal,root,phase)')
        self.db.execute('INSERT INTO ingress_runtime_intents VALUES(?,?,?,?,?,?,?)',('app','development',registry.canonical(self.gateway),grants._digest(self.gateway),json.dumps({'container_id':'a'*64,'transport_id':'b'*64,'binding':{'container_id':'a'*64}}),'/managed','verified'))
        self.db.execute('CREATE TABLE ingress_ports(project,environment,operation_id,port,container_id)')
        self.db.execute('INSERT INTO ingress_ports VALUES(?,?,?,?,?)',('app','development','gateway-operation-12345',31001,'a'*64))
        self.db.execute('INSERT INTO ingress_capacity(project,environment,operation_id,container_id,state,memory_mib,cpu,created) VALUES(?,?,?,?,?,64,.25,0)',('app','development','gateway-operation-12345','a'*64,'bound'))
        ingress_bindings.initialize(self.db)
        with registry.transaction(self.db):ingress_bindings.seed(self.db,self.gateway)
        self.principal=self.domain.principal;self.observations=0
        self.binding={'container_id':'a'*64,'port':31001,'definition_digest':grants._digest(self.gateway),'application_network_id':'e'*64,'transport_network_id':'b'*64,'compose_project':'app','observed':time.time(),'http_verified':True}
        self.issuer=grants.HostGrantIssuer(self.adapter,self.db,verify_gateway=self.verify,resolve_principal=lambda db,operation:self.principal)
    def tearDown(self):self.temp.cleanup();self.domain.tearDown()
    def verify(self,db,definition,*,root,authorize):
        authorize();self.observations+=1;return copy.deepcopy(self.binding)
    def policy(self):return json.loads((self.root/'policy.json').read_text())
    def test_issue_derives_grant_and_replay_freshly_verifies(self):
        result=self.issuer.issue(self.request)
        self.assertEqual(result['grant'],{'hostname':'dev.example.test','zone':'example.test','port':31001,'revision':1});self.assertFalse(result['ready'])
        self.assertEqual(self.issuer.issue(self.request),result);self.assertEqual(self.observations,2)
        self.assertEqual(self.policy()['routes'][self.route],result['grant']);self.assertEqual(self.executor.calls,[])
    def test_caller_cannot_supply_hostname_port_or_wrong_operation(self):
        for change in ({'port':31002},{'hostname':'evil.test'},{'operation_id':'f'*32},{'expected_revision':True},{'expected_revision':1}):
            with self.subTest(change=change),self.assertRaises(ValueError):self.issuer.issue(dict(self.request,**change))
        self.assertEqual(self.policy()['routes'],{})
    def test_revoked_scope_and_mismatched_live_gateway_fail(self):
        original=self.principal;self.principal=dataclasses.replace(original,allowed_environments=frozenset({('app','production')}))
        with self.assertRaises(ValueError):self.issuer.issue(self.request)
        self.principal=original
        for key,value in (('container_id','f'*64),('compose_project','other'),('observed',time.time()-60),('port',31002)):
            old=self.binding[key];self.binding[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):self.issuer.issue(self.request)
            self.binding[key]=old
        self.assertEqual(self.policy()['routes'],{})
    def test_foreign_policy_hostname_port_and_effective_vhost_preserved(self):
        policy=self.policy()
        for field,value in (('hostname','dev.example.test'),('port',31001)):
            other={'hostname':'peer.example.test','zone':'example.test','port':31002,'revision':1};other[field]=value
            policy['routes']={'f'*32:other};(self.root/'policy.json').write_text(json.dumps(policy))
            before=(self.root/'policy.json').read_bytes()
            with self.assertRaises(ValueError):self.issuer.issue(self.request)
            self.assertEqual((self.root/'policy.json').read_bytes(),before)
        policy['routes']={};(self.root/'policy.json').write_text(json.dumps(policy));self.executor.names=[{'names':['*.example.test'],'path':'/foreign.conf'}]
        with self.assertRaises(ValueError):self.issuer.issue(self.request)
    def test_lost_policy_write_reply_replays_without_replacing_other_grants(self):
        original=self.adapter._write
        def lost(path,text):
            original(path,text)
            if path==self.adapter.policy_path:raise RuntimeError('lost reply')
        with patch.object(self.adapter,'_write',side_effect=lost),self.assertRaises(RuntimeError):self.issuer.issue(self.request)
        self.assertEqual(self.issuer.issue(self.request)['status'],'granted')
        self.assertEqual(self.policy()['routes'][self.route]['revision'],1)
    def test_capacity_and_cross_environment_gateway_rejected(self):
        self.db.execute("UPDATE ingress_capacity SET container_id=?",('f'*64,))
        with self.assertRaises(ValueError):self.issuer.issue(self.request)
        self.db.execute("UPDATE ingress_capacity SET container_id=?",('a'*64,))
        self.gateway['environment']='production'
        self.db.execute('UPDATE ingress_runtime_intents SET definition=?,digest=?',(registry.canonical(self.gateway),grants._digest(self.gateway)))
        with self.assertRaises(ValueError):self.issuer.issue(self.request)

    def test_http_evidence_and_unverified_runtime_phase_rejected(self):
        self.binding.pop('http_verified')
        with self.assertRaises(ValueError):self.issuer.issue(self.request)
        self.binding['http_verified']=True
        self.db.execute("UPDATE ingress_runtime_intents SET phase='recovery-required'")
        with self.assertRaises(ValueError):self.issuer.issue(self.request)
        self.assertEqual(self.policy()['routes'],{})

    def test_active_grant_cannot_be_changed_by_new_operation(self):
        self.issuer.issue(self.request)
        operation=dict(self.db.execute('SELECT * FROM ingress_operations WHERE id=?',(self.operation,)).fetchone())
        operation['id']='f'*32;operation['request_key']='other-request-123456'
        self.db.execute('INSERT INTO ingress_operations VALUES(?,?,?,?,?,?,?,?,?)',tuple(operation.values()))
        _,state=self.adapter._paths(self.policy(),self.route)
        self.adapter._save(state,{'status':'active'})
        before=(self.root/'policy.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'activation'):self.issuer.issue(dict(self.request,operation_id='f'*32,expected_revision=1))
        self.assertEqual((self.root/'policy.json').read_bytes(),before)

    def test_authority_revoked_after_gateway_observation_blocks_publish(self):
        original=self.issuer.verify_gateway
        def revoke(*args,**kwargs):
            result=original(*args,**kwargs)
            self.principal=dataclasses.replace(self.principal,scopes=frozenset())
            return result
        self.issuer.verify_gateway=revoke
        with self.assertRaises(ValueError):self.issuer.issue(self.request)
        self.assertEqual(self.policy()['routes'],{})

    def test_preserves_unrelated_policy_grants(self):
        policy=self.policy();policy['routes']['f'*32]={'hostname':'peer.example.test','zone':'example.test','port':31002,'revision':9}
        (self.root/'policy.json').write_text(json.dumps(policy))
        self.issuer.issue(self.request)
        self.assertEqual(self.policy()['routes']['f'*32],policy['routes']['f'*32])

    def test_concrete_verifier_checks_identity_before_and_after_http(self):
        import ingress_runtime
        calls=[]
        def verify(*args,**kwargs):
            self.assertTrue(kwargs['check_backends']);calls.append('verify');return dict(self.binding)
        def probe(*args):calls.append('http')
        verifier=grants.DockerGatewayVerifier('/usr/bin/docker')
        with patch.object(ingress_runtime,'verify_gateway',side_effect=verify),patch.object(ingress_runtime,'_wait_http',side_effect=probe):
            self.assertTrue(verifier(self.db,self.gateway,root='/managed',authorize=lambda:None)['http_verified'])
        self.assertEqual(calls,['verify','http','verify'])

    def test_concrete_verifier_refuses_http_failure_or_changed_identity(self):
        import ingress_runtime
        verifier=grants.DockerGatewayVerifier('/usr/bin/docker')
        with patch.object(ingress_runtime,'verify_gateway',return_value=dict(self.binding)),patch.object(ingress_runtime,'_wait_http',side_effect=ValueError('HTTP failed')):
            with self.assertRaises(ValueError):verifier(self.db,self.gateway,root='/managed',authorize=lambda:None)
        with patch.object(ingress_runtime,'verify_gateway',side_effect=[dict(self.binding),dict(self.binding,container_id='f'*64)]),patch.object(ingress_runtime,'_wait_http'):
            with self.assertRaises(ValueError):verifier(self.db,self.gateway,root='/managed',authorize=lambda:None)
        with self.assertRaises(ValueError):grants.DockerGatewayVerifier('docker')
