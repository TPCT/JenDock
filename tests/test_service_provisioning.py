import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import managed_services
import service_provisioning as provision
from store import connect


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.db = connect(self.root / 'state')
        self.addCleanup(self.db.close)
        self.definition = {'service_id': 'service-id', 'project': 'example', 'environment': 'development',
            'name': 'app', 'artifact_id': 'artifact-id', 'template_id': 'app', 'template_revision': 1,
            'environment_revision': 1, 'policy_revision': 1, 'desired_state': 'stopped',
            'resources': {'memory_mib': 64, 'cpu': 0.25},
            'artifact': {'image': 'sha256:' + 'a' * 64, 'command': ['app', '--value=$unchanged'],
                         'healthcheck': ['app', 'health'], 'user': '65534:65534'}}
        self.network = None; self.container = None; self.calls = []
        self.patchers = [patch.object(managed_services, 'artifact_for_service', side_effect=lambda *args: copy.deepcopy(self.definition['artifact'])),
            patch.object(managed_services, 'environment_definitions', create=True, side_effect=lambda *args: [{'definition': copy.deepcopy(self.definition), 'operation_id': 'operation-id'}]),
            patch.object(provision, 'command', side_effect=self.command)]
        for patcher in self.patchers: patcher.start(); self.addCleanup(patcher.stop)

    def command(self, args, **kwargs):
        self.calls.append(args)
        if args[1:3] == ['image', 'inspect']:
            return json.dumps([{'Id': self.definition['artifact']['image'], 'Config': {}}])
        if args[1:3] == ['network', 'ls']: return self.network['Id'] if self.network else ''
        if args[1:3] == ['network', 'create']:
            self.network = {'Id': 'b'*64, 'Name': 'example-development-managed', 'Driver': 'bridge',
                'Internal': True, 'EnableIPv6': False, 'Options': {}, 'Containers': {},
                'Labels': {provision.PREFIX+'project': 'example', provision.PREFIX+'environment': 'development'}}
            return self.network['Id']
        if args[1:3] == ['network', 'inspect']: return json.dumps([self.network])
        if args[1] == 'ps': return self.container['Id'] if self.container else ''
        if args[0] == 'compose':
            spec = provision.service_config(self.definition, 'operation-id')
            self.container = {'Id': 'c'*64, 'Name': '/example-development-app-1', 'Image': self.definition['artifact']['image'],
                'Config': {'Labels': {**spec['labels'], 'com.docker.compose.project': 'example',
                    'com.docker.compose.service': 'development-app', 'com.docker.compose.project.config_files': str(self.root/'example/development/compose.json')},
                    'Entrypoint': ['app'], 'Cmd': ['--value=$unchanged'], 'User': '65534:65534',
                    'Healthcheck': {'Test': ['CMD', 'app', 'health']}},
                'HostConfig': {'Privileged': False, 'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                    'SecurityOpt': ['no-new-privileges:true'], 'Memory': 64*1048576, 'MemorySwap': 64*1048576,
                    'NanoCpus': 250000000, 'PidsLimit': 128, 'IpcMode': 'private', 'NetworkMode': 'example-development-managed'},
                'Mounts': [], 'NetworkSettings': {'Networks': {'example-development-managed': {'NetworkID': self.network['Id']}}},
                'State': {'Running': False, 'StartedAt': '0001-01-01T00:00:00Z'}}
            return ''
        if args[1] == 'inspect': return json.dumps([self.container])
        if args[1] == 'start':
            self.container['State'] = {'Running': True, 'StartedAt': '2026-01-01T00:00:00Z', 'Health': {'Status': 'healthy'}}
            return args[-1]
        raise AssertionError(args)

    def execute(self, authorize=lambda: None):
        return provision.provision(self.db, 'operation-id', self.definition, root=self.root,
                                   docker='docker', compose='compose', authorize=authorize, health_timeout=0)

    def test_stopped_creation_pins_isolation_and_replay_keeps_id_without_recreate(self):
        first = self.execute(); second = self.execute()
        self.assertEqual(first['container_id'], second['container_id'])
        self.assertEqual(first['state'], 'stopped')
        self.assertEqual(len([call for call in self.calls if call[0] == 'compose']), 1)
        self.assertFalse(any(call[1] == 'start' for call in self.calls))
        document = json.loads((self.root/'example/development/compose.json').read_text())
        service = document['services']['development-app']
        self.assertEqual(service['command'], ['--value=$$unchanged'])
        self.assertFalse({'volumes', 'ports', 'environment', 'privileged'} & set(service))

    def test_running_requires_actual_health_and_start_uses_immutable_id(self):
        self.definition['desired_state'] = 'running'
        result = self.execute(); self.execute()
        self.assertEqual(result['state'], 'running')
        self.assertEqual([call for call in self.calls if call[1] == 'start'], [['docker', 'start', 'c'*64]])

    def test_replaced_container_cannot_be_adopted_on_replay(self):
        self.execute(); self.container['Id'] = 'd'*64
        with self.assertRaisesRegex(ValueError, 'replaced'): self.execute()

    def test_missing_container_after_verified_never_recreates(self):
        self.execute(); self.container = None
        with self.assertRaisesRegex(ValueError, 'outcome is uncertain'): self.execute()
        self.assertEqual(len([call for call in self.calls if call[0] == 'compose']), 1)

    def test_foreign_network_rejected_without_container_create(self):
        self.command(['docker', 'network', 'create'])
        self.network['Labels'][provision.PREFIX+'environment'] = 'production'
        with self.assertRaisesRegex(ValueError, 'network.*ownership'): self.execute()
        self.assertFalse(any(call[0] == 'compose' for call in self.calls))

    def test_changed_artifact_or_unauthorized_worker_never_mutates(self):
        with patch.object(managed_services, 'artifact_for_service', return_value={}):
            with self.assertRaisesRegex(ValueError, 'artifact changed'): self.execute()
        with self.assertRaisesRegex(ValueError, 'revoked'):
            self.execute(authorize=lambda: (_ for _ in ()).throw(ValueError('revoked')))
        self.assertEqual(self.calls, [])

    def test_manifest_drift_preserved_and_rejected(self):
        self.execute(); path = self.root/'example/development/compose.json'
        data = json.loads(path.read_text()); data['services']['foreign'] = {}; path.write_text(json.dumps(data))
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'drifted'): self.execute()
        self.assertEqual(path.read_bytes(), before)

    def test_previously_started_service_not_silently_restarted_after_failure(self):
        self.definition['desired_state'] = 'running'; self.execute()
        self.container['State']['Running'] = False
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'Recovery is required before restarting'): self.execute()
        self.assertEqual(len([call for call in self.calls if call[1] == 'start']), 1)

    def test_ambiguous_create_response_reconciles_exact_container_without_second_create(self):
        original = self.command
        interrupted = False
        def fail_after_create(args, **kwargs):
            nonlocal interrupted
            result = original(args, **kwargs)
            if args[0] == 'compose' and not interrupted:
                interrupted = True
                raise provision.ProvisioningError('outcome uncertain')
            return result
        with patch.object(provision, 'command', side_effect=fail_after_create):
            with self.assertRaisesRegex(ValueError, 'outcome uncertain'): self.execute()
            result = self.execute()
        self.assertEqual(result['container_id'], 'c'*64)
        self.assertEqual(len([call for call in self.calls if call[0] == 'compose']), 1)

    def test_image_declared_volumes_fail_before_files_or_network_creation(self):
        with patch.object(provision, 'command', return_value=json.dumps([{'Id':'sha256:'+'a'*64,'Config':{'Volumes':{'/data':{}}}}])):
            with self.assertRaisesRegex(ValueError, 'persistence not supported'): self.execute()
        self.assertFalse((self.root/'example').exists())

    def test_managed_path_symlink_is_not_followed(self):
        foreign = self.root/'foreign'; foreign.mkdir(); (self.root/'example').symlink_to(foreign)
        with self.assertRaisesRegex(ValueError, 'private directory'): self.execute()
        self.assertEqual(list(foreign.iterdir()), [])

    def test_health_failure_never_reports_success_and_retains_start_journal(self):
        self.definition['desired_state'] = 'running'
        original = self.command
        def unhealthy(args, **kwargs):
            result = original(args, **kwargs)
            if args[1] == 'start': self.container['State']['Health']['Status'] = 'unhealthy'
            return result
        with patch.object(provision, 'command', side_effect=unhealthy):
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, 'health was not verified'): self.execute()
        self.assertEqual(len([call for call in self.calls if call[1] == 'start']), 1)
        journal = json.loads(self.db.execute('SELECT document FROM service_provisioning_journals').fetchone()[0])
        self.assertEqual(journal['phase'], 'start-intent')

    def test_same_id_stopped_peer_is_refreshed_before_new_running_service_finishes(self):
        self.definition['desired_state'] = 'running'
        first_definition = copy.deepcopy(self.definition)
        first_binding = self.execute()
        stopped_peer = copy.deepcopy(self.container)
        stopped_peer['State']['Running'] = False
        # A normal stop preserves ID, command, image, ownership and resource caps.
        self.definition = dict(first_definition, name='second', service_id='second-id')
        self.container = None
        refreshed = []
        original = self.command
        def command(args, **kwargs):
            if args[1] == 'ps' and args[-1] == 'name=^/example-development-app-1$':
                return stopped_peer['Id']
            if args[1] == 'inspect' and args[-1] == stopped_peer['Id']:
                return json.dumps([stopped_peer])
            value = original(args, **kwargs)
            if args[0] == 'compose':
                self.container['Id'] = 'd'*64
                self.container['Name'] = '/example-development-second-1'
                self.container['Config']['Labels'] = {
                    **provision._labels(self.definition, 'second-operation'),
                    'com.docker.compose.project': 'example', 'com.docker.compose.service': 'development-second',
                    'com.docker.compose.project.config_files': str(self.root/'example/development/compose.json')}
            return value
        accepted = [{'definition': first_definition, 'operation_id': 'operation-id', 'binding': first_binding},
                    {'definition': self.definition, 'operation_id': 'second-operation'}]
        with patch.object(managed_services, 'environment_definitions', return_value=accepted), \
             patch.object(managed_services, 'refresh_binding', side_effect=lambda *args: refreshed.append(args[1:])), \
             patch.object(provision, 'command', side_effect=command):
            result = provision.provision(self.db, 'second-operation', self.definition, root=self.root,
                                         docker='docker', compose='compose', authorize=lambda: None, health_timeout=0)
        self.assertEqual(result['state'], 'running')
        self.assertEqual(len(refreshed), 1)
        service_id, expected_id, current = refreshed[0]
        self.assertEqual((service_id, expected_id, current['container_id']), ('service-id', 'c'*64, 'c'*64))
        self.assertEqual(current['state'], 'stopped')
        self.assertGreaterEqual(current['observed'], first_binding['observed'])
        self.assertEqual(first_binding['state'], 'running')  # Original creation evidence is unchanged.
