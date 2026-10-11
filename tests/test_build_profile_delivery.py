import json
import unittest

import build_profiles
import job_target
import registry
import test_receiver
import workspace
from store import connect, snapshot


class BuildProfileDeliveryTests(unittest.TestCase):
    deliver = test_receiver.ReceiverTests.deliver

    def setUp(self):
        test_receiver.ReceiverTests.setUp(self)
        self.app.state(); self.db = connect(self.app.database)
        self.values = {'dockerfile':'Dockerfile','context':'.','target':None,'test_command':['app','test'],
                       'command':['app','serve'],'healthcheck':['app','health'],'user':'65534:65534'}
        self.save_profile()
        snapshot(self.db,'branches:test',{'branches':{'development':'b'*40,'main':'a'*40},'error':False})
        self.session = {'username':'admin'}

    def tearDown(self): self.db.close(); test_receiver.ReceiverTests.tearDown(self)

    def save_profile(self, values=None, environment='development'):
        old = build_profiles.get(self.db,'test',environment)
        return build_profiles.save(self.db,'test',environment,old['project_revision'],old['environment_revision'],old['profile_revision'],values or self.values)

    def request(self, **changes):
        source = build_profiles.get(self.db,'test','development')
        return dict({'action':'build','project':'test','environment':'development','project_revision':source['project_revision'],
            'environment_revision':source['environment_revision'],'source_revision':source['source_revision'],
            'branch':'development','sha':'b'*40,'mode':'validate','request_id':'source-build-request-123'},**changes)

    def test_manual_source_build_pins_private_recipe_and_preserves_unprovisioned_state(self):
        before = registry.get_environment(self.db,'test','development')
        code, result = self.app.action(self.session,self.request())
        self.assertEqual(code,202,result)
        target = job_target.load(self.db,result['job_id'])
        self.assertEqual(target['recipe']['schema_kind'],'artifact-build')
        self.assertEqual(target['mode'],'validate'); self.assertNotIn('compose_files',target['recipe'])
        public = self.app.state()['jobs'][0]['target']
        self.assertEqual(public['source_revision'],target['recipe_revision']); self.assertNotIn('recipe',public)
        self.assertEqual(registry.get_environment(self.db,'test','development'),before)
        current = workspace.read(self.db,'test','development')
        self.assertTrue(current['capabilities']['build']['enabled'])
        self.assertFalse(current['capabilities']['deploy']['enabled'])
        self.assertIsNone(current['environment']['recipe_revision'])

    def test_stale_review_deploy_and_mixed_source_runtime_requests_reject_without_queue(self):
        old = self.request(); self.save_profile(dict(self.values,target='release'))
        for body in (old,self.request(mode='deploy'),self.request(recipe_revision=1),self.request(source_revision=999)):
            with self.subTest(body=body): self.assertEqual(self.app.action(self.session,body)[0],409)
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)

    def test_lost_response_replay_keeps_old_snapshot_after_profile_change(self):
        body = self.request(); code, first = self.app.action(self.session,body)
        self.assertEqual(code,202,first); original = job_target.load(self.db,first['job_id'])
        self.save_profile(dict(self.values,command=['app','new-server']))
        code, repeated = self.app.action(self.session,body)
        self.assertEqual(code,200,repeated); self.assertEqual(repeated['job_id'],first['job_id'])
        self.assertEqual(job_target.load(self.db,first['job_id']),original)
        self.assertEqual(self.app.action(self.session,self.request())[0],409)

    def test_source_only_signed_webhook_validates_and_replays_old_profile(self):
        payload = dict(self.payload,ref='refs/heads/development',after='b'*40)
        code, result = self.deliver(payload)
        self.assertEqual(code,202,result); self.assertEqual(len(result['jobs']),1)
        job = self.db.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual(job['status'],'validation-queued'); self.assertEqual(job['mode'],'validate')
        original = job_target.load(self.db,job['id'])
        self.save_profile(dict(self.values,target='release'))
        repeated = self.deliver(payload)[1]
        self.assertEqual(repeated['jobs'],result['jobs']); self.assertFalse(repeated['new'])
        self.assertEqual(job_target.load(self.db,job['id']),original)
        self.assertIsNone(registry.get_environment(self.db,'test','development')['recipe_revision'])

    def test_disabled_source_webhook_never_enters_worker_queue(self):
        env = registry.get_environment(self.db,'test','development')
        registry.update_environment(self.db,'test','development',env['revision'],{'automatic':False})
        code, result = self.deliver(dict(self.payload,ref='refs/heads/development',after='b'*40))
        self.assertEqual(code,202,result)
        row = self.db.execute('SELECT status,mode FROM jobs').fetchone()
        self.assertEqual(tuple(row),('auto-deploy-disabled','validate'))

    def test_same_branch_profiles_have_independent_targets(self):
        self.save_profile(environment='production')
        for environment in ('development','production'):
            env = registry.get_environment(self.db,'test',environment)
            registry.update_environment(self.db,'test',environment,env['revision'],{'branch':'main'})
        code,result = self.deliver()
        self.assertEqual(code,202,result); self.assertEqual(len(result['jobs']),2)
        self.assertEqual({job_target.load(self.db,item)['environment'] for item in result['jobs']},{'development','production'})
        self.assertEqual({row[0] for row in self.db.execute('SELECT status FROM jobs')},{'validation-queued'})

    def bind_runtime(self):
        import hashlib
        import time
        runtime = {'compose_project':'test','compose_files':['/managed/test.yml'],'services':['web'],
                   'images':[{'name':'app'}],'tests':[{'command':['true']}],
                   'http_checks':[{'url':'http://example.test','statuses':[200]}]}
        document = registry.canonical(runtime)
        row = self.db.execute('INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,?)',
                             ('test',hashlib.sha256(document.encode()).hexdigest(),document,time.time()))
        self.db.execute("UPDATE registry_environments SET recipe_revision=?,state='running',revision=revision+1 WHERE project='test' AND name='development'",(row.lastrowid,))
        snapshot(self.db,'containers',[{'ID':'c'*64,'Names':'test-web','State':'running','ComposeProject':'test',
                 'ComposeService':'web','ComposeConfigFiles':'/managed/test.yml'}])
        snapshot(self.db,'worker',{'state':'running'})
        return row.lastrowid

    def test_source_validation_preserves_runtime_and_webhooks_keep_runtime_delivery(self):
        runtime_revision = self.bind_runtime()
        before = registry.get_environment(self.db,'test','development')
        code,result = self.app.action(self.session,self.request())
        self.assertEqual(code,202,result)
        self.assertEqual(registry.get_environment(self.db,'test','development'),before)
        code,result = self.deliver(dict(self.payload,ref='refs/heads/development',after='b'*40))
        self.assertEqual(code,202,result)
        target = job_target.load(self.db,result['jobs'][0])
        self.assertEqual(target['mode'],'deploy'); self.assertEqual(target['recipe_revision'],runtime_revision)

    def test_webhook_replay_preserves_source_target_after_runtime_becomes_bound(self):
        payload = dict(self.payload,ref='refs/heads/development',after='b'*40)
        first = self.deliver(payload)[1]
        original = job_target.load(self.db,first['jobs'][0])
        self.bind_runtime()
        repeated = self.deliver(payload)[1]
        self.assertEqual(repeated['jobs'],first['jobs'])
        self.assertFalse(repeated['new'])
        self.assertEqual(job_target.load(self.db,first['jobs'][0]),original)

    def test_managed_runtime_without_build_instructions_keeps_source_webhook_route(self):
        import hashlib
        import time
        runtime = {'managed_environment':{'project':'test','environment':'development'},
                   'compose_project':'test','compose_files':['/managed/test/development/compose.json'],
                   'services':['development-web'],'owned_services':['development-web']}
        document = registry.canonical(runtime)
        cursor = self.db.execute('INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,?)',
            ('test',hashlib.sha256(document.encode()).hexdigest(),document,time.time()))
        self.db.execute("UPDATE registry_environments SET recipe_revision=?,state='stopped',revision=revision+1 WHERE project='test' AND name='development'",(cursor.lastrowid,))
        before = registry.get_environment(self.db,'test','development')
        code, result = self.deliver(dict(self.payload,ref='refs/heads/development',after='b'*40))
        self.assertEqual(code,202,result)
        self.assertEqual(len(result['jobs']),1,result)
        target = job_target.load(self.db,result['jobs'][0])
        self.assertEqual(target['mode'],'validate')
        self.assertEqual(target['recipe']['schema_kind'],'artifact-build')
        self.assertEqual(registry.get_environment(self.db,'test','development'),before)
