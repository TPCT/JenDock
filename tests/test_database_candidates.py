import copy
import json
import time
import unittest
import adoption
import database_candidates as candidates
import test_ownership as fixtures


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.RegisteredOwnershipTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db=self.fixture.db
        self.fixture.bind('development',service='db')
        adoption.initialize(self.db)
        self.row={'Id':'a'*64,'Image':'sha256:'+'b'*64,'Config':{'Env':['PASSWORD=private'], 'Labels':{
            'com.docker.compose.project':'example','com.docker.compose.service':'db',
            'com.docker.compose.project.config_files':self.fixture.files['development']}},
            'State':{'Running':True,'StartedAt':'2026-10-11T00:00:00Z'},'HostConfig':{'NetworkMode':'example_default'},
            'Mounts':[{'Type':'volume','Name':'data','Source':'/vol/data','Destination':'/data','RW':True}],
            'NetworkSettings':{'Networks':{'default':{'NetworkID':'c'*64,'EndpointID':'d'*64,'IPAddress':'172.20.0.2','Aliases':['db']}}}}
        self.inventory={'observed':time.time(),'containers':[self.row], 'volumes':{'data':{'Name':'data','Driver':'local','Mountpoint':'/vol/data','CreatedAt':'2026-10-11'}}}
        evidence={'project':'example','environment':'development','containers':[{'id':self.row['Id'],'image_id':self.row['Image'],'service':'db'}],
                  'databases':[{'container_id':self.row['Id'],'engine':'pgsql','database':'app','port':5432,'client':'e'*64,'host':'db',
                    'storage':[{'type':'volume','source':'/vol/data','destination':'/data'}]}]}
        evidence['fingerprint']=candidates.digest(evidence)
        self.db.execute('INSERT INTO workload_adoptions VALUES(?,?,?,?,?,?)',('example','development',evidence['fingerprint'],json.dumps(evidence),'owner',0))
    def resolve(self,**kwargs):
        return candidates.resolve(self.db,'example',kwargs.pop('environment','development'),'db',inventory=lambda:copy.deepcopy(self.inventory),**kwargs)
    def test_actual_registry_private_identity_and_safe_public(self):
        value=self.resolve();self.assertEqual(value['container_id'],self.row['Id'])
        self.assertFalse(candidates.public(value)['verified'])
        self.assertNotIn('/vol/data',json.dumps(candidates.public(value)))
        self.assertNotIn('private',json.dumps(value))
        self.assertEqual(value['fingerprint'],self.resolve()['fingerprint'])
    def test_registered_engine_family_is_preserved_without_image_guessing(self):
        row=self.db.execute('SELECT evidence FROM workload_adoptions').fetchone()
        original=json.loads(row['evidence'])
        for engine in ('mysql','mariadb'):
            evidence=copy.deepcopy(original);evidence.pop('fingerprint')
            evidence['databases'][0].update(engine=engine,port=3306)
            evidence['fingerprint']=candidates.digest(evidence)
            self.db.execute('UPDATE workload_adoptions SET fingerprint=?,evidence=?',(evidence['fingerprint'],json.dumps(evidence)))
            actual=self.resolve()
            self.assertEqual(actual['engine'],engine);self.assertEqual(actual['port'],3306)
    def test_wrong_environment_and_managed_role(self):
        for kwargs in ({'environment':'production'},{'kind':'managed-service'}):
            with self.assertRaises(candidates.CandidateError):self.resolve(**kwargs)
    def test_replaced_stopped_ambiguous_and_shared(self):
        original=copy.deepcopy(self.inventory)
        for mutation,code in (
            (lambda: self.row.update(Id='f'*64),'identity_changed'),
            (lambda:self.row['State'].update(Running=False),'target_stopped'),
            (lambda:self.inventory['containers'].append(copy.deepcopy(self.row)),'candidate_ambiguous'),
            (lambda:self.inventory['containers'].append({'Id':'f'*64,'Mounts':copy.deepcopy(self.row['Mounts'])}),'shared_storage')):
            self.inventory=copy.deepcopy(original);self.row=self.inventory['containers'][0];mutation()
            with self.assertRaisesRegex(candidates.CandidateError,code):self.resolve()
    def test_adoption_integrity(self):
        self.db.execute("UPDATE workload_adoptions SET fingerprint=?",('0'*64,))
        with self.assertRaisesRegex(candidates.CandidateError,'adoption_integrity'):self.resolve()
    def test_recipe_integrity_and_no_prefix_fallback(self):
        for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='registry_recipes'").fetchall():
            self.db.execute('DROP TRIGGER "'+row[0]+'"')
        self.db.execute("UPDATE registry_recipes SET digest=?",('0'*64,))
        with self.assertRaises(candidates.CandidateError):self.resolve()
    def test_changed_namespace_volume_and_stale_observation(self):
        original=copy.deepcopy(self.inventory)
        for mutate in (lambda:self.inventory.update(observed=0),lambda:self.inventory['volumes'].clear(),
                       lambda:self.inventory['containers'][0]['HostConfig'].update(NetworkMode='host')):
            self.inventory=copy.deepcopy(original);mutate()
            with self.assertRaises(candidates.CandidateError):self.resolve()
    def test_unregistered_files_and_overlapping_storage_fail(self):
        self.row['Config']['Labels']['com.docker.compose.project.config_files']='/unregistered.yml'
        with self.assertRaises(candidates.CandidateError):self.resolve()
        self.row['Config']['Labels']['com.docker.compose.project.config_files']=self.fixture.files['development']
        self.row['Mounts'].append({'Type':'bind','Source':'/elsewhere','Destination':'/data/subdir','RW':True})
        with self.assertRaisesRegex(candidates.CandidateError,'overlapping_storage'):self.resolve()
    def test_unrelated_compose_db_service_does_not_conflict(self):
        other=copy.deepcopy(self.row);other['Id']='f'*64;other['Mounts']=[]
        other['Config']['Labels']['com.docker.compose.project']='another-project'
        self.inventory['containers'].append(other)
        self.assertEqual(self.resolve()['reference'],'db')
    def test_fingerprint_changes_on_fresh_runtime_and_project_revision(self):
        first=self.resolve()['fingerprint']
        self.row['State']['StartedAt']='2026-10-11T01:00:00Z'
        self.assertNotEqual(first,self.resolve()['fingerprint'])
        second=self.resolve()['fingerprint']
        self.db.execute("UPDATE registry_projects SET revision=revision+1 WHERE id='example'")
        self.assertNotEqual(second,self.resolve()['fingerprint'])
    def test_duplicate_mount_and_nonboolean_rw_rejected(self):
        self.row['Mounts'].append(copy.deepcopy(self.row['Mounts'][0]))
        with self.assertRaisesRegex(candidates.CandidateError,'invalid_storage'):self.resolve()
        self.row['Mounts'].pop();self.row['Mounts'][0]['RW']='true'
        with self.assertRaisesRegex(candidates.CandidateError,'invalid_storage'):self.resolve()
    def test_volume_options_are_digest_only_and_malformed_rows_sanitized(self):
        self.inventory['volumes']['data']['Options']={'password':'network-secret'}
        self.assertNotIn('network-secret',json.dumps(self.resolve()))
        self.inventory['containers']=[None]
        with self.assertRaisesRegex(candidates.CandidateError,'identity_unavailable'):self.resolve()
    def test_ambiguous_environment_claim(self):
        self.fixture.bind('production',service='db',files=[self.fixture.files['development']],owned_services=['db'])
        with self.assertRaises(candidates.CandidateError):self.resolve()

if __name__=='__main__':unittest.main()
