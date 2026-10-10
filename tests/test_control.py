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
    def test_resource_change_preserves_sibling_alias_and_unrelated_settings(self):
        source=yaml.safe_load('services:\n  app: &base\n    image: app:v1\n    mem_limit: 64m\n    environment: {KEY: old}\n  queue: *base\n')
        after=control.mutate_resources(source,'app',{'memory_mib':128,'cpu':0.5})
        self.assertEqual(after['services']['queue'],source['services']['queue'])
        self.assertEqual(after['services']['app']['mem_limit'],134217728)
        self.assertEqual(after['services']['app']['memswap_limit'],134217728)
        self.assertEqual(after['services']['app']['deploy']['resources']['limits']['memory'], '134217728')
        self.assertEqual(control.without_resource_limits(after,'app'),control.without_resource_limits(source,'app'))

    def test_resource_comparison_ignores_compose_empty_placement_only(self):
        source = {'services': {'app': {'image': 'app:v1'}}}
        normalized = copy.deepcopy(source)
        normalized['services']['app']['deploy'] = {'placement': {}, 'resources': {'limits': {'memory': '134217728', 'cpus': '0.5'}}}
        self.assertEqual(control.without_resource_limits(normalized, 'app'), source)
        normalized['services']['app']['deploy']['placement'] = {'constraints': ['node.role==manager']}
        self.assertNotEqual(control.without_resource_limits(normalized, 'app'), source)

    def test_resource_limits_support_both_docker_cpu_encodings(self):
        for host in ({'Memory':134217728,'NanoCpus':500000000},{'Memory':134217728,'CpuPeriod':100000,'CpuQuota':50000}):
            self.assertEqual(control.resource_limits({'HostConfig':host})['cpu'],0.5)
            self.assertEqual(control.resource_limits({'HostConfig':host})['memory_mib'],128)

    def test_resource_apply_persists_verifies_and_restores_on_failure(self):
        from resource_policy import read,save
        for failure in ('none', 'runtime', 'swap-mismatch'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as d:
                root=Path(d);file=root/'compose.yml';original='services:\n  app:\n    image: app:v1\n    mem_limit: 64m\n    cpus: 0.25\n'
                file.write_text(original);database=str(root/'db');db=connect(database)
                doc=read(db,'app')['document'];doc['budget']={'memory_mib':128,'cpu':0.5}
                doc['environments']['production']={'budget':{'memory_mib':128,'cpu':0.5},'services':{'app-1':{'memory_mib':128,'cpu':0.5}}}
                save(db,'app',0,doc);db.close()
                c={'Id':'id','Image':'sha256:old','Config':{'Env':[]},'State':{'Running':False},'HostConfig':{'Memory':67108864,'NanoCpus':250000000}}
                config=yaml.safe_load(original);after=control.mutate_resources(config,'app',{'memory_mib':128,'cpu':0.5})
                body={'action':'resource-apply','project':'app','environment':'production','policy_revision':1,'container':'app-1','container_id':'id','request_id':'r'*20,'revision':control.revision([file],c,config)}
                updated=copy.deepcopy(c);updated['HostConfig']={'Memory':134217728,'MemorySwap':268435456 if failure == 'swap-mismatch' else 134217728,'NanoCpus':500000000}
                with patch.object(control,'DATABASE',database),patch.object(control,'ROOT',root),patch.object(control,'JOURNAL',root/'journal'),patch.object(control,'inspect_target',return_value=(c,'app',[file],['compose'])),patch.object(control,'assert_image'),patch.object(control,'configuration',side_effect=[config,config,config,after]+([config] if failure != 'none' else [])),patch.object(control,'recreate') as recreate,patch.object(control,'wait_runtime',side_effect=[control.ControlError('failed'),c] if failure == 'runtime' else [updated,c] if failure == 'swap-mismatch' else [updated]):
                    result=control.execute(body)
                self.assertEqual(result['status'],'rolled-back' if failure != 'none' else 'succeeded')
                self.assertFalse((root/'journal').exists())
                self.assertFalse(recreate.call_args.args[-1])
                if failure != 'none':self.assertEqual(file.read_text(),original)
                else:self.assertEqual(yaml.safe_load(file.read_text())['services']['app']['cpus'],0.5)
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
            self.assertIn('--no-start',args);self.assertIn('--no-deps',args);self.assertIn('--no-build',args)
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

    def test_resource_policy_change_between_preflight_and_execution_does_not_mutate(self):
        import resource_policy
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            file = root / 'compose.yml'
            original = 'services:\n  app:\n    image: app:v1\n'
            file.write_text(original)
            database = root / 'db'
            db = connect(database)
            document = resource_policy.read(db, 'app')['document']
            document['budget'] = {'memory_mib': 128, 'cpu': 0.5}
            document['environments']['production'] = {
                'budget': {'memory_mib': 128, 'cpu': 0.5},
                'services': {'app-1': {'memory_mib': 128, 'cpu': 0.5}},
            }
            resource_policy.save(db, 'app', 0, document)
            db.close()
            container = {'Id': 'id', 'Image': 'image', 'State': {'Running': False}}
            config = yaml.safe_load(original)
            body = {'action': 'resource-apply', 'project': 'app', 'environment': 'production',
                    'policy_revision': 1, 'container': 'app-1', 'container_id': 'id',
                    'request_id': 'race-request-12345678', 'revision': control.revision([file], container, config)}
            original_allocation = resource_policy.allocation
            first_call = True
            def concurrent_edit(*args):
                nonlocal first_call
                limits = original_allocation(*args)
                if first_call:
                    first_call = False
                    other = connect(database)
                    try:
                        changed = copy.deepcopy(document)
                        changed['environments']['production']['services']['app-1']['cpu'] = 0.25
                        resource_policy.save(other, 'app', 1, changed)
                    finally:
                        other.close()
                return limits
            with patch.object(control, 'DATABASE', database), patch.object(control, 'ROOT', root), \
                 patch.object(control, 'JOURNAL', root / 'journal'), \
                 patch.object(control, 'inspect_target', return_value=(container, 'app', [file], ['compose'])), \
                 patch.object(control, 'configuration', return_value=config), patch.object(control, 'assert_image'), \
                 patch.object(resource_policy, 'allocation', side_effect=concurrent_edit), \
                 patch.object(control, 'recreate') as recreate:
                result = control.execute(body)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(file.read_text(), original)
            self.assertFalse((root / 'journal').exists())
            recreate.assert_not_called()
