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
from test_ingress_runtime import GatewayConfigurationTests


class RouteAdditionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.db=sqlite3.connect(':memory:',isolation_level=None);self.db.row_factory=sqlite3.Row;self.addCleanup(self.db.close)
        self.d=GatewayConfigurationTests().definition();self.d['schema_version']=2
        self.cid='e'*64;self.tid='f'*64
        _,self.config,self.manifest=runtime._paths(self.root,self.d,create=True)
        runtime._save(self.db,self.d,self.root,{'container_id':self.cid,'transport_id':self.tid},'verified')
        self.config.write_text(runtime._effective_config(self.db,self.d))
        self.binding={'container_id':self.cid,'transport_network_id':self.tid}
        self.commands=[];self.observed_token=generations.token(self.d,generations.head(self.db,self.d))
        self.operation='route-addition-operation-12345';self.route={'hostname':'second.example.test','service_id':'service-id'}
        self.backend={k:v for k,v in self.d['routes'][0].items() if k!='hostname'}
        def command(args,**kwargs):
            self.commands.append(args)
            if '-s' in args:
                op=generations.operation(self.db,self.operation)
                self.observed_token=generations.token(self.d,op['document']['after'])
            return ''
        def probe(docker,cid,token):
            if token!=self.observed_token:raise runtime.GatewayError('Unknown running generation')
        self.command=command
        for name,kwargs in (
            ('_verify_gateway',{'return_value':self.binding}),
            ('_verify_generation',{'side_effect':probe}),
            ('_backend',{'return_value':(self.backend,{'Id':self.d['application_network_id']})}),
            ('_command',{'side_effect':command}),('_wait_http',{'return_value':None})):
            mocked=patch.object(runtime,name,**kwargs);mocked.start();self.addCleanup(mocked.stop)
    def prepare(self):return runtime.prepare_route_addition(self.db,self.d,self.operation,self.route,root=self.root,docker='docker',authorize=lambda:None)
    def apply(self):return runtime.apply_route_addition(self.db,self.d,self.operation,root=self.root,docker='docker',authorize=lambda:None)
    def signals(self):return [c for c in self.commands if '-s' in c]

    def test_add_preserves_bootstrap_and_prior_route_updates_selected_generation(self):
        before=bindings.capture(self.db,'app','development','service-id')
        self.prepare()
        with self.assertRaisesRegex(ValueError,'pending'):bindings.capture(self.db,'app','development','service-id')
        self.apply()
        self.assertEqual([r['hostname'] for r in bindings.current_routes(self.db,self.d)],['dev.example.test','second.example.test'])
        self.assertEqual(bindings.capture(self.db,'app','development','service-id')['generation'],before['generation']+1)
        row=self.db.execute('SELECT definition,digest FROM ingress_runtime_intents').fetchone()
        self.assertEqual(json.loads(row['definition']),self.d);self.assertEqual(row['digest'],runtime.digest(self.d))
        self.assertEqual(len(self.signals()),1);self.apply();self.assertEqual(len(self.signals()),1)

    def test_lost_reload_reply_only_observes(self):
        self.prepare()
        def lost(args,**kwargs):
            result=self.command(args,**kwargs)
            if '-s' in args:raise runtime.GatewayError('lost reply')
            return result
        with patch.object(runtime,'_command',side_effect=lost),self.assertRaises(ValueError):self.apply()
        self.assertEqual(generations.operation(self.db,self.operation)['phase'],'reload-intent')
        self.apply();self.assertEqual(len(self.signals()),1)

    def test_unknown_signal_outcome_never_resignals(self):
        self.prepare()
        def unknown(args,**kwargs):
            if '-s' in args:self.commands.append(args);raise runtime.GatewayError('unknown')
            return self.command(args,**kwargs)
        with patch.object(runtime,'_command',side_effect=unknown),self.assertRaises(ValueError):self.apply()
        with self.assertRaisesRegex(ValueError,'Unknown running'):self.apply()
        self.assertEqual(len(self.signals()),1)
        self.assertEqual(generations.head(self.db,self.d)['generation'],1)

    def test_invalid_candidate_restores_A_without_signal(self):
        original=self.config.read_text();self.prepare()
        with patch.object(runtime,'_command',side_effect=runtime.GatewayError('invalid')),self.assertRaises(ValueError):self.apply()
        self.assertEqual(self.config.read_text(),original);self.assertEqual(self.signals(),[])
        self.assertEqual(generations.operation(self.db,self.operation)['phase'],'aborted')
        self.assertEqual(generations.head(self.db,self.d)['generation'],1)

    def test_pre_signal_crash_resumes_idempotently(self):
        class Crash(BaseException):pass
        self.prepare();original=runtime._replace_route_config
        def crash(*args):original(*args);raise Crash()
        with patch.object(runtime,'_replace_route_config',side_effect=crash),self.assertRaises(Crash):self.apply()
        self.assertEqual(generations.operation(self.db,self.operation)['phase'],'installing')
        self.apply();self.assertEqual(len(self.signals()),1)

    def test_existing_file_mount_requires_migration(self):
        d=copy.deepcopy(self.d);d['schema_version']=1
        with self.assertRaisesRegex(ValueError,'migration'):runtime.prepare_route_addition(self.db,d,self.operation,self.route,root=self.root,docker='docker',authorize=lambda:None)

    def test_schema2_mount_directory_and_generation_endpoint(self):
        service=runtime.gateway_manifest(self.d,self.config,self.tid)['services']['development-ingress']
        self.assertEqual(service['volumes'][0]['source'],str(self.config.parent))
        self.assertEqual(service['volumes'][0]['target'],'/etc/nginx/ingress')
        self.assertIn('/generation',self.config.read_text())
        self.assertIn(self.observed_token,self.config.read_text())

    def test_second_pending_operation_and_changed_replay_rejected(self):
        self.prepare()
        with self.assertRaises(ValueError):runtime.prepare_route_addition(self.db,self.d,'different-operation-12345',self.route,root=self.root,docker='docker',authorize=lambda:None)
        with self.assertRaises(ValueError):runtime.prepare_route_addition(self.db,self.d,self.operation,dict(self.route,hostname='other.example.test'),root=self.root,docker='docker',authorize=lambda:None)

    def test_new_service_becomes_capturable_and_sibling_snapshot_stays_valid(self):
        sibling=bindings.capture(self.db,'app','development','service-id')
        self.route['service_id']='new-service'
        new=dict(self.backend,service_id='new-service',definition_id='new-definition',alias='development-api',container_id='1'*64)
        original=self.backend
        def backend(db,project,environment,sid,docker):
            return (new if sid=='new-service' else original),{'Id':self.d['application_network_id']}
        with patch.object(runtime,'_backend',side_effect=backend):self.prepare();self.apply()
        self.assertEqual(bindings.capture(self.db,'app','development','service-id'),sibling)
        self.assertEqual(bindings.capture(self.db,'app','development','new-service')['backend'],new)

    def test_stale_config_generation_and_backend_drift_fail_before_signal(self):
        self.prepare()
        self.db.execute('UPDATE ingress_route_heads SET generation=9')
        with self.assertRaisesRegex(ValueError,'generation changed'):self.apply()
        self.assertEqual(self.commands,[])

    def test_backend_commit_fence_rolls_back_partial_generation_changes(self):
        self.prepare();before=bindings.capture(self.db,'app','development','service-id',allow_pending=True)
        original=generations.phase
        def fail(db,operation,expected,target):
            if target=='committed':raise ValueError('CAS failed')
            return original(db,operation,expected,target)
        with patch.object(generations,'phase',side_effect=fail),self.assertRaisesRegex(ValueError,'CAS failed'):self.apply()
        self.assertEqual(generations.head(self.db,self.d)['generation'],1)
        self.assertEqual(bindings.capture(self.db,'app','development','service-id',allow_pending=True),before)
        self.apply();self.assertEqual(len(self.signals()),1)

    def test_missing_effective_head_is_not_reconstructed_from_bootstrap(self):
        self.db.execute('DELETE FROM ingress_route_heads')
        with self.assertRaisesRegex(ValueError,'evidence is missing'):runtime._effective_config(self.db,self.d)
        with self.assertRaisesRegex(ValueError,'evidence is missing'):runtime._save(self.db,self.d,self.root,{},'prepared')
        self.assertFalse(self.db.execute('SELECT 1 FROM ingress_route_heads').fetchone())

    def abort(self):
        self.prepare()
        with patch.object(runtime,'_command',side_effect=runtime.GatewayError('invalid')),self.assertRaises(ValueError):self.apply()

    def retry(self):
        return runtime.retry_aborted_route_addition(self.db,self.d,self.operation,root=self.root,docker='docker',authorize=lambda:None)

    def test_explicit_retry_retains_aborted_attempt_then_completes(self):
        self.abort();old=generations.operation(self.db,self.operation)['document']
        retried=self.retry()
        self.assertEqual(retried['phase'],'prepared')
        self.assertEqual(retried['document']['attempt'],2)
        self.assertEqual(retried['document']['attempt_history'],[{'phase':'aborted','document':old}])
        self.apply();self.assertEqual(len(self.signals()),1)
        self.assertEqual(generations.operation(self.db,self.operation)['phase'],'committed')

    def test_retry_refuses_active_candidate_unknown_signal_and_foreign_file(self):
        self.abort();old=generations.operation(self.db,self.operation)['document']
        self.observed_token=generations.token(self.d,old['after'])
        with self.assertRaisesRegex(ValueError,'Unknown running'):self.retry()
        self.observed_token=generations.token(self.d,old['before'])
        self.config.write_text('foreign')
        with self.assertRaisesRegex(ValueError,'Compensated'):self.retry()
        self.config.write_text(old['previous'])
        self.db.execute("UPDATE ingress_route_operations SET phase='reload-intent'")
        with self.assertRaisesRegex(ValueError,'Only an aborted'):self.retry()
        self.assertEqual(self.signals(),[])
