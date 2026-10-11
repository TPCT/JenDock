import copy
import json
import unittest
from unittest.mock import patch
import service_provisioning as provision
import service_runtime
import workload_environment
import build_profiles
import test_service_provisioning as fixtures
from test_workload_role_profiles import role_values


class RoleRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ProvisioningTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.definition=copy.deepcopy(self.f.definition)
        profile=build_profiles.role_profile('laravel-database-queue-v1',role_values(),'app','65534:65534')
        self.definition['artifact'].update({k:v for k,v in profile.items() if k not in ('revision','image_name')})
    def test_metadata_does_not_modify_definition_and_grace_matches_accepted_setting(self):
        before=copy.deepcopy(self.definition)
        spec=provision.service_config(self.definition,'operation-id')
        self.assertEqual(self.definition,before)
        self.assertEqual(spec['environment'],workload_environment.metadata(self.definition))
        self.assertEqual(spec['stop_grace_period'],'70s')
        self.assertEqual(json.loads(spec['environment']['DEPLOY_MONITOR_WORKLOAD']),self.definition['artifact']['workload'])
        changed=copy.deepcopy(self.definition);changed['resources']['memory_mib']=80
        self.assertNotEqual(provision.service_config(changed,'operation-id')['environment']['DEPLOY_MONITOR_DEFINITION_DIGEST'],spec['environment']['DEPLOY_MONITOR_DEFINITION_DIGEST'])
    def test_context_verifies_all_role_env_even_without_runtime_values(self):
        values={'PATH':'/usr/bin',**workload_environment.metadata(self.definition)}
        actual={'Config':{'Env':[k+'='+v for k,v in values.items()]}}
        for context in [service_runtime.Context(self.f.db,lambda:None),service_runtime.StoppingContext(self.f.db,lambda:None)]:
            context.verify(self.definition,actual,['PATH=/usr/bin'])
            for key in workload_environment.metadata(self.definition):
                changed=copy.deepcopy(actual);changed['Config']['Env']=[entry if not entry.startswith(key+'=') else key+'=forged' for entry in changed['Config']['Env']]
                with self.subTest(key=key,context=type(context).__name__),self.assertRaises(ValueError):context.verify(self.definition,changed,['PATH=/usr/bin'])
    def test_docker_stop_timeout_and_metadata_are_verified(self):
        # Build an otherwise valid ordinary container fixture then apply role config.
        self.f.execute();row=copy.deepcopy(self.f.container);spec=provision.service_config(self.definition,'operation-id')
        row['Config'].update(WorkingDir='/app',Labels={**row['Config']['Labels'],**spec['labels']},Entrypoint=[self.definition['artifact']['command'][0]],Cmd=self.definition['artifact']['command'][1:],Healthcheck={'Test':['CMD',*self.definition['artifact']['healthcheck']]},StopTimeout=70,Env=[k+'='+v for k,v in spec['environment'].items()])
        context=service_runtime.Context(self.f.db,lambda:None)
        args=(row,self.definition,'operation-id',row['Image'],self.f.network['Id'],'example-development-app-1',self.f.root/'example/development/compose.json')
        row['HostConfig']['Tmpfs']={'/tmp':'rw,noexec,nosuid,size=16m'}
        with patch.object(provision,'_json',return_value=[{'Id':row['Image'],'Config':{'WorkingDir':'/app'}}]):
            provision._verify(*args,runtime_context=context,docker='docker')
        row['Config']['StopTimeout']=5
        with patch.object(provision,'_json',return_value=[{'Id':row['Image'],'Config':{'WorkingDir':'/app'}}]),self.assertRaises(provision.ProvisioningError):
            provision._verify(*args,runtime_context=context,docker='docker')


    def test_workload_layout_rejects_writable_code_and_changed_php_directory(self):
        actual={'Config':{'WorkingDir':'/app'},'HostConfig':{'Tmpfs':{'/tmp':'rw,noexec,nosuid,size=16m'}}}
        provision.verify_role_layout(actual,{'WorkingDir':'/app'})
        for options in [{'WorkingDir':'/tmp'}, {'Tmpfs':{}}, {'Tmpfs':{'/tmp':'rw,nosuid,size=16m'}},
                        {'Tmpfs':{'/tmp':'rw,noexec,nosuid,size=16m','/app':'rw'}},
                        {'Tmpfs':{'/tmp':'rw,noexec,nosuid,size=64m'}}]:
            changed=copy.deepcopy(actual)
            changed['Config' if 'WorkingDir' in options else 'HostConfig'].update(options)
            with self.subTest(options=options),self.assertRaises(provision.ProvisioningError):
                provision.verify_role_layout(changed,{'WorkingDir':'/app'})
        actual['HostConfig']['Tmpfs']['/tmp']='nosuid,size=16777216,rw,noexec'
        provision.verify_role_layout(actual,{'WorkingDir':'/app'})

    def test_lifecycle_honors_role_grace_and_command_deadline(self):
        import managed_lifecycle as lifecycle
        service={'id':self.definition['service_id'],'definition':self.definition,'binding':{'container_id':'c'*64}}
        row={'state':'accepted','service_id':service['id'],'captured':{'action':'stop','target':'stopped','peers':[service]}}
        with patch.object(lifecycle,'load',return_value=row),patch.object(lifecycle,'observe',side_effect=[{'State':{'Running':True}},{'State':{'Running':False}}]),patch.object(lifecycle,'_authority'),patch.object(lifecycle,'_write'),patch.object(lifecycle,'_commit',return_value={'status':'succeeded'}),patch.object(provision,'command') as command:
            self.assertEqual(lifecycle.apply(self.f.db,'op',docker='docker',authorize=lambda:None)['status'],'succeeded')
        command.assert_called_once_with(['docker','stop','--time','70','c'*64],timeout=100)
