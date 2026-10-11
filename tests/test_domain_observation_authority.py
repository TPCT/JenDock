import json
import time
import unittest
from unittest.mock import patch
import domain_execution as execution
import registry
import test_domain_authority


class ObservationAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_domain_authority.AuthorityTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db=self.fixture.db;self.principal=self.fixture.principal
        self.operation=self.db.execute('SELECT * FROM ingress_operations').fetchone()
        execution.enqueue(self.db,self.operation['id'])
        self.claim=execution.claim_next(self.db,'observation-worker')
    def resolver(self,db,operation):return self.principal
    def late(self,phase='external'):
        cert={'route_id':self.operation['route_id'],'operation_id':self.operation['id'],'status':'verified','sans':['dev.example.test'],
              'generation':'a'*64,'fingerprint':'b'*64,'not_after':time.time()+3600}
        stage=dict(cert,status='staged',digest='c'*64)
        journal={'certificate':cert,'tls_stage':stage,'tls_verified':{'digest':'c'*64,'generation':'a'*64,'fingerprint':'b'*64,'not_after':cert['not_after'],'observed':time.time()}}
        self.db.execute('UPDATE domain_executions SET phase=?,journal=?',(phase,registry.canonical(journal)))
    def authorize(self):return execution.authorize_claim(self.db,self.claim,resolve_principal=self.resolver)
    def test_committed_replacement_allowed_for_post_tls_observation(self):
        self.late();self.fixture.replace()
        self.assertEqual(self.authorize()['container_id'],'d'*64)
        operation,current=execution._resume_authority(self.db,self.principal,'app','development',self.operation['id'],'domains:create')
        self.assertEqual(current['container_id'],'d'*64)
    def test_initial_and_activation_recovery_keep_original_exact_fence(self):
        self.fixture.replace()
        for phase in ('dns-preview','gateway','grant','stage','activate','dns','awaiting-tls','certificate','stage-tls','activate-tls'):
            with self.subTest(phase=phase):
                self.late(phase)
                with self.assertRaises(ValueError):self.authorize()
    def test_phase_alone_without_committed_tls_is_insufficient(self):
        self.fixture.replace();self.db.execute("UPDATE domain_executions SET phase='external',journal='{}'")
        with self.assertRaises(ValueError):self.authorize()
    def test_uncommitted_backend_or_stale_lease_rejected(self):
        self.late();self.fixture.replace(commit=False)
        with self.assertRaises(ValueError):self.authorize()
        self.db.execute('UPDATE domain_executions SET lease_generation=lease_generation+1')
        with self.assertRaises(ValueError):self.authorize()
    def test_latest_backend_does_not_rewrite_accepted_identity(self):
        self.late();self.fixture.replace();self.authorize()
        _,accepted=execution._definition(self.db,self.operation)
        self.assertEqual(accepted['container_id'],'c'*64)
        row=execution._row(self.db,self.operation['id'])
        self.assertEqual(row['definition_digest'],execution._definition(self.db,self.operation)[0]['digest'])
