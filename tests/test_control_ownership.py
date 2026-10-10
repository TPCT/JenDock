import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import control

class ControlOwnershipTests(unittest.TestCase):
    def test_compose_group_and_exact_files_must_match_registered_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);owned=root/'owned.yml';foreign=root/'foreign.yml'
            owned.write_text('services: {}');foreign.write_text('services: {}')
            config=root/'projects.json'
            config.write_text(json.dumps({'owned':{'repository':'TPCT/owned','branch':'development','container_prefixes':['owned-'],'recipe':{'compose_project':'owned','compose_files':[str(owned)]}}}))
            with patch.object(control,'DATABASE',str(root/'db')),patch.object(control,'CONFIG',str(config)),patch.object(control,'ROOT',root):
                for group,file,valid in [('owned',owned,True),('foreign',owned,False),('owned',foreign,False)]:
                    payload=[{'Name':'/owned-app-1','Config':{'Labels':{'com.docker.compose.project':group,'com.docker.compose.service':'app','com.docker.compose.project.config_files':str(file)}}}]
                    with self.subTest(group=group,file=file.name),patch.object(control,'command',return_value=json.dumps(payload)):
                        if valid:self.assertEqual(control.inspect_target('owned','owned-app-1')[1],'app')
                        else:
                            with self.assertRaises(control.ControlError):control.inspect_target('owned','owned-app-1')

    def test_reversed_and_duplicate_files_cannot_authorize_container_control(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = [root / 'base.yml', root / 'override.yml']
            for file in files: file.write_text('services: {}')
            config = root / 'projects.json'
            config.write_text(json.dumps({'owned': {'repository': 'TPCT/owned', 'branch': 'main',
                'container_prefixes': ['owned-'], 'recipe': {'compose_project': 'owned', 'compose_files': list(map(str, files))}}}))
            with patch.object(control, 'DATABASE', str(root / 'db')), patch.object(control, 'CONFIG', str(config)), patch.object(control, 'ROOT', root):
                for actual, allowed in [(files, True), (files[::-1], False), (files + files[:1], False)]:
                    payload = [{'Name': '/owned-app-1', 'Config': {'Labels': {
                        'com.docker.compose.project': 'owned', 'com.docker.compose.service': 'app',
                        'com.docker.compose.project.config_files': ','.join(map(str, actual))}}}]
                    with self.subTest(actual=actual), patch.object(control, 'command', return_value=json.dumps(payload)):
                        if allowed: self.assertEqual(control.inspect_target('owned', 'owned-app-1')[2], files)
                        else:
                            with self.assertRaises(control.ControlError): control.inspect_target('owned', 'owned-app-1')

    def test_verified_alternate_invocation_revalidates_and_returns_canonical_command(self):
        self.exercise_provenance()

    def test_canonical_container_still_revalidates_its_recorded_alternate_proof(self):
        self.exercise_provenance(canonical_labels=True)

    def test_configuration_drift_rejects_even_when_resolved_service_stays_equal(self):
        self.exercise_provenance(drift=True)

    def exercise_provenance(self, *, canonical_labels=False, drift=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, canonical, env = root / 'old.yml', root / 'compose.yml', root / 'configured.env'
            resolved = {'services': {'app': {'image': 'fixture:fixed', 'network_mode': 'none'}}}
            old.write_text(json.dumps(resolved)); canonical.write_text(json.dumps(resolved))
            env.write_text('PRIVATE_TOKEN=synthetic-private-value\n')
            recipe = {'compose_project': 'owned', 'compose_files': [str(canonical)],
                      'env_file': str(env), 'services': ['app'], 'owned_services': ['app']}
            calls = []
            def command(argv, **kwargs):
                calls.append(argv)
                if argv[1] == 'inspect':
                    return json.dumps([{'Id': 'a' * 64, 'Name': '/owned-app-1', 'Config': {'Labels': {
                        'com.docker.compose.project': 'owned', 'com.docker.compose.service': 'app',
                        'com.docker.compose.project.config_files': str(canonical if canonical_labels else old)}}}])
                self.assertEqual(argv[-3:], ['config', '--format', 'json'])
                return json.dumps(resolved)
            policy = {'container_prefixes': ['owned-'], 'recipe': recipe}
            with patch.object(control, 'ROOT', root), patch.object(control, 'DATABASE', str(root / 'state.sqlite')), \
                 patch.object(control, 'command', side_effect=command), \
                 patch.object(control, 'effective_projects', return_value={'owned': policy}):
                # Real resolver and proof generation/validation, only Docker I/O stubbed.
                proof = control.review_compose(recipe, 'app', [str(old)])['proof']
                recipe['service_provenance'] = {'app': proof}
                calls.clear()
                if drift:
                    # Semantically unchanged JSON, but the reviewed file bytes changed.
                    old.write_text(old.read_text() + '\n')
                    with self.assertRaisesRegex(control.ControlError, 'provenance changed'):
                        control.inspect_target('owned', 'owned-app-1')
                    self.assertFalse(any('up' in argv or 'start' in argv for argv in calls))
                else:
                    container, service, files, argv = control.inspect_target('owned', 'owned-app-1')
                    self.assertEqual(container['Id'], 'a' * 64)
                    self.assertEqual(service, 'app')
                    self.assertEqual(files, [canonical])
                    self.assertEqual(argv[argv.index('-f') + 1], str(canonical))
                    self.assertNotIn(str(old), argv)
                    self.assertEqual(argv[argv.index('--env-file') + 1], str(env))
                    config_calls = [call for call in calls if call[-3:] == ['config', '--format', 'json']]
                    self.assertEqual(len(config_calls), 4)
                    self.assertEqual(sum(str(old) in call for call in config_calls), 2)
                    self.assertEqual(sum(str(canonical) in call for call in config_calls), 2)

    def test_exact_adopted_recipe_rejects_service_outside_owned_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); canonical = root / 'compose.yml'
            canonical.write_text('services: {}')
            recipe = {'compose_project': 'owned', 'compose_files': [str(canonical)],
                      'services': ['app'], 'owned_services': ['app', 'db']}
            policy = {'container_prefixes': ['owned-'], 'environment': 'production', 'recipe': recipe}
            actual = [{'Id': 'a' * 64, 'Name': '/owned-unadopted-1', 'Config': {'Labels': {
                'com.docker.compose.project': 'owned', 'com.docker.compose.service': 'unadopted',
                'com.docker.compose.project.config_files': str(canonical)}}}]
            with patch.object(control, 'ROOT', root), patch.object(control, 'DATABASE', str(root / 'state.sqlite')), \
                 patch.object(control, 'effective_projects', return_value={'owned': policy}), \
                 patch.object(control, 'command', return_value=json.dumps(actual)):
                with self.assertRaises(control.ControlError):
                    control.inspect_target('owned', 'owned-unadopted-1')
