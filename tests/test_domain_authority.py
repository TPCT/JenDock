import copy
import dataclasses
import json
import time
import unittest
from unittest.mock import patch
import domain_authority
import ingress
import ingress_bindings as bindings
import registry
import managed_services as services
import test_ingress


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_ingress.IngressTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.db = self.fixture.db; self.principal = self.fixture.principal
        result = self.fixture.accept()
        row = self.db.execute('SELECT document FROM ingress_definitions WHERE id=?', (result['route']['definition_id'],)).fetchone()
        self.accepted = json.loads(row['document'])
        self.fixture.definition['service_id'] = 'service-one'
        self.fixture.service['binding']['compose_service'] = 'development-web'
        self.persist()
        self.gateway = {'project':'app','environment':'development','routes':[self.route()]}
        self.db.execute('CREATE TABLE ingress_runtime_intents(project,environment,definition,digest,phase)')
        self.db.execute('INSERT INTO ingress_runtime_intents VALUES(?,?,?,?,?)', ('app','development',registry.canonical(self.gateway),bindings._digest(self.gateway),'verified'))
        bindings.initialize(self.db)
        with registry.transaction(self.db): bindings.seed(self.db,self.gateway)
    def persist(self):
        f=self.fixture; s=f.service; d=f.definition; b=s['binding']
        b['definition_digest']=services.digest(d)
        self.db.execute('INSERT OR IGNORE INTO service_definitions VALUES(?,?,?,?)',(s['definition_id'],registry.canonical(d),services.digest(d),time.time()))
        self.db.execute('INSERT OR REPLACE INTO managed_services VALUES(?,?,?,?,?,?,?,?,?)',(s['id'],'app','development','web',s['definition_id'],'fixture-operation','running',time.time(),time.time()))
        self.db.execute('INSERT OR REPLACE INTO service_bindings VALUES(?,?,?)',(s['id'],registry.canonical(b),time.time()))
    def route(self):
        s=self.fixture.service
        return {'hostname':'dev.example.test','service_id':s['id'],'definition_id':s['definition_id'],'definition_digest':s['binding']['definition_digest'],'container_id':s['binding']['container_id'],'alias':'development-web','http_endpoint':self.fixture.artifact['http_endpoint']}
    def validate(self): return domain_authority.validate_current(self.db,self.principal,self.accepted)
    def replace(self,commit=True):
        snapshot=bindings.capture(self.db,'app','development','service-one')
        self.fixture.artifact=dict(self.fixture.artifact,id='artifact-two',image='sha256:'+'e'*64)
        self.fixture.definition['artifact']=self.fixture.artifact
        self.fixture.definition['artifact_id']='artifact-two'
        self.fixture.service['binding']['image_id']=self.fixture.artifact['image']
        self.fixture.definition['replacement']='new-build'
        self.fixture.service['definition_id']='definition-two'
        self.fixture.service['binding']['container_id']='d'*64
        self.persist()
        if commit:
            with registry.transaction(self.db):bindings.advance(self.db,snapshot,**{k:v for k,v in self.route().items() if k not in ('hostname','service_id')})
    def test_current_and_committed_replacement(self):
        self.assertEqual(self.validate()['container_id'],'c'*64)
        self.replace()
        result=self.validate()
        self.assertEqual(result['container_id'],'d'*64)
        self.assertEqual(self.accepted['container_id'],'c'*64)
    def test_replacement_without_committed_route_rejected(self):
        self.replace(commit=False)
        with self.assertRaises(ValueError):self.validate()
    def test_stable_configuration_changes_rejected(self):
        for field in ('policy_revision','source_revision','integration_revision','integration_observation_digest','origin_revision','http_endpoint','account_id','hostname'):
            with self.subTest(field=field):
                current=ingress._definition(self.db,self.principal,'app','development',{k:self.accepted[k] for k in ingress.FIELDS},'domains:create')
                current[field]='changed'
                with patch.object(ingress,'_definition',return_value=current),self.assertRaises(ValueError):self.validate()
    def test_unregistered_accepted_document_rejected(self):
        self.accepted=dict(self.accepted,container_id='e'*64)
        with self.assertRaises(ValueError):self.validate()
    def test_wrong_hostname_or_missing_runtime_rejected(self):
        self.db.execute("DELETE FROM ingress_runtime_intents")
        with self.assertRaises(ValueError):self.validate()
    def test_revoked_scope_and_pending_gateway_rejected(self):
        self.principal=dataclasses.replace(self.principal,scopes=frozenset({'domains:read'}))
        with self.assertRaises(ValueError):self.validate()
        self.principal=self.fixture.principal
        self.db.execute("UPDATE ingress_runtime_intents SET phase='recovery-required'")
        with self.assertRaises(ValueError):self.validate()
    def test_committed_route_without_current_service_binding_rejected(self):
        self.replace()
        self.db.execute('DELETE FROM service_bindings')
        with self.assertRaises(ValueError):self.validate()

    def test_credential_rechecked(self):
        with patch.object(ingress.ops,'recheck_credential',side_effect=ingress.ops.OperationError('revoked','Revoked.',403)):
            with self.assertRaises(ValueError):self.validate()
    def test_wrong_hostname_route_is_not_authority(self):
        gateway=copy.deepcopy(self.gateway);gateway['routes'][0]['hostname']='other.example.test'
        self.db.execute('UPDATE ingress_runtime_intents SET definition=?,digest=?',(registry.canonical(gateway),bindings._digest(gateway)))
        self.db.execute('UPDATE ingress_binding_sets SET gateway_digest=?',(bindings._digest(gateway),))
        self.db.execute('UPDATE ingress_backend_bindings SET gateway_digest=?',(bindings._digest(gateway),))
        with self.assertRaises(ValueError):self.validate()
    def test_pending_addition_is_not_committed_authority(self):
        import ingress_route_generations as generations
        generations.initialize(self.db)
        self.db.execute('INSERT INTO ingress_route_operations VALUES(?,?,?,?,?)',('pending','app','development','{}','prepared'))
        with self.assertRaises(ValueError):self.validate()
    def test_read_helper_does_not_write_or_change_accepted_document(self):
        before=self.db.total_changes
        self.validate()
        self.assertEqual(before,self.db.total_changes)
        self.replace()
        before=self.db.total_changes
        current=self.validate()
        self.assertEqual(before,self.db.total_changes)
        self.assertNotEqual(current['artifact_id'],self.accepted['artifact_id'])
