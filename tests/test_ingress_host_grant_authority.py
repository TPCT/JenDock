import json
import time
import unittest
from unittest.mock import patch
import domain_execution
import ingress_bindings
import ingress_host_grants
import managed_services
import registry
import test_ingress_host_grants as fixtures


class HostGrantAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.HostGrantTests();self.f.setUp();self.addCleanup(self.f.tearDown)
    def test_initial_provider_revision_change_denies_without_policy_write(self):
        self.f.db.execute("UPDATE integration_connections SET revision=revision+1 WHERE provider='cloudflare'")
        with self.assertRaises(ValueError):self.f.issuer.issue(self.f.request)
        self.assertEqual(self.f.policy()['routes'],{})
        self.assertEqual(self.f.observations,0)
    def test_postflight_provider_change_does_not_publish_grant(self):
        original=self.f.issuer.verify_gateway
        def changed(*args,**kwargs):
            result=original(*args,**kwargs)
            self.f.db.execute("UPDATE integration_connections SET revision=revision+1 WHERE provider='cloudflare'")
            return result
        self.f.issuer.verify_gateway=changed
        with self.assertRaises(ValueError):self.f.issuer.issue(self.f.request)
        self.assertEqual(self.f.policy()['routes'],{})
    def replacement(self):
        f=self.f;d=f.domain.definition;s=f.domain.service
        s['definition_id']='replacement-definition';s['binding'].update(container_id='f'*64,compose_service='development-web')
        d['replacement']='changed';s['binding']['definition_digest']=managed_services.digest(d)
        db=f.db;now=time.time()
        db.execute('INSERT INTO service_definitions VALUES(?,?,?,?)',(s['definition_id'],registry.canonical(d),managed_services.digest(d),now))
        db.execute('INSERT INTO managed_services VALUES(?,?,?,?,?,?,?,?,?)',(s['id'],'app','development','web',s['definition_id'],f.operation,'running',now,now))
        db.execute('INSERT INTO service_bindings VALUES(?,?,?)',(s['id'],registry.canonical(s['binding']),now))
        backend=dict(f.gateway['routes'][0]);backend.pop('hostname');backend.update(definition_id=s['definition_id'],definition_digest=s['binding']['definition_digest'],container_id='f'*64)
        db.execute('UPDATE ingress_backend_bindings SET generation=generation+1,backend=?',(registry.canonical(backend),))
    def observation(self,phase='external'):
        f=self.f;domain_execution.enqueue(f.db,f.operation)
        cert={'route_id':f.route,'operation_id':f.operation,'generation':'1'*64,'fingerprint':'2'*64,'status':'verified','sans':['dev.example.test'],'not_after':time.time()+3600}
        stage={k:cert[k] for k in ('route_id','operation_id','generation','fingerprint')};stage.update(status='active',digest='3'*64)
        verified={k:cert[k] for k in ('generation','fingerprint','not_after')};verified.update(digest='3'*64,observed=time.time())
        f.db.execute('UPDATE domain_executions SET phase=?,journal=? WHERE operation_id=?',(phase,registry.canonical({'certificate':cert,'tls_stage':stage,'tls_verified':verified}),f.operation))
    def test_committed_observation_allows_current_backend_without_changing_grant(self):
        original=self.f.issuer.issue(self.f.request)
        self.observation();self.replacement()
        result=self.f.issuer.issue(self.f.request)
        self.assertEqual(result,original)
        self.assertEqual(self.f.policy()['routes'][self.f.route],original['grant'])
    def test_changed_backend_without_observation_or_with_wrong_phase_denied(self):
        self.replacement()
        with self.assertRaises(ValueError):self.f.issuer.issue(self.f.request)
        self.observation('activate-tls')
        with self.assertRaises(ValueError):self.f.issuer.issue(self.f.request)
        self.assertEqual(self.f.policy()['routes'],{})
    def test_observation_must_belong_to_original_definition(self):
        self.observation();self.replacement()
        self.f.db.execute("UPDATE domain_executions SET definition_digest=?",('9'*64,))
        with self.assertRaises(ValueError):self.f.issuer.issue(self.f.request)
