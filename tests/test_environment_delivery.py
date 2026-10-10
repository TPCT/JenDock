import json
import time
import unittest

import test_receiver
from store import connect, snapshot, enqueue
import registry
import job_target
from delivery_target import capture_registered_target


class EnvironmentDeliveryTests(unittest.TestCase):
    deliver = test_receiver.ReceiverTests.deliver
    def setUp(self):
        test_receiver.ReceiverTests.setUp(self)
        self.recipe = {'compose_project': 'test', 'compose_files': ['/managed/test/compose.yml'], 'services': ['web'],
                       'images': [{'name': 'app'}], 'tests': [{'command': ['true']}], 'http_checks': [{'url': 'http://example.test', 'statuses': [200]}]}
        policy = json.loads(self.config.read_text()); policy['test']['recipe'] = self.recipe
        self.config.write_text(json.dumps(policy))
        self.app.state()
        self.db = connect(self.app.database)
        registry.update_project(self.db, 'test', 1, {'legacy_binding': 'production'})
        recipe = dict(self.recipe, compose_project='test-development', compose_files=['/managed/development/compose.yml'])
        import hashlib
        encoded = registry.canonical(recipe)
        cursor = self.db.execute('INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,?)',
                                 ('test', hashlib.sha256(encoded.encode()).hexdigest(), encoded, time.time()))
        self.dev_recipe = cursor.lastrowid
        self.db.execute("UPDATE registry_environments SET recipe_revision=?,state='running' WHERE project='test' AND name='development'", (self.dev_recipe,))
        snapshot(self.db, 'containers', [self.container('test'), self.container('test-development')])
        snapshot(self.db, 'worker', {'state': 'running'})
        snapshot(self.db, 'branches:test', {'branches': {'main': 'a' * 40, 'development': 'b' * 40}, 'error': False})
        _, _, token = self.app.login({'username': 'admin', 'password': 'local-test-only'}, 'fixture')
        self.session = self.app.session(self.app.cookie_name + '=' + token)

    def tearDown(self): self.db.close(); test_receiver.ReceiverTests.tearDown(self)

    def container(self, group):
        return {'ID': ('a' if group == 'test' else 'b') * 64, 'Names': group + '-web', 'State': 'running',
                'ComposeProject': group, 'ComposeService': 'web',
                'ComposeConfigFiles': '/managed/test/compose.yml' if group == 'test' else '/managed/development/compose.yml'}

    def request(self, environment='development', **changes):
        record = registry.get_project(self.db, 'test'); env = record['environments'][environment]
        return dict({'action': 'build', 'project': 'test', 'environment': environment,
                     'project_revision': record['revision'], 'environment_revision': env['revision'], 'recipe_revision': env['recipe_revision'],
                     'branch': env['branch'], 'sha': ('a' if env['branch'] == 'main' else 'b') * 40, 'mode': 'deploy',
                     'request_id': 'environment-request-' + environment}, **changes)

    def test_manual_target_uses_requested_environment_and_exact_recipe(self):
        code, result = self.app.action(self.session, self.request())
        self.assertEqual(code, 202, result)
        target = job_target.load(self.db, result['job_id'])
        self.assertEqual(target['environment'], 'development')
        self.assertEqual(target['recipe']['compose_project'], 'test-development')
        self.assertEqual(self.app.state()['jobs'][0]['target']['environment'], 'development')

    def test_stale_review_or_cross_environment_replay_is_rejected(self):
        body = self.request(environment_revision=999)
        self.assertEqual(self.app.action(self.session, body)[0], 409)
        body = self.request()
        self.assertEqual(self.app.action(self.session, body)[0], 202)
        cross = self.request('production', request_id=body['request_id'], branch=body['branch'], sha=body['sha'])
        self.assertEqual(self.app.action(self.session, cross)[0], 409)

    def test_stopped_environment_can_validate_but_cannot_deploy_or_wake(self):
        rows = [self.container('test'), dict(self.container('test-development'), State='exited')]
        snapshot(self.db, 'containers', rows)
        code, result = self.app.action(self.session, self.request())
        self.assertEqual(code, 409, result)
        self.assertEqual(self.app.action(self.session, self.request(mode='validate'))[0], 202)

    def test_each_environment_queues_independently(self):
        self.assertEqual(self.app.action(self.session, self.request())[0], 202)
        code, result = self.app.action(self.session, self.request('production'))
        self.assertEqual(code, 202, result)

    def test_webhook_routes_each_configured_environment_without_legacy_duplicate(self):
        code, result = self.deliver(dict(self.payload, ref='refs/heads/development', after='b' * 40))
        self.assertEqual(code, 202, result)
        self.assertEqual(len(result['jobs']), 1)
        self.assertEqual(job_target.public(self.db, result['jobs'][0])['environment'], 'development')
        self.assertEqual(self.deliver(dict(self.payload, ref='refs/heads/development', after='b' * 40))[1]['jobs'], result['jobs'])

    def test_same_branch_webhooks_do_not_supersede_sibling_environment(self):
        self.db.execute("UPDATE registry_environments SET branch='main' WHERE project='test' AND name='development'")
        code, result = self.deliver()
        self.assertEqual(code, 202, result)
        self.assertEqual(len(result['jobs']), 2)
        self.assertEqual({row['status'] for row in self.db.execute('SELECT status FROM jobs')}, {'queued'})

    def test_inactive_webhook_records_skip_without_waking(self):
        snapshot(self.db, 'containers', [self.container('test'), dict(self.container('test-development'), State='exited')])
        code, result = self.deliver(dict(self.payload, ref='refs/heads/development', after='b' * 40))
        self.assertEqual(code, 202, result)
        self.assertEqual(result['jobs'], [])
        self.assertTrue(result['skipped'])

    def test_webhook_replay_keeps_original_target_after_settings_revision_changes(self):
        payload = dict(self.payload, ref='refs/heads/development', after='b' * 40)
        first = self.deliver(payload)[1]
        original = job_target.load(self.db, first['jobs'][0])
        registry.update_environment(self.db, 'test', 'development', 1, {'automatic': False})
        repeated = self.deliver(payload)[1]
        self.assertEqual(repeated['jobs'], first['jobs'])
        self.assertFalse(repeated['new'])
        self.assertEqual(job_target.load(self.db, first['jobs'][0]), original)

    def test_disabled_webhook_records_target_without_waking_stopped_environment(self):
        registry.update_environment(self.db, 'test', 'development', 1, {'automatic': False})
        snapshot(self.db, 'containers', [self.container('test'), dict(self.container('test-development'), State='exited')])
        result = self.deliver(dict(self.payload, ref='refs/heads/development', after='b' * 40))[1]
        self.assertEqual(len(result['jobs']), 1)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()['status'], 'auto-deploy-disabled')
        self.assertEqual(job_target.public(self.db, result['jobs'][0])['environment'], 'development')

    def test_not_provisioned_cannot_deploy_even_if_matching_containers_exist(self):
        self.db.execute("UPDATE registry_environments SET state='not-provisioned' WHERE project='test' AND name='development'")
        code, result = self.app.action(self.session, self.request())
        self.assertEqual(code, 409, result)
        self.assertIn('not provisioned', result['error'])

    def test_retry_cannot_switch_original_environment(self):
        code, first = self.app.action(self.session, self.request())
        self.assertEqual(code, 202, first)
        self.db.execute("UPDATE jobs SET status='failed' WHERE id=?", (first['job_id'],))
        body = self.request('production', action='retry', job_id=first['job_id'])
        code, result = self.app.action(self.session, body)
        self.assertEqual(code, 409, result)
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
