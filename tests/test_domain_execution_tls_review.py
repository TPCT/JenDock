"""Orchestrator contract against actual host HTTP/TLS adapters, no issuance effects."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
import ingress_host
import ingress_host_tls
import domain_execution
import test_domain_execution_tls as execution_fixtures
import test_ingress_host as host_fixtures


class DomainTLSContractReviewTests(unittest.TestCase):
    def setUp(self):
        self.f=execution_fixtures.DomainTLSTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        for name in ('managed','state','challenge'):(self.root/name).mkdir(mode=0o700)
        route=self.f.fixture.route;operation=self.f.operation
        policy={'managed_dir':str(self.root/'managed'),'state_dir':str(self.root/'state'),'listener':{'address':'127.0.0.1','port':8080},
                'routes':{route:{'hostname':'dev.example.test','zone':'example.test','port':31001,'revision':1}}}
        path=self.root/'policy.json';path.write_text(json.dumps(policy));path.chmod(0o600)
        self.executor=host_fixtures.Executor();self.executor.observe_tls=Mock(return_value=True);self.executor.verify_challenge=Mock(return_value=True)
        self.http=ingress_host.HostAdapter(path,self.executor,owner_uid=os.getuid(),challenge_root=self.root/'challenge')
        self.cert=Mock();self.cert.paths={'challenge_root':self.root/'challenge'}
        metadata={'route_id':route,'operation_id':operation,'hostname':'dev.example.test','generation':'1'*64,'fingerprint':'2'*64,'status':'verified','sans':['dev.example.test'],'not_after':time.time()+3600}
        files={}
        for name in ('fullchain','privkey'):
            path=self.root/(name+'.pem');path.write_text('opaque test certificate capability');path.chmod(0o600);files[name]=str(path)
        self.cert.generation_files.return_value=dict(metadata,**files)
        def issue(*args,**kwargs):kwargs['authorize']();return dict(metadata)
        self.cert.request.side_effect=issue
        self.tls=ingress_host_tls.HostTLSAdapter(self.http,self.cert,listener={'address':'127.0.0.1','port':8443},authorize_certificate=lambda *args:None)
        self.calls=[]
        def call(method,request):
            self.assertFalse(self.f.db.in_transaction);self.calls.append(method)
            if method=='capabilities':return {'schema':1,'http':True,'tls':True}
            if method=='issue_grant':return {'route_id':route,'operation_id':operation,'status':'granted'}
            target=self.tls if method in ('request_certificate','stage_tls','activate_tls','inspect_tls','recover_tls','verify_tls') else self.http
            return getattr(target,method)(request)
        self.f.call=call

    def until(self,phase):
        for _ in range(16):
            if self.f.status()['phase']==phase:return
            self.f.step()
        self.fail(self.f.status())

    def test_actual_host_response_shapes_reach_recorded_origin_only(self):
        self.until('origin-verified');self.f.step()
        status=self.f.status()
        self.assertEqual(status['status'],'awaiting-external');self.assertFalse(status['ready'])
        self.assertEqual(status['tls']['status'],'origin-verified')
        self.assertFalse(status['tls']['live_checked'])
        self.assertEqual(self.cert.request.call_count,1)
        self.assertEqual(self.executor.calls,['test','reload','test','reload'])
        self.assertEqual(len(self.f.fixture.writes),1)

    def test_actual_installing_recovery_compensates_then_activates_once(self):
        self.until('activate-tls')
        policy=self.http._policy();path,journal=self.tls._paths(policy,self.f.fixture.route)
        state=self.http._state(journal);state['status']='installing'
        self.http._save(journal,state);self.http._write(path,state['candidate'])
        row=domain_execution._row(self.f.db,self.f.operation);journal_data=json.loads(row['journal']);journal_data['pending_effect']='activate-tls'
        self.f.db.execute("UPDATE domain_executions SET journal=? WHERE operation_id=?",(json.dumps(journal_data),self.f.operation))
        self.f.step()
        self.assertEqual(self.f.status()['phase'],'origin-verified')
        self.assertEqual(self.calls.count('recover_tls'),1)
        self.assertEqual(self.calls.count('activate_tls'),1)
        self.assertEqual(self.executor.calls.count('reload'),2)
