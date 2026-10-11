import copy
import json
import unittest
from unittest.mock import patch

import test_service_provisioning as fixture
import managed_rollout
import managed_services
import service_provisioning as provision


class RolloutTests(unittest.TestCase):
    command = fixture.ProvisioningTests.command
    execute = fixture.ProvisioningTests.execute
    # Reuse the deterministic Docker fixture, not the provisioning test cases.
    def setUp(self):
        fixture.ProvisioningTests.setUp(self)
        self.definition['desired_state'] = 'running'
        binding = self.execute()
        self.old_definition = copy.deepcopy(self.definition)
        self.old_container = copy.deepcopy(self.container)
        self.new_definition = copy.deepcopy(self.definition)
        self.new_definition['artifact']['image'] = 'sha256:' + 'e' * 64
        self.new_definition['artifact']['id'] = 'new-artifact'
        self.new_definition['artifact_id'] = 'new-artifact'
        self.item = {'definition': self.old_definition, 'binding': binding,
                     'resources': {'memory_mib':64, 'cpu':.25}, 'next_definition':self.new_definition,
                     'artifact':self.new_definition['artifact'], 'operation_id':'operation-id'}
        self.peers = [{'id':'service-id', 'definition':self.old_definition, 'binding':binding,
                       'effective_resources':self.item['resources'], 'operation_id':'operation-id'}]
        self.calls.clear()
        self.replacement_count = 0
        self.unhealthy = False
        self.uncertain = False
        self.extra = [patch.object(managed_services, 'environment_definitions', side_effect=lambda *a:copy.deepcopy(self.peers)),
                      patch.object(managed_services, 'artifact_for_service', return_value=self.new_definition['artifact']),
                      patch.object(provision, 'command', side_effect=self.rollout_command)]
        for p in self.extra: p.start(); self.addCleanup(p.stop)

    def rollout_command(self, args, **kwargs):
        if args[1:3] == ['image','inspect']:
            return json.dumps([{'Id':args[-1], 'Config':{}}])
        if args[0] == 'compose':
            self.replacement_count += 1
            self.definition = self.new_definition if self.replacement_count == 1 else self.old_definition
            result = self.command(args, **kwargs)
            self.container['Id'] = ('d' if self.replacement_count == 1 else 'f') * 64
            if self.uncertain: raise provision.ProvisioningError('uncertain')
            return result
        result = self.command(args, **kwargs)
        if args[1] == 'start' and self.unhealthy and self.replacement_count == 1:
            self.container['State']['Health']['Status'] = 'unhealthy'
        return result

    def deliver(self):
        return managed_rollout.rollout(self.db, 'delivery-1', self.item, docker='docker', compose='compose', authorize=lambda:None, health_timeout=0)

    def test_rollout_verified_new_image_and_exact_start(self):
        result = self.deliver()
        self.assertEqual(result['image_id'], self.new_definition['artifact']['image'])
        self.assertEqual(result['container_id'], 'd'*64)
        self.assertIn(['docker','start','d'*64], self.calls)
        self.assertEqual(self.replacement_count, 1)
        with self.assertRaisesRegex(ValueError,'reconciliation'): self.deliver()
        self.assertEqual(self.replacement_count, 1)

    def test_stopped_after_build_never_wakes(self):
        self.container['State']['Running'] = False
        with self.assertRaisesRegex(ValueError,'runtime state changed'): self.deliver()
        self.assertEqual(self.replacement_count,0)

    def test_stopped_selection_remains_untouched(self):
        self.container['State']['Running'] = False
        self.item['binding']['state'] = 'stopped'
        result = self.deliver()
        self.assertEqual(result['state'],'stopped')
        self.assertEqual(self.replacement_count,0)

    def test_health_failure_restores_verified_old_image(self):
        self.unhealthy = True
        with self.assertRaises(managed_rollout.RolloutError) as error: self.deliver()
        self.assertEqual(error.exception.restored_binding['image_id'], self.old_definition['artifact']['image'])
        self.assertEqual(self.replacement_count,2)
        self.assertEqual(self.container['State']['Health']['Status'],'healthy')

    def test_ambiguous_create_never_retries_or_rolls_back(self):
        self.uncertain = True
        with self.assertRaisesRegex(ValueError,'uncertain'): self.deliver()
        with self.assertRaisesRegex(ValueError,'reconciliation'): self.deliver()
        self.assertEqual(self.replacement_count,1)

    def test_exact_old_id_mismatch_blocks_before_mutation(self):
        self.container['Id'] = '9'*64
        with self.assertRaisesRegex(ValueError,'identity changed'): self.deliver()
        self.assertEqual(self.replacement_count,0)

    def test_manifest_drift_blocks_before_mutation(self):
        path = self.root/'example/development/compose.json'
        document=json.loads(path.read_text()); document['services']['development-app']['privileged']=True
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError,'manifest drifted'): self.deliver()
        self.assertEqual(self.replacement_count,0)

    def reconcile(self):
        return managed_rollout.reconcile(self.db, 'delivery-1', self.item, docker='docker', authorize=lambda:None)

    def test_reconcile_delivery_verified_is_read_only(self):
        result = self.deliver()
        self.calls.clear()
        before = self.db.total_changes
        evidence = self.reconcile()
        self.assertEqual(evidence['outcome'], 'verified-new', evidence)
        self.assertEqual(evidence['binding']['container_id'], result['container_id'])
        self.assertEqual(self.db.total_changes, before)
        self.assertTrue(all(c[1] in ('inspect','network') for c in self.calls))
        self.assertEqual(self.replacement_count, 1)

    def test_reconcile_rollback_verified(self):
        self.unhealthy = True
        with self.assertRaises(managed_rollout.RolloutError): self.deliver()
        evidence = self.reconcile()
        self.assertEqual(evidence['outcome'], 'verified-old', evidence)
        self.assertEqual(evidence['binding']['container_id'], 'f'*64)
        self.assertEqual(self.replacement_count, 2)

    def test_reconcile_never_adopts_absence_or_unrecorded_replacement(self):
        self.uncertain = True
        with self.assertRaises(ValueError): self.deliver()
        self.assertEqual(self.reconcile()['outcome'], 'unknown')
        self.container = None
        self.assertEqual(self.reconcile()['outcome'], 'unknown')
        self.assertEqual(self.replacement_count, 1)

    def test_reconcile_no_journal_proves_untouched_running_and_stopped(self):
        self.assertEqual(self.reconcile()['outcome'], 'verified-old')
        self.container['State']['Running'] = False
        self.item['binding']['state'] = 'stopped'
        self.assertEqual(self.reconcile()['outcome'], 'verified-old')
        self.assertEqual(self.replacement_count, 0)

    def test_reconcile_rejects_fingerprint_health_configuration_network_drift(self):
        self.deliver()
        pristine = copy.deepcopy(self.container)
        self.item['resources']['cpu'] = .5
        self.assertEqual(self.reconcile()['outcome'], 'unknown')
        self.item['resources']['cpu'] = .25
        for mutate in (lambda: self.container['State']['Health'].update(Status='unhealthy'),
                       lambda: self.container['HostConfig'].update(Privileged=True),
                       lambda: self.container.update(Id='9'*64)):
            mutate()
            self.assertEqual(self.reconcile()['outcome'], 'unknown')
            self.container = copy.deepcopy(pristine)
        self.network['Containers'] = {'9'*64: {}}
        self.assertEqual(self.reconcile()['outcome'], 'unknown')

    def test_reconcile_revoked_authorization_propagates(self):
        with self.assertRaisesRegex(ValueError, 'revoked'):
            managed_rollout.reconcile(self.db, 'delivery-1', self.item, docker='docker',
                authorize=lambda: (_ for _ in ()).throw(ValueError('revoked')))
        self.assertEqual(self.calls, [])
    def test_reconcile_completed_metadata_normalizes_only_plan_progress(self):
        self.item['state'] = 'pending'
        result = self.deliver()
        self.item.update(state='succeeded', verified_binding=result)
        self.peers[0].update(definition=self.new_definition, binding=result)
        evidence = self.reconcile()
        self.assertEqual(evidence['outcome'], 'verified-new', evidence)

    def test_reconcile_skipped_stopped_without_next_definition(self):
        self.container['State']['Running'] = False
        self.item['binding']['state'] = 'stopped'
        del self.item['next_definition']
        del self.item['artifact']
        self.item['state'] = 'skipped-stopped'
        self.assertEqual(self.reconcile()['outcome'], 'verified-old')

    def test_reconcile_manifest_intent_requires_untouched_manifest_and_id(self):
        with patch.object(provision, '_atomic', side_effect=OSError('interrupted before manifest write')):
            with self.assertRaises(OSError): self.deliver()
        self.assertEqual(self.reconcile()['outcome'], 'verified-old')
        self.container['Id'] = '9'*64
        self.assertEqual(self.reconcile()['outcome'], 'unknown')

    def test_reconcile_start_intent_uses_actual_health_not_phase_claim(self):
        self.deliver()
        row = self.db.execute('SELECT document FROM managed_rollout_journals').fetchone()
        journal = json.loads(row['document']); journal['phase'] = 'delivery-start-intent'
        self.db.execute('UPDATE managed_rollout_journals SET document=?', (json.dumps(journal),))
        self.assertEqual(self.reconcile()['outcome'], 'verified-new')
        self.container['State']['Running'] = False
        self.assertEqual(self.reconcile()['outcome'], 'unknown')
    def test_reconcile_repeated_rollback_observation_keeps_original_fingerprint(self):
        self.item['state'] = 'pending'
        self.unhealthy = True
        with self.assertRaises(managed_rollout.RolloutError): self.deliver()
        result = self.reconcile()['binding']
        self.item.update(state='rolled-back', verified_binding=result)
        self.peers[0]['binding'] = result
        self.assertEqual(self.reconcile()['outcome'], 'verified-old')
        self.item['state'] = 'unchanged'
        self.assertEqual(self.reconcile()['outcome'], 'verified-old')

    def test_verified_gateway_admitted_in_rollout_and_reconciliation(self):
        from types import SimpleNamespace
        self.network['Containers'] = {'8'*64: {}}
        helper = SimpleNamespace(verified_network_members=lambda *a, **k: frozenset({'8'*64}))
        with patch.dict('sys.modules', {'ingress_runtime': helper}):
            self.assertEqual(self.deliver()['container_id'], 'd'*64)
            self.assertEqual(self.reconcile()['outcome'], 'verified-new')
            helper.verified_network_members = lambda *a, **k: (_ for _ in ()).throw(ValueError('gateway missing'))
            self.assertEqual(self.reconcile()['outcome'], 'unknown')

    def test_gateway_drift_blocks_rollout_before_mutation(self):
        from types import SimpleNamespace
        helper = SimpleNamespace(verified_network_members=lambda *a, **k: (_ for _ in ()).throw(ValueError('gateway drift')))
        with patch.dict('sys.modules', {'ingress_runtime': helper}):
            with self.assertRaisesRegex(ValueError, 'gateway drift'): self.deliver()
        self.assertEqual(self.replacement_count, 0)

    def test_unverified_same_label_peer_blocks_rollout_and_reconciliation(self):
        self.network['Containers'] = {'9'*64: {}}
        with self.assertRaisesRegex(ValueError, 'unknown container'): self.deliver()
        self.assertEqual(self.reconcile()['outcome'], 'unknown')
        self.assertEqual(self.replacement_count, 0)

    def test_incompatible_gateway_endpoint_fails_before_docker_mutation(self):
        import ingress_bindings
        with patch.object(ingress_bindings,'preflight',side_effect=ValueError('incompatible endpoint')):
            with self.assertRaisesRegex(ValueError,'incompatible endpoint'):self.deliver()
        self.assertEqual(self.replacement_count,0)
        self.assertEqual(self.calls,[])

    def test_reconcile_fingerprint_excludes_only_committed_route_snapshot(self):
        self.item.update(state='pending',ingress_snapshot=None)
        result=self.deliver()
        self.item.update(state='succeeded',verified_binding=result,ingress_current=None)
        self.assertEqual(self.reconcile()['outcome'],'verified-new')
        self.item['ingress_snapshot']={'changed':'original intent'}
        self.assertEqual(self.reconcile()['outcome'],'unknown')
