import json
import unittest
import adopted_workloads as retained
import database_candidates
import database_target
import ownership
import registry
import test_database_candidates as fixtures

class AdoptedWorkloadTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.CandidateTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.db=self.f.db
        env=registry.get_environment(self.db,'example','development');self.recipe_id=env['recipe_revision']
        row=self.db.execute('SELECT document,digest FROM registry_recipes WHERE id=?',(self.recipe_id,)).fetchone()
        self.recipe=json.loads(row['document']);e=json.loads(self.db.execute('SELECT evidence FROM workload_adoptions').fetchone()[0])
        e.pop('fingerprint');e['recipe_digest']=row['digest'];e['fingerprint']=database_candidates.digest(e)
        self.db.execute('UPDATE workload_adoptions SET evidence=?,fingerprint=?',(json.dumps(e),e['fingerprint']))
        self.recipe['owned_services']=['db'];document=registry.canonical(self.recipe)
        self.db.execute('INSERT INTO registry_recipes(project,document,digest,created) VALUES(?,?,?,?)',('example',document,database_candidates.digest(self.recipe),0))
        self.recipe_id=self.db.execute('SELECT id FROM registry_recipes WHERE digest=?',(database_candidates.digest(self.recipe),)).fetchone()[0]
        self.db.execute("UPDATE registry_environments SET recipe_revision=? WHERE project='example' AND name='development'",(self.recipe_id,))
    def test_retain_database_owner_across_managed_recipe_change(self):
        with registry.transaction(self.db):retained.retain(self.db,'example','development')
        self.assertTrue(retained.database_only(self.db,'example','development'))
        self.f.fixture.bind('development',service='managed-web')
        owner=ownership.resolve(self.db,'example','example','db',[self.f.fixture.files['development']])
        self.assertEqual(owner['recipe_revision'],self.recipe_id)
        candidate=self.f.resolve();self.assertEqual(candidate['recipe_revision'],self.recipe_id)
        database_target.check_registered(self.db,candidate)
        with self.assertRaises(Exception):self.db.execute("UPDATE adopted_workload_owners SET recipe_revision=999")
    def test_same_current_and_retained_owner_deduplicates(self):
        with registry.transaction(self.db):retained.retain(self.db,'example','development')
        self.assertEqual(self.f.resolve()['recipe_revision'],self.recipe_id)
    def test_unrelated_recipe_cannot_be_retained(self):
        self.f.fixture.bind('development',service='other')
        with registry.transaction(self.db),self.assertRaises(ValueError):retained.retain(self.db,'example','development')
    def test_association_detects_adoption_tampering(self):
        with registry.transaction(self.db):retained.retain(self.db,'example','development')
        self.db.execute("UPDATE workload_adoptions SET fingerprint=?",('0'*64,))
        with self.assertRaises(ValueError):retained.get(self.db,'example','development')
    def test_rebound_nonmanaged_environment_rejected_before_provision(self):
        import managed_services
        with registry.transaction(self.db):retained.retain(self.db,'example','development')
        self.f.fixture.bind('development',service='unrelated-app')
        with self.assertRaises(ValueError):managed_services.ensure_environment_eligible(self.db,'example','development')

    def test_foreign_environment_conflict_rejected(self):
        with registry.transaction(self.db):retained.retain(self.db,'example','development')
        self.f.fixture.bind('production',service='db',files=self.f.fixture.files['development'])
        with self.assertRaises(ValueError):ownership.resolve(self.db,'example','example','db',[self.f.fixture.files['development']])
