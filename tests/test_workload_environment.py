import copy
import hashlib
import unittest
import registry
import workload_environment as env

class WorkloadEnvironmentTests(unittest.TestCase):
    def definition(self):
        return {'project':'app','environment':'development','service_id':'service-1',
                'artifact':{'workload':{'role':'queue','configuration':{'queues':['default']}}}}
    def test_metadata_is_derived_without_mutating_definition(self):
        definition=self.definition();before=copy.deepcopy(definition)
        values=env.metadata(definition)
        self.assertEqual(definition,before)
        self.assertEqual(values['DEPLOY_MONITOR_DEFINITION_DIGEST'],hashlib.sha256(registry.canonical(definition).encode()).hexdigest())
        self.assertEqual(values['DEPLOY_MONITOR_WORKLOAD'],registry.canonical(definition['artifact']['workload']))
        for key,field in [('PROJECT','project'),('ENVIRONMENT','environment'),('SERVICE_ID','service_id')]:
            self.assertEqual(values['DEPLOY_MONITOR_'+key],definition[field])
        changed=copy.deepcopy(definition);changed['environment']='production'
        self.assertNotEqual(env.metadata(changed),values)
    def test_application_has_no_instrumentation_metadata(self):
        self.assertEqual(env.metadata({'artifact':{}}),{})
    def test_observed_metadata_never_becomes_mutable_runtime_input(self):
        values={'PATH':'/bin',**env.metadata(self.definition()),'DEPLOY_MONITOR_FUTURE':'private'}
        self.assertEqual(env.user_values(values),{'PATH':'/bin'})
        self.assertIn('DEPLOY_MONITOR_PROJECT',values)
    def test_reserved_edits_rejected_even_unknown_future_keys(self):
        for key in ('DEPLOY_MONITOR_PROJECT','DEPLOY_MONITOR_FUTURE'):
            with self.assertRaisesRegex(ValueError,'workload definition'):
                env.validate_user_values({key:'spoofed'})
        env.validate_user_values({'PATH':'/bin'})
