import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import build_profiles as profiles
import registry
from store import connect


class BuildProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)/'db'; self.db = connect(self.path)
        self.runtime = {'compose_project':'app', 'compose_files':['/existing/app.yml'], 'services':['web']}
        registry.import_legacy(self.db, {'app':{'repository':'TPCT/app','repository_id':12,'branch':'main','recipe':self.runtime}}, [])
        self.values = {'dockerfile':'Dockerfile','context':'.','target':None,'test_command':['app','test'],
                       'command':['app','serve'],'healthcheck':['app','health'],'user':'65534:65534'}

    def tearDown(self): self.db.close(); self.tmp.cleanup()

    def save(self, values=None, environment='development'):
        before = profiles.get(self.db,'app',environment)
        return profiles.save(self.db,'app',environment,before['project_revision'],before['environment_revision'],before['profile_revision'],values or self.values)

    def test_missing_save_reopen_and_independent_environment(self):
        empty = profiles.get(self.db,'app','development')
        self.assertIsNone(empty['values']); self.assertEqual(empty['profile_revision'],0)
        saved = self.save()
        self.assertEqual(saved['values'],self.values); self.assertEqual(saved['profile_revision'],1)
        self.assertEqual(saved['capabilities'],{'validate':True,'deploy':False})
        source = profiles.get_private(self.db,'app','development')
        self.assertEqual(source['recipe']['images'][0]['name'],'app-development')
        self.assertNotIn('compose_files',source['recipe'])
        self.db.close(); self.db = connect(self.path)
        self.assertEqual(profiles.get(self.db,'app','development'),saved)
        self.assertIsNone(profiles.get(self.db,'app','production')['values'])
        self.save(dict(self.values,context='backend',dockerfile='backend/Dockerfile'), 'production')
        self.assertEqual(profiles.get_private(self.db,'app','development'),source)
        with self.assertRaises(Exception): self.db.execute('UPDATE registry_recipes SET document=? WHERE id=?', ('{}',source['source_revision']))

    def test_adopted_runtime_preferences_and_binding_are_preserved(self):
        registry.update_project(self.db,'app',1,{'legacy_binding':'production'})
        before = registry.get_environment(self.db,'app','production')
        self.save(environment='production')
        after = registry.get_environment(self.db,'app','production')
        for key in ('recipe_revision','state','branch','automatic','wake_on_push','provision_on_push'):
            self.assertEqual(before[key],after[key])
        self.assertEqual(json.loads(self.db.execute('SELECT document FROM registry_recipes WHERE id=?',(after['recipe_revision'],)).fetchone()[0]),self.runtime)

    def test_strict_input_and_private_recipe_validation(self):
        for change in ({'dockerfile':'/host/file'}, {'dockerfile':'../Dockerfile'}, {'dockerfile':'a/../Dockerfile'},
                       {'context':'https://example.org'}, {'context':'.git'}, {'dockerfile':'a\\b'},
                       {'target':{}}, {'test_command':[]}, {'command':['bad\narg']}, {'healthcheck':'curl /'},
                       {'user':'0:0'}, {'environment':{'PASSWORD':'secret'}}, {'context':True}):
            with self.subTest(change=change), self.assertRaises(ValueError): self.save(dict(self.values,**change))
        self.assertEqual(profiles.get(self.db,'app','development')['profile_revision'],0)
        self.save(); source = profiles.get_private(self.db,'app','development')['recipe']
        for change in ({'env_file':'/secrets'}, {'compose_files':['/host']}, {'schema_version':True}, {'images':[None]}):
            with self.subTest(change=change), self.assertRaises(ValueError): profiles.validate_recipe(dict(source,**change))
        malformed = copy.deepcopy(source); malformed['images'][0]['name']='foreign'
        with self.assertRaises(ValueError): profiles.validate_recipe(malformed)

    def test_revision_race_one_winner_no_lost_update(self):
        before = profiles.get(self.db,'app','development')
        def write(target):
            db = connect(self.path)
            try:
                try: return profiles.save(db,'app','development',before['project_revision'],before['environment_revision'],0,dict(self.values,target=target))
                except ValueError: return None
            finally: db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write,['first','second']))
        self.assertEqual(sum(item is not None for item in results),1)
        self.assertEqual(profiles.get(self.db,'app','development')['profile_revision'],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM environment_build_profiles').fetchone()[0],1)

    def test_authenticated_adapter_returns_same_contract_and_audits_session_actor(self):
        import server
        credentials = Path(self.tmp.name)/'credentials'; credentials.write_text('{}')
        config = Path(self.tmp.name)/'missing.json'
        app = server.Application(config,self.path,credentials)
        status, response = app.action({'username':'owner'}, {'action':'build-profile-get','project':'app','environment':'development'})
        self.assertEqual(status,200)
        before = response['profile']
        body = {'action':'build-profile-save','project':'app','environment':'development','values':self.values,
                'project_revision':before['project_revision'],'environment_revision':before['environment_revision'],'profile_revision':0,'actor':'forged'}
        status, response = app.action({'username':'owner'},body)
        self.assertEqual(status,200); self.assertEqual(response['profile']['profile_revision'],1)
        self.assertEqual(app.action({'username':'owner'},body)[0],409)
        row = self.db.execute("SELECT actor FROM audit WHERE action='build-profile-save'").fetchone()
        self.assertEqual(row[0],'owner')

    def test_http_endpoint_uses_v2_and_preserves_v1_recipe(self):
        old=self.save();before=profiles.get_private(self.db,'app','development')
        old_document=self.db.execute('SELECT document FROM registry_recipes WHERE id=?',(before['source_revision'],)).fetchone()[0]
        endpoint={'port':8080,'health_path':'/health/ready'}
        saved=self.save(dict(self.values,http_endpoint=endpoint))
        recipe=profiles.get_private(self.db,'app','development')['recipe']
        self.assertEqual(recipe['schema_version'],2)
        self.assertEqual(recipe['service_profiles']['application-v1']['revision'],2)
        self.assertEqual(saved['values']['http_endpoint'],endpoint)
        self.assertEqual(profiles.validate_recipe(recipe),recipe)
        self.assertEqual(self.db.execute('SELECT document FROM registry_recipes WHERE id=?',(before['source_revision'],)).fetchone()[0],old_document)
        self.assertNotIn('http_endpoint',before['recipe']['service_profiles']['application-v1'])
        self.assertEqual(profiles.validate_values(dict(self.values,http_endpoint=None)),self.values)

    def test_http_endpoint_closed_schema_and_normalized_path(self):
        bad=[None,True,0,65536,'8080',1.5]
        for port in bad:
            with self.subTest(port=port),self.assertRaises(ValueError):
                profiles.validate_values(dict(self.values,http_endpoint={'port':port,'health_path':'/'}))
        for path in ('http://localhost','//host','/a//b','/a/../b','/a/./b','/a/','/x?password=secret','/x#fragment','/%2e%2e','/a\\b','/a\nb','/user:pass@host','/has space'):
            with self.subTest(path=path),self.assertRaises(ValueError):
                profiles.validate_values(dict(self.values,http_endpoint={'port':80,'health_path':path}))
        for endpoint in ({'port':80},{'port':80,'health_path':'/','url':'http://host'},[]):
            with self.assertRaises(ValueError):profiles.validate_values(dict(self.values,http_endpoint=endpoint))
        self.save();recipe=profiles.get_private(self.db,'app','development')['recipe']
        recipe['service_profiles']['application-v1']['http_endpoint']={'port':80,'health_path':'/'}
        with self.assertRaises(ValueError):profiles.validate_recipe(recipe)
