import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch
import domain_external_flow as flow
import ingress_runtime as runtime
import ingress_route_generations as generations
import test_ingress_runtime as fixtures


class ExternalProjectionReviewTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:',isolation_level=None);self.db.row_factory=sqlite3.Row;self.addCleanup(self.db.close)
        self.gateway=fixtures.GatewayConfigurationTests().definition();self.gateway['schema_version']=3
        self.route=self.gateway['routes'][0];self.gid='e'*64
        runtime._save(self.db,self.gateway,Path('/managed'),{'binding':{'container_id':self.gid}},'verified')
        self.db.execute('CREATE TABLE managed_services(id TEXT,project TEXT,environment TEXT,definition_id TEXT,state TEXT)')
        self.db.execute('CREATE TABLE service_bindings(service_id TEXT,document TEXT)')
        self.db.execute('INSERT INTO managed_services VALUES(?,?,?,?,?)',(self.route['service_id'],'app','development',self.route['definition_id'],'running'))
        self.binding={'container_id':self.route['container_id'],'state':'running','compose_service':self.route['alias'],'definition_digest':self.route['definition_digest']}
        self.db.execute('INSERT INTO service_bindings VALUES(?,?)',(self.route['service_id'],json.dumps(self.binding)))
        self.definition=dict(self.route,project='app',environment='development')
        now=time.time();self.journal={'certificate':{'fingerprint':'1'*64,'not_after':now+300},'tls_stage':{'digest':'2'*64},'external_verified':{
            'hostname':self.route['hostname'],'gateway_id':self.gid,'backend_id':self.route['container_id'],
            'proof_digest':hashlib.sha256(runtime.route_proof_token('app','development',self.route).encode()).hexdigest(),
            'tls_fingerprint':'1'*64,'tls_digest':'2'*64,'observed_at':now,'dns_observed_at':now,'origin_observed_at':now,'expires_at':now+60}}
        for name in ('resolve_authority','_resume_authority'):
            mocked=patch('domain_execution.'+name);setattr(self,name,mocked.start());self.addCleanup(mocked.stop)
    def project(self):return flow.projection(self.db,{'id':'operation'},self.definition,self.journal,'ready')
    def test_fresh_evidence_is_ready_without_writes(self):
        statements=[];self.db.set_trace_callback(statements.append)
        self.assertTrue(self.project()[0]);self.assertFalse(any(s.startswith(('INSERT','UPDATE','DELETE','CREATE','BEGIN')) for s in statements))
    def test_expired_or_nonfinite_evidence_is_not_ready(self):
        baseline=copy.deepcopy(self.journal)
        for field,value in [('expires_at',time.time()-1),('expires_at',float('inf')),('observed_at',float('nan')),('expires_at',time.time()+1000)]:
            with self.subTest(field=field,value=value):
                self.journal=copy.deepcopy(baseline);self.journal['external_verified'][field]=value;self.assertFalse(self.project()[0])
    def test_revoked_authority_is_not_ready(self):
        self._resume_authority.side_effect=ValueError('revoked');self.assertFalse(self.project()[0])
    def test_changed_gateway_identity_is_not_ready(self):
        self.db.execute('UPDATE ingress_runtime_intents SET journal=?',(json.dumps({'binding':{'container_id':'f'*64}}),));self.assertFalse(self.project()[0])
    def test_pending_addition_is_not_ready(self):
        self.db.execute('INSERT INTO ingress_route_operations VALUES(?,?,?,?,?)',('addition','app','development','{}','reload-intent'));self.assertFalse(self.project()[0])
    def test_changed_backend_definition_invalidates_same_container(self):
        backend={k:v for k,v in self.route.items() if k!='hostname'};backend['definition_digest']='f'*64
        self.db.execute('UPDATE ingress_backend_bindings SET backend=?',(json.dumps(backend),));self.assertFalse(self.project()[0])
    def test_stopped_binding_is_not_ready(self):
        self.binding['state']='stopped';self.db.execute('UPDATE service_bindings SET document=?',(json.dumps(self.binding),));self.assertFalse(self.project()[0])
    def test_missing_gateway_table_fails_closed(self):
        self.db.execute('DROP TABLE ingress_runtime_intents');self.assertFalse(self.project()[0])
    def test_unrelated_committed_route_does_not_invalidate_selected_proof(self):
        head=generations.head(self.db,self.gateway);peer=dict(self.route,hostname='another.example.test');head['routes'].append(peer)
        self.db.execute('UPDATE ingress_route_heads SET generation=generation+1,routes=?',(json.dumps(head['routes']),));self.assertTrue(self.project()[0])

    def test_background_read_only_recheck_keeps_unexpired_evidence(self):
        self.journal['pending_effect']='external'
        self.assertTrue(flow.projection(self.db,{'id':'operation'},self.definition,self.journal,'running','external')[0])
        self.assertFalse(flow.projection(self.db,{'id':'operation'},self.definition,self.journal,'running','activate-tls')[0])
        self.journal['pending_effect']='activate-tls'
        self.assertFalse(flow.projection(self.db,{'id':'operation'},self.definition,self.journal,'running','external')[0])
