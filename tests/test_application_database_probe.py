import copy
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import application_database_bindings as bindings
import application_database_probe as probe
import registry
import service_runtime
import test_application_database_bindings as fixtures


class ApplicationProbeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.AttachmentTests()
        self.f.engine = 'mysql'
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.saved = self.f.save()
        self.pin = self.f.pin(self.saved)
        self.metadata = bindings.resolve_runtime(self.f.db, 'example', 'development', self.f.service, self.pin, authorize=lambda: None)
        self.candidate = {'project':'example','environment':'development','reference':'db',
            'engine':'mysql','database':'app','port':3306,'recipe_revision':self.f.f.recipe_id,
            'project_revision':registry.get_project(self.f.db,'example')['revision'],
            'environment_revision':registry.get_environment(self.f.db,'example','development')['revision'],
            'container_id':'a'*64}
        self.helper = Mock()
        self.proof = {'engine':'mysql','database':'app','server_version':'8.4.11','cluster_identity_verified':False}
        def inspect(container, request, *, revalidate):
            self.request = request
            revalidate(container)
            return {'result': dict(self.proof)}
        self.helper.inspect.side_effect = inspect
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def verify(self, **changes):
        options = dict(docker='/usr/bin/docker',socket='unix:///tmp/probe.sock',key_path=self.f.key,
            resolve_candidate=lambda *args, **kwargs: copy.deepcopy(self.candidate),authorize=lambda:None,
            state_dir=self.tmp.name)
        with patch.object(probe,'InspectorHelper',return_value=self.helper) as factory:
            result = probe.verify(self.f.db,self.metadata,**(options|changes))
            self.factory_args = factory.call_args.kwargs
            return result

    def test_real_binding_credential_and_fresh_candidate_private_helper(self):
        result=self.verify()
        self.assertEqual(result['identity'],self.proof)
        self.assertEqual(result['candidate']['container_id'],self.candidate['container_id'])
        self.assertEqual(self.request['password'],'private/@:value')
        self.assertEqual(self.request['action'],'application-identity')
        self.assertEqual(self.factory_args['socket'],'unix:///tmp/probe.sock')
        self.assertNotIn('private',str(self.proof))
        self.helper.reconcile_pending.assert_called_once()

    def test_identity_drift_and_malformed_engine_result_reject(self):
        def changed(container, request, *, revalidate):
            self.candidate['container_id'] = 'b'*64
            revalidate(container)
        self.helper.inspect.side_effect = changed
        with self.assertRaises(probe.ProbeError):self.verify()
        self.helper.inspect.side_effect = lambda *args,**kwargs: {'result':self.proof|{'engine':'mariadb'}}
        with self.assertRaises(probe.ProbeError):self.verify()

    def test_deadline_and_authority_fail_before_private_dispatch(self):
        with self.assertRaises(probe.ProbeError):self.verify(deadline=time.monotonic()-1)
        self.helper.inspect.assert_not_called()
        with self.assertRaises(probe.ProbeError) as error:
            self.verify(authorize=Mock(side_effect=ValueError('private/secret')))
        self.assertNotIn('private',str(error.exception))
        self.helper.inspect.assert_not_called()

    def test_bounded_transport_preserves_socket_payload_and_overall_deadline(self):
        runner = Mock(return_value=b'{}')
        deadline=time.monotonic()+1
        self.verify(runner=runner,deadline=deadline)
        call=self.factory_args['runner']
        self.assertEqual(call(['/usr/bin/docker','inspect','a'],b'private',timeout=20,env={'DOCKER_HOST':'unix:///tmp/probe.sock'}),b'{}')
        self.assertLessEqual(runner.call_args.kwargs['timeout'],1)
        with self.assertRaises(probe.ProbeError):call(['/usr/bin/docker','inspect'],timeout=1,env={'DOCKER_HOST':'unix:///wrong.sock'})

    def test_context_probe_rejects_before_ensure_or_verify_network_effects(self):
        context=service_runtime.Context(self.f.db,lambda:None,self.f.key)
        definition={'project':'example','environment':'development','service_id':self.f.service,
                    'runtime':{'database_attachment':self.pin}}
        for ensure in (False,True):
            with patch.object(probe,'verify',side_effect=probe.ProbeError('identity_changed')), patch('application_database_networks.ensure_attachment') as create, patch('application_database_networks.verify_attachment') as verify:
                with self.assertRaises(probe.ProbeError):context.attachment(definition,'/usr/bin/docker',ensure=ensure)
                create.assert_not_called();verify.assert_not_called()

    def test_workload_probe_keeps_runner_socket_and_deadline_before_bridge(self):
        from workload_transport import WorkloadTransport
        runner=Mock()
        transport=WorkloadTransport(self.f.db,docker='/usr/bin/docker',socket='unix:///tmp/workload.sock',runner=runner)
        definition={'project':'example','environment':'development','service_id':self.f.service,
                    'runtime':{'database_attachment':self.pin}}
        deadline=time.monotonic()+7
        with patch.object(probe,'verify',side_effect=probe.ProbeError('identity_changed')) as check, patch('application_database_networks.verify_attachment') as bridge:
            with self.assertRaises(probe.ProbeError):transport._database_attachment(definition,lambda:None,deadline)
            self.assertIs(check.call_args.kwargs['runner'],runner)
            self.assertEqual(check.call_args.kwargs['deadline'],deadline)
            self.assertEqual(check.call_args.kwargs['socket'],'unix:///tmp/workload.sock')
            bridge.assert_not_called();runner.assert_not_called()

    def test_context_candidate_inventory_and_compose_share_probe_deadline(self):
        import database_candidates
        import database_helper
        import compose_review
        import control
        context=service_runtime.Context(self.f.db,lambda:None,self.f.key)
        definition={'project':'example','environment':'development','service_id':self.f.service,
                    'runtime':{'database_attachment':self.pin}}
        clock=[100.0]
        def resolve(*args,**options):
            options['inventory'].runner(['/usr/bin/docker','inspect','target'],timeout=8,env={},limit=1048576)
            options['provenance_resolver']({},'db',[])
        def review(recipe,service,files,root,command,compose):
            return command([compose,'config'])
        def check(db,metadata,**options):
            self.assertEqual(options['deadline'],145.0)
            clock[0]=144.5
            options['resolve_candidate']('example','development','db')
            clock[0]=145.0
            with self.assertRaises(probe.ProbeError):options['resolve_candidate']('example','development','db')
            raise probe.ProbeError('fixture_stop')
        with patch('time.monotonic',side_effect=lambda:clock[0]), patch.object(probe,'verify',side_effect=check), patch.object(database_candidates,'resolve',side_effect=resolve), patch.object(compose_review,'review',side_effect=review), patch.object(control,'COMPOSE','/trusted/compose'), patch.object(database_helper,'exchange',return_value=b'{}') as exchange, patch('application_database_networks.ensure_attachment') as bridge:
            with self.assertRaises(probe.ProbeError):context.attachment(definition,'/usr/bin/docker',ensure=True)
            self.assertEqual(exchange.call_count,2)
            for call in exchange.call_args_list:
                self.assertEqual(call.kwargs['timeout'],0.5)
                self.assertEqual(call.kwargs['env']['DOCKER_HOST'],'unix:///run/docker-isolation.sock')
            self.assertEqual(exchange.call_args_list[1].args[0][0],'/trusted/compose')
            bridge.assert_not_called()

    def test_candidate_continuity_normalizes_mariadb_and_fences_first_network_snapshot(self):
        import database_target
        metadata=copy.deepcopy(self.metadata)
        metadata['definition']['engine']='mariadb'
        raw=dict(self.candidate,started_at='original',networks=[{'id':'original'}])
        expected=database_target.resolve_ownership(metadata,resolve_candidate=lambda *a,**kw:copy.deepcopy(raw))
        proof={'identity':self.proof,'candidate':expected}
        resolver=lambda *a,**kw:copy.deepcopy(raw)
        pinned=probe.bind_candidate(metadata,proof,resolver)
        self.assertEqual(pinned('example','development','db')['engine'],'mysql')
        # A later sanctioned bridge network change is left to network journal rules.
        raw['networks'].append({'id':'bridge'})
        self.assertEqual(len(pinned('example','development','db')['networks']),2)
        raw['started_at']='restarted'
        with self.assertRaises(probe.ProbeError):pinned('example','development','db')
        raw['started_at']='original'
        first=probe.bind_candidate(metadata,proof,resolver)
        with self.assertRaises(probe.ProbeError):first('example','development','db')

    def test_restart_after_probe_before_first_ensure_rejects_before_network_mutation(self):
        import database_candidates
        import database_target
        context=service_runtime.Context(self.f.db,lambda:None,self.f.key)
        definition={'project':'example','environment':'development','service_id':self.f.service,
                    'runtime':{'database_attachment':self.pin}}
        raw=dict(self.candidate,started_at='original',networks=[])
        expected=database_target.resolve_ownership(self.metadata,resolve_candidate=lambda *a,**kw:raw)
        effects=Mock()
        def ensure(db,metadata,**options):
            options['resolve_candidate']('example','development','db',kind='adopted-compose')
            effects()
        with patch.object(probe,'verify',return_value={'identity':self.proof,'candidate':expected}), patch.object(database_candidates,'resolve',return_value=raw|{'started_at':'restarted'}), patch('application_database_networks.ensure_attachment',side_effect=ensure):
            with self.assertRaises(probe.ProbeError):context.attachment(definition,'/usr/bin/docker',ensure=True)
        effects.assert_not_called()


if __name__=='__main__':unittest.main()
