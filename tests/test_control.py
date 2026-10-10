import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import yaml
import control
from store import connect

class ControlTests(unittest.TestCase):
    def test_yaml_alias_and_literal_dollars_do_not_change_sibling(self):
        source=yaml.safe_load('services:\n  app: &base\n    environment: {TOKEN: old}\n  queue: *base\n')
        after=control.mutate_service(source,'app',{'TOKEN':'new$secret','EMPTY':''})
        self.assertEqual(after['services']['queue']['environment'],{'TOKEN':'old'})
        self.assertEqual(after['services']['app']['environment']['TOKEN'],'new$$secret')
        self.assertEqual(source['services']['app']['environment']['TOKEN'],'old')
    def test_invalid_values(self):
        for changes in ({}, {'BAD-KEY':'x'}, {'OK':3}, {'OK':'x\x00y'}):
            with self.assertRaises(control.ControlError):control.validate_changes(changes)
    def test_path_guard_resolves_symlinks(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as other:
            root=Path(d);external=Path(other)/'secret';external.write_text('secret')
            (root/'link').symlink_to(external)
            with patch.object(control,'ROOT',root):
                with self.assertRaises(control.ControlError):control.checked_path(root/'link')
    def test_stopped_recreate_never_starts_service(self):
        with patch.object(control,'command') as command:
            control.recreate(['compose'],'app',False)
            args=command.call_args.args[0]
            self.assertEqual(args[1],'create');self.assertIn('--no-deps',args);self.assertIn('--no-build',args)
    def test_image_drift_rejected(self):
        with patch.object(control,'command',return_value='[{"Id":"new"}]'):
            with self.assertRaises(control.ControlError):control.assert_image({'services':{'app':{'image':'tag'}}},'app','old')
    def test_lock_released_when_database_unavailable(self):
        with patch.object(control,'LOCK',threading.Lock()),patch.object(control,'connect',side_effect=OSError()):
            with self.assertRaises(OSError):control.execute({'action':'container-start','request_id':'a'*20})
            self.assertFalse(control.LOCK.locked())
    def test_idempotency_and_no_secret_in_audit(self):
        with tempfile.TemporaryDirectory() as d:
            database=str(Path(d)/'state.sqlite')
            body={'action':'container-start','project':'app','container':'app-1','container_id':'id','request_id':'a'*20}
            c={'Id':'id','State':{'Running':False}}
            with patch.object(control,'DATABASE',database),patch.object(control,'JOURNAL',Path(d)/'journal'),patch.object(control,'inspect_target',return_value=(c,'app',[],[])),patch.object(control,'command') as cmd,patch.object(control,'wait_runtime'):
                self.assertEqual(control.execute(body)['status'],'succeeded')
                self.assertEqual(control.execute(body)['status'],'succeeded')
                self.assertEqual(cmd.call_count,1)
            db=connect(database)
            self.assertEqual(db.execute('select count(*) from control_runs').fetchone()[0],1);db.close()
    def test_stale_container_rejected_without_command(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(control,'DATABASE',str(Path(d)/'db')),patch.object(control,'JOURNAL',Path(d)/'journal'),patch.object(control,'inspect_target',return_value=({'Id':'new'},'app',[],[])),patch.object(control,'command') as cmd:
                with self.assertRaises(control.ControlError):control.execute({'action':'container-stop','project':'app','container':'app-1','container_id':'old','request_id':'b'*20})
                cmd.assert_not_called()
    def test_environment_save_and_rollback(self):
        for failure in (False,True):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as d:
                root=Path(d);file=root/'compose.yml';original='services:\n  app:\n    image: app:v1\n    environment:\n      TOKEN: old\n'
                file.write_text(original)
                c={'Id':'id','Image':'sha256:old','Config':{'Env':['TOKEN=old']},'State':{'Running':True}}
                config={'services':{'app':{'image':'app:v1','environment':{'TOKEN':'old'}}}}
                after=copy.deepcopy(config);after['services']['app']['environment']['TOKEN']='private-value'
                body={'action':'environment-save','project':'app','container':'app-1','container_id':'id','request_id':'c'*20,'changes':{'TOKEN':'private-value'},'revision':control.revision([file],c,config)}
                dbpath=str(root/'db');journal=root/'journal'
                with patch.object(control,'DATABASE',dbpath),patch.object(control,'ROOT',root),patch.object(control,'JOURNAL',journal),patch.object(control,'inspect_target',return_value=(c,'app',[file],['compose'])),patch.object(control,'assert_image'),patch.object(control,'configuration',side_effect=[config,config,config,after]+([config] if failure else [])),patch.object(control,'recreate'),patch.object(control,'wait_runtime',side_effect=[control.ControlError('failed'),c] if failure else [{'Image':'sha256:old','Config':{'Env':['TOKEN=private-value']}}]):
                    result=control.execute(body)
                self.assertEqual(result['status'],'rolled-back' if failure else 'succeeded')
                self.assertFalse(journal.exists())
                if failure:self.assertEqual(file.read_text(),original)
                else:self.assertEqual(yaml.safe_load(file.read_text())['services']['app']['environment']['TOKEN'],'private-value')
                db=connect(dbpath)
                for table in ('audit','control_runs'):
                    self.assertNotIn('private-value',str([tuple(r) for r in db.execute('SELECT * FROM '+table)]))
                db.close()
