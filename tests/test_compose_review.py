import json
import tempfile
import unittest
from pathlib import Path
from compose_review import review


class ComposeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.old=self.root/'old.yml';self.old.write_text('services: {}')
        self.new=self.root/'compose.yml';self.new.write_text('services: {}')
        self.env=self.root/'.env';self.env.write_text('PASSWORD=private-sentinel')
        self.recipe={'compose_project':'app','compose_files':[str(self.new)]}
        self.config={'name':'app','services':{'web':{'network_mode':'none','image':'app:fixed','environment':{'PASSWORD':'private-sentinel'}}}}
        self.calls=[]
    def command(self,args):
        self.calls.append(args)
        return json.dumps(self.config)
    def check(self,command=None,files=None):
        return review(self.recipe,'web',files or [str(self.old)],self.root,command or self.command,'compose')
    def test_equal_resolved_service_has_private_proof_not_values(self):
        result=self.check()
        self.assertTrue(result['equivalent'])
        self.assertIsNotNone(result['proof'])
        self.assertNotIn('private-sentinel',json.dumps(result))
        self.assertEqual(len(self.calls),4)
        self.assertTrue(all('--env-file' in call and call[-3:]==['config','--format','json'] for call in self.calls))
    def test_configuration_changed_during_review_rejects(self):
        def command(args):
            result=self.command(args)
            self.env.write_text('PASSWORD=changed')
            return result
        with self.assertRaisesRegex(ValueError,'changed during'):self.check(command)
    def test_outside_or_duplicate_paths_reject_before_execution(self):
        for files in (['/etc/passwd'],[str(self.old),str(self.old)]):
            with self.assertRaises(ValueError):self.check(files=files)
        self.assertEqual(self.calls,[])
    def test_failed_resolution_does_not_leak_diagnostic_values(self):
        def command(args):raise RuntimeError('private-sentinel')
        with self.assertRaisesRegex(ValueError,'could not be resolved') as caught:self.check(command)
        self.assertNotIn('private-sentinel',str(caught.exception))
    def test_different_project_directory_rejects(self):
        other=self.root/'other';other.mkdir();path=other/'compose.yml';path.write_text('services: {}')
        with self.assertRaisesRegex(ValueError,'different project directories'):self.check(files=[str(path)])
    def test_referenced_secret_and_config_bytes_change_proof_without_exposing_values(self):
        for category in ('secrets', 'configs'):
            with self.subTest(category=category):
                path = self.root / (category + '.txt')
                path.write_text('first-private-value')
                self.config[category] = {'fixture': {'file': str(path)}}
                self.config['services']['web'][category] = [{'source': 'fixture'}]
                first = self.check()
                path.write_text('second-private-value')
                second = self.check()
                self.assertTrue(first['equivalent'] and second['equivalent'])
                self.assertNotEqual(first['proof']['fingerprint'], second['proof']['fingerprint'])
                for result in (first, second):
                    self.assertNotIn('first-private-value', json.dumps(result))
                    self.assertNotIn('second-private-value', json.dumps(result))

    def test_referenced_secret_changed_during_second_resolution_rejects(self):
        path = self.root / 'secret.txt'
        path.write_text('first-private-value')
        self.config['secrets'] = {'fixture': {'file': str(path)}}
        self.config['services']['web']['secrets'] = [{'source': 'fixture'}]
        def command(args):
            result = self.command(args)
            if len(self.calls) == 3:
                path.write_text('changed-private-value')
            return result
        with self.assertRaisesRegex(ValueError, 'Referenced configuration changed'):
            self.check(command)
