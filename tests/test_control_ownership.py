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
