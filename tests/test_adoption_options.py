import json,unittest
import adoption,registry
import test_adoption_environments as fixtures

class OptionsTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.AdoptionEnvironmentTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.db=self.f.db
 def test_safe_catalog_excludes_paths_and_other_projects(self):
  registry.import_legacy(self.db,{'foreign':{'repository':'TPCT/foreign','repository_id':13,'branch':'main','container_prefixes':['foreign-'],'recipe':{'compose_project':'secret-group','compose_files':['/private/secret.yml'],'services':['secret-service']}}},[])
  result=adoption.options(self.db,'app','production')
  self.assertEqual(result['project'],'app');self.assertEqual(result['environment'],'production')
  for item in [result['default'],*result['recipes']]:
   self.assertEqual(set(item),{'recipe_revision','digest','compose_project','services'})
   self.assertEqual(item['services'],['development-db','production-db'])
  self.assertNotIn('/managed',json.dumps(result));self.assertNotIn('secret',json.dumps(result))
 def test_corrupt_catalog_fails_closed(self):
  self.db.execute("INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,0)",('app','f'*64,registry.canonical(self.f.recipe)))
  with self.assertRaises(ValueError):adoption.options(self.db,'app','development')
 def test_second_environment_catalog_available_after_first_adoption(self):
  self.f.accept('development',services=['development-db'])
  result=adoption.options(self.db,'app','production')
  self.assertIn('production-db',result['default']['services'])
  self.assertEqual(registry.get_project(self.db,'app')['legacy_binding'],'development')
