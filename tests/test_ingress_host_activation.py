import dataclasses
import json
import unittest
from unittest.mock import patch
import test_ingress_host_grants as fixtures

class HostActivationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.HostGrantTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.f=self.fixture;self.f.issuer.issue(self.f.request)
        self.f.adapter.authorize_route=self.f.issuer.authorize_route
    def stage(self):
        request={'route_id':self.f.route,'operation_id':'b'*32,'expected_digest':None}
        staged=self.f.adapter.stage_http(request)
        return dict(request,digest=staged['digest'])
    def test_active_authorization_refreshes_gateway(self):
        request=self.stage();self.f.adapter.activate_http(request)
        self.assertEqual(self.f.adapter.verify({'route_id':self.f.route})['status'],'active')
        self.assertGreaterEqual(self.f.observations,4)
    def test_revocation_after_stage_blocks_activation(self):
        request=self.stage();self.f.principal=dataclasses.replace(self.f.principal,scopes=frozenset())
        with self.assertRaises(ValueError):self.f.adapter.activate_http(request)
        self.assertEqual(self.f.executor.calls,[])
        self.assertFalse(list((self.f.root/'managed').iterdir()))
    def test_gateway_drift_after_stage_blocks_activation(self):
        request=self.stage();self.f.binding['container_id']='f'*64
        with self.assertRaises(ValueError):self.f.adapter.activate_http(request)
        self.assertEqual(self.f.executor.calls,[])
    def test_revoked_recovery_has_no_effects(self):
        request=self.stage();self.f.executor.fail='reload'
        with self.assertRaises(RuntimeError):self.f.adapter.activate_http(request)
        calls=list(self.f.executor.calls);before=list((self.f.root/'managed').iterdir())[0].read_bytes()
        self.f.principal=dataclasses.replace(self.f.principal,scopes=frozenset())
        with self.assertRaises(ValueError):self.f.adapter.recover({'route_id':self.f.route})
        self.assertEqual(self.f.executor.calls,calls)
        self.assertEqual(list((self.f.root/'managed').iterdir())[0].read_bytes(),before)
    def test_unpublished_or_changed_journal_rejected(self):
        _,state=self.f.adapter._paths(self.f.policy(),self.f.route);journal=state.with_name(self.f.route+'.grant.json')
        proof=json.loads(journal.read_text());proof['status']='publishing';self.f.adapter._save(journal,proof)
        with self.assertRaises(ValueError):self.stage()
    def test_no_nested_host_lock(self):
        grant=self.f.policy()['routes'][self.f.route]
        with self.f.adapter._locked(),patch.object(self.f.adapter,'_locked',side_effect=AssertionError('nested lock')):
            self.f.issuer.authorize_route(self.f.route,grant)

    def test_credential_revocation_is_rechecked_before_activation(self):
        import application_operations as ops
        request=self.stage()
        with patch.object(ops,'recheck_credential',side_effect=ops.OperationError('credential_unavailable','Revoked',403)) as recheck:
            with self.assertRaises(ValueError):self.f.adapter.activate_http(request)
        self.assertTrue(recheck.called);self.assertEqual(self.f.executor.calls,[])

    def test_physical_gateway_replacement_cannot_reuse_grant(self):
        request=self.stage()
        # Coherent new runtime evidence with the same immutable gateway config
        # must still differ from the physical identity granted earlier.
        runtime=self.f.db.execute('SELECT journal FROM ingress_runtime_intents').fetchone()
        journal=json.loads(runtime['journal']);journal['container_id']='f'*64;journal['binding']['container_id']='f'*64
        self.f.db.execute('UPDATE ingress_runtime_intents SET journal=?',(json.dumps(journal),))
        self.f.db.execute('UPDATE ingress_ports SET container_id=?',('f'*64,))
        self.f.db.execute('UPDATE ingress_capacity SET container_id=?',('f'*64,))
        self.f.binding['container_id']='f'*64
        with self.assertRaisesRegex(ValueError,'identity changed'):self.f.adapter.activate_http(request)
        self.assertEqual(self.f.executor.calls,[])

    def test_revocation_during_gateway_probe_blocks_effects(self):
        request=self.stage();original=self.f.issuer.verify_gateway
        def revoke(*args,**kwargs):
            result=original(*args,**kwargs)
            self.f.principal=dataclasses.replace(self.f.principal,scopes=frozenset())
            return result
        self.f.issuer.verify_gateway=revoke
        with self.assertRaises(ValueError):self.f.adapter.activate_http(request)
        self.assertEqual(self.f.executor.calls,[])

    def test_revocation_during_configtest_removes_candidate_before_sibling_reload(self):
        request=self.stage()
        def revoke():self.f.principal=dataclasses.replace(self.f.principal,scopes=frozenset())
        self.f.executor.configtest=revoke
        with self.assertRaises(ValueError):self.f.adapter.activate_http(request)
        self.assertFalse(list((self.f.root/'managed').glob('*.conf')))
        # A sibling's complete-directory reload has no denied candidate to load.
        self.f.executor.reload()
        self.assertEqual(self.f.adapter.inspect({'route_id':self.f.route})['status'],'staged')

    def test_invalid_candidate_compensates_even_when_authority_revoked(self):
        request=self.stage()
        def fail():
            self.f.principal=dataclasses.replace(self.f.principal,scopes=frozenset())
            raise RuntimeError('invalid candidate')
        self.f.executor.configtest=fail
        with self.assertRaises(RuntimeError):self.f.adapter.activate_http(request)
        self.assertFalse(list((self.f.root/'managed').glob('*.conf')))
        self.assertEqual(self.f.executor.calls,[])

    def test_compensation_crash_before_restore_resumes_without_forward_authority(self):
        request=self.stage()
        self.f.executor.configtest=lambda:setattr(self.f,'principal',dataclasses.replace(self.f.principal,scopes=frozenset()))
        with patch.object(self.f.adapter,'_restore',side_effect=RuntimeError('crash before restore')):
            with self.assertRaises(RuntimeError):self.f.adapter.activate_http(request)
        self.assertEqual(self.f.adapter.inspect({'route_id':self.f.route})['status'],'compensating-file')
        self.assertEqual(self.f.adapter.recover({'route_id':self.f.route})['status'],'staged')
        self.assertFalse(list((self.f.root/'managed').glob('*.conf')))
        self.assertEqual(self.f.executor.calls,[])

    def test_compensation_crash_after_restore_is_idempotent(self):
        request=self.stage();original=self.f.adapter._restore
        self.f.executor.configtest=lambda:setattr(self.f,'principal',dataclasses.replace(self.f.principal,scopes=frozenset()))
        def restore_then_crash(*args):original(*args);raise RuntimeError('crash after restore')
        with patch.object(self.f.adapter,'_restore',side_effect=restore_then_crash):
            with self.assertRaises(RuntimeError):self.f.adapter.activate_http(request)
        self.assertFalse(list((self.f.root/'managed').glob('*.conf')))
        self.assertEqual(self.f.adapter.recover({'route_id':self.f.route})['status'],'staged')
        self.assertEqual(self.f.executor.calls,[])

    def test_compensation_never_removes_foreign_replacement(self):
        request=self.stage()
        self.f.executor.configtest=lambda:setattr(self.f,'principal',dataclasses.replace(self.f.principal,scopes=frozenset()))
        with patch.object(self.f.adapter,'_restore',side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):self.f.adapter.activate_http(request)
        path=next((self.f.root/'managed').glob('*.conf'));path.write_text('foreign replacement')
        with self.assertRaises(ValueError):self.f.adapter.recover({'route_id':self.f.route})
        self.assertEqual(path.read_text(),'foreign replacement')
        self.assertEqual(self.f.executor.calls,[])

    def test_installing_crash_then_revocation_restores_only_owned_file(self):
        request=self.stage()
        policy=self.f.policy();path,state=self.f.adapter._paths(policy,self.f.route)
        journal=self.f.adapter._state(state);journal['status']='installing'
        self.f.adapter._save(state,journal);self.f.adapter._write(path,journal['candidate'])
        self.f.principal=dataclasses.replace(self.f.principal,scopes=frozenset())
        with self.assertRaises(ValueError):self.f.adapter.recover({'route_id':self.f.route})
        self.assertFalse(path.exists());self.assertEqual(self.f.executor.calls,[])
        self.assertEqual(self.f.adapter.inspect({'route_id':self.f.route})['status'],'staged')
