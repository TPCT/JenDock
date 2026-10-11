import copy
import dataclasses
import json
import os
import time
import unittest
from unittest.mock import patch
import application_operations as ops
import ingress
import managed_services as services
import registry
import test_managed_services as fixtures

class IngressTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ManagedServiceTests();self.fixture.setUp();self.db=self.fixture.db
        self.principal=dataclasses.replace(self.fixture.principal,scopes=frozenset({'domains:read','domains:create'}))
        self.artifact=dict(self.fixture.artifact,http_endpoint={'port':8080,'health_path':'/ready'})
        self.definition={'artifact_id':self.artifact['id'],'artifact':self.artifact,'project':'app','environment':'development','service_id':'service-one','name':'web'}
        self.service={'id':'service-one','project':'app','environment':'development','state':'running','definition_id':'definition-one','definition':self.definition,
                      'binding':{'container_id':'c'*64,'state':'running','definition_digest':services.digest(self.definition),'image_id':self.artifact['image'],'observed':time.time()}}
        self.service_patch=patch.object(services,'get',side_effect=lambda *args:self.service);self.service_patch.start()
        self.artifact_patch=patch.object(services,'artifact_for_service',side_effect=lambda *args:self.artifact);self.artifact_patch.start()
        self.origin=patch.dict(os.environ,{'INGRESS_ORIGIN_IPV4':'192.0.2.10','INGRESS_ORIGIN_IPV6':'2001:db8::10','INGRESS_ORIGIN_HOSTNAME':'origin.example.test'});self.origin.start()
        ingress.initialize(self.db)
        self.metadata={'api_access':'verified','zone_read':'verified','token_kind':'account','account_id':'a'*32,'zones':[{'id':'b'*32,'name':'example.test','status':'active','account_id':'a'*32}]}
        self.db.execute('INSERT INTO integration_connections VALUES(?,?,?,?,?,?,?,?,?)',('cloudflare',1,b'never-read',1,registry.canonical(self.metadata),'{}',time.time(),time.time(),'fixture'))
        self.values={'service_id':'service-one','hostname':'Dev.Example.Test.','zone_id':'b'*32,'dns_type':'A','target':'192.0.2.10','proxied':True,'ttl':1}
    def tearDown(self):
        self.service_patch.stop();self.artifact_patch.stop();self.origin.stop();self.fixture.tearDown()
    def preview(self,**changes):return ingress.preview(self.db,self.principal,'app','development',dict(self.values,**changes))
    def accepted_values(self):
        preview=self.preview();return dict(self.values,**{key:preview[key] for key in ingress.PINS})
    def accept(self,values=None,key='domain-request-123456',principal=None):return ingress.accept(self.db,principal or self.principal,'app','development',values or self.accepted_values(),key)
    def test_preview_accept_replay_is_immutable_and_truthful(self):
        preview=self.preview();self.assertFalse(preview['dns_live_checked']);self.assertFalse(preview['ready'])
        self.assertEqual(preview['definition']['hostname'],'dev.example.test')
        values=self.accepted_values();result=self.accept(values)
        self.assertEqual(self.accept(values),result);self.assertEqual(result['operation']['status'],'pending-provisioning');self.assertFalse(result['operation']['executor_available'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM ingress_routes').fetchone()[0],1)
        with self.assertRaises(Exception):self.db.execute("UPDATE ingress_definitions SET digest='changed'")
        with self.assertRaises(ops.OperationError):self.accept(dict(values,hostname='other.example.test'))
        self.assertNotIn('never-read',json.dumps(result))
    def test_global_hostname_collision_and_stale_pins(self):
        values=self.accepted_values();self.accept(values)
        with self.assertRaisesRegex(ops.OperationError,'reserved'):self.accept(values,key='another-request-12345')
        with self.assertRaisesRegex(ops.OperationError,'reserved'):self.preview()
    def test_same_revision_integration_metadata_and_origin_change_stale_preview(self):
        values=self.accepted_values();self.metadata['zones'][0]['account_id']='d'*32;self.metadata['account_id']='d'*32
        self.db.execute("UPDATE integration_connections SET metadata=? WHERE provider='cloudflare'",(registry.canonical(self.metadata),))
        with self.assertRaisesRegex(ops.OperationError,'changed'):self.accept(values)
        values=self.accepted_values()
        with patch.dict(os.environ,{'INGRESS_ORIGIN_IPV6':'2001:db8::11'}),self.assertRaisesRegex(ops.OperationError,'changed'):self.accept(values)
    def test_stopped_unverified_missing_endpoint_and_sibling_rejected(self):
        for change in ({'state':'stopped'},{'environment':'production'},{'binding':None}):
            old=copy.deepcopy(self.service);self.service.update(change)
            with self.assertRaises(ValueError):self.preview()
            self.service=old
        self.service['binding']['observed']=time.time()-100
        self.assertEqual(self.preview()['runtime_verification'],'pending')
        self.service['binding']['observed']=time.time();self.artifact.pop('http_endpoint')
        with self.assertRaises(ValueError):self.preview()
    def test_scope_environment_repository_and_replay_authorization(self):
        values=self.accepted_values();self.accept(values)
        for principal in (dataclasses.replace(self.principal,scopes=frozenset({'domains:read'})),dataclasses.replace(self.principal,allowed_environments=frozenset({('app','production')})),dataclasses.replace(self.principal,allowed_repository_ids=frozenset({99}))):
            with self.assertRaises(ops.OperationError):self.accept(values,principal=principal)
    def test_host_zone_target_and_dns_schema(self):
        for change in ({'hostname':'evilexample.test'},{'hostname':'https://dev.example.test'},{'hostname':'*.example.test'},{'target':'192.0.2.99'},{'target':'http://192.0.2.10'},{'ttl':300},{'dns_type':'TXT'},{'adopt':True}):
            with self.subTest(change=change),self.assertRaises(ValueError):self.preview(**change)
        self.assertEqual(self.preview(dns_type='AAAA',target='2001:db8:0:0::10')['definition']['target'],'2001:db8::10')
        self.assertEqual(self.preview(dns_type='CNAME',target='ORIGIN.EXAMPLE.TEST.')['definition']['target'],'origin.example.test')
        self.metadata['account_id']='d'*32
        self.db.execute("UPDATE integration_connections SET metadata=? WHERE provider='cloudflare'",(registry.canonical(self.metadata),))
        with self.assertRaises(ValueError):self.preview()

    def test_preview_pin_types_and_source_grants_on_replay(self):
        values=self.accepted_values()
        for key,value in (('expected_project_revision',True),('expected_environment_revision',1.0),('expected_source_revision',{}),('preview_fingerprint','bad'),('expected_origin_revision',None)):
            with self.subTest(key=key),self.assertRaises(ops.OperationError):self.accept(dict(values,**{key:value}))
        self.artifact['source_environment']='production';self.service['definition']['artifact']=self.artifact
        self.service['binding']['definition_digest']=services.digest(self.definition)
        values=self.accepted_values();self.accept(values)
        restricted=dataclasses.replace(self.principal,allowed_environments=frozenset({('app','development')}))
        with self.assertRaises(ops.OperationError):self.accept(values,principal=restricted)

    def test_revocation_rechecked_inside_acceptance_transaction(self):
        values=self.accepted_values()
        def recheck(db,principal):
            if db.in_transaction:raise ops.OperationError('revoked','Credential revoked.',403)
        with patch.object(ops,'recheck_credential',side_effect=recheck),self.assertRaises(ops.OperationError):self.accept(values)
        self.assertEqual(self.db.execute('SELECT count(*) FROM ingress_routes').fetchone()[0],0)
