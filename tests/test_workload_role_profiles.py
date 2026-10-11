import copy
import unittest
import build_profiles as profiles
import test_build_profiles as fixtures


def role_values(role='queue'):
    return {'framework_version':'12.1.0','instrumentation':{'version':'1.0.0','digest':'a'*64},
            'configuration':({'connection':'pgsql','queues':['default','mail'],'timeout_seconds':60,
                'retry_after_seconds':90,'max_attempts':3,'sleep_seconds':1,'shutdown_grace_seconds':70}
                if role=='queue' else {'timezone':'Africa/Cairo','scope':'default','connection':'pgsql'})}


class WorkloadRoleProfileTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.BuildProfileTests();self.f.setUp();self.addCleanup(self.f.tearDown)
    def test_version3_roundtrip_and_legacy_application_identity(self):
        old=self.f.save(); old_recipe=profiles.get_private(self.f.db,'app','development')['recipe']
        values=dict(self.f.values,role_profiles={'laravel-database-queue-v1':role_values(),
                                               'laravel-scheduler-v1':role_values('scheduler')})
        saved=self.f.save(values);recipe=profiles.get_private(self.f.db,'app','development')['recipe']
        self.assertEqual(saved['values'],values)
        self.assertEqual(recipe['schema_version'],3)
        self.assertEqual(recipe['service_profiles']['application-v1'],old_recipe['service_profiles']['application-v1'])
        self.assertEqual(profiles.validate_recipe(recipe),recipe)
        queue=recipe['service_profiles']['laravel-database-queue-v1']
        self.assertEqual(queue['workload']['configuration'],values['role_profiles']['laravel-database-queue-v1']['configuration'])
        self.assertEqual(queue['command'][:4],['php','artisan','queue:work','pgsql'])
        self.assertEqual(profiles.validate_recipe(old_recipe),old_recipe)
    def test_closed_typed_settings_and_immutable_argv(self):
        base=role_values()
        for change in [{'timeout_seconds':True},{'timeout_seconds':90},{'shutdown_grace_seconds':59},
                       {'queues':['default','default']},{'queues':['--unsafe']},{'connection':'../secret'},
                       {'max_attempts':0},{'shell':'sh'}]:
            role=copy.deepcopy(base);role['configuration'].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.f.save(dict(self.f.values,role_profiles={'laravel-database-queue-v1':role}))
        for change in [{'framework_version':'13.0.0'},{'instrumentation':{'version':'1.0.0','digest':'latest'}},
                       {'command':['sh']}]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.f.save(dict(self.f.values,role_profiles={'laravel-database-queue-v1':dict(base,**change)}))
        self.f.save(dict(self.f.values,role_profiles={'laravel-database-queue-v1':base}))
        recipe=profiles.get_private(self.f.db,'app','development')['recipe']
        recipe['service_profiles']['laravel-database-queue-v1']['command']=['sh']
        with self.assertRaises(ValueError):profiles.validate_recipe(recipe)
    def test_scheduler_timezone_and_single_environment_scope(self):
        for config in [{'timezone':'Unknown/Zone','scope':'default'},{'timezone':'UTC','scope':'another'},
                       {'timezone':'UTC','scope':'default','command':'run'}]:
            with self.subTest(config=config),self.assertRaises(ValueError):
                self.f.save(dict(self.f.values,role_profiles={'laravel-scheduler-v1':dict(role_values('scheduler'),configuration=config)}))
