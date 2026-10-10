"""Docker observations expose exact ownership without publishing arbitrary labels."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import worker
from store import connect


class WorkerObservationTests(unittest.TestCase):
    def observe(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(Path(directory) / 'state.sqlite')
            try:
                with patch.object(worker, 'run', side_effect=[
                    '\n'.join(json.dumps(row) for row in rows),
                    json.dumps({'Name': 'app-web-1', 'CPUPerc': '1.20%', 'MemUsage': '8MiB / 128MiB',
                                'MemPerc': '6.25%', 'PIDs': '2', 'Secret': 'stats-secret'}),
                ]) as command, patch.object(worker.shutil, 'disk_usage', return_value=SimpleNamespace(total=100, free=50)):
                    worker.observe(db)
                result = json.loads(db.execute("SELECT value FROM snapshots WHERE name='containers'").fetchone()[0])
                return result, command.call_args_list[0].args[0]
            finally:
                db.close()

    def test_exact_ownership_and_safe_display_fields_only(self):
        row = {'ID': 'a' * 64, 'Names': 'app-web-1', 'Image': 'app:main', 'State': 'running',
               'Status': 'Up 1 hour', 'ComposeProject': 'app', 'ComposeService': 'web',
               'ComposeConfigFiles': '/managed/base.yml,/managed/override.yml',
               'Labels': 'password=private', 'Config': {'Env': ['TOKEN=private']}}
        result, command = self.observe([row])
        self.assertEqual(len(result), 1)
        expected = {key: value for key, value in row.items() if key not in ('Labels', 'Config')}
        expected.update(CPUPerc='1.20%', MemUsage='8MiB / 128MiB', MemPerc='6.25%', PIDs='2')
        self.assertEqual(result[0], expected)
        self.assertIn('--no-trunc', command)
        template = command[command.index('--format') + 1]
        for label in ('com.docker.compose.project', 'com.docker.compose.service', 'com.docker.compose.project.config_files'):
            self.assertIn('.Label "' + label + '"', template)
        self.assertNotIn('.Labels', template)
        self.assertNotIn('{{json .}}', template)
        self.assertNotIn('private', json.dumps(result))

    def test_missing_or_wrong_type_labels_do_not_infer_ownership_from_name(self):
        result, _ = self.observe([{'ID': 'b' * 64, 'Names': 'production-web-1',
                                   'ComposeProject': {}, 'ComposeConfigFiles': ['unexpected']}])
        for key in ('ComposeProject', 'ComposeService', 'ComposeConfigFiles'):
            self.assertIsNone(result[0][key])

    def test_excluded_test_workload_stays_excluded(self):
        result, _ = self.observe([{'ID': 'c' * 64, 'Names': 'deploy-test-web-1'}])
        self.assertEqual(result, [])
