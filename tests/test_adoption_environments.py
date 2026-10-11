import copy
import json
import sqlite3
import tempfile
from pathlib import Path
import unittest

import adoption
import adopted_workloads
import database_candidates
import registry


class AdoptionEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'state'
        self.db=sqlite3.connect(self.path,isolation_level=None);self.db.row_factory=sqlite3.Row;self.addCleanup(lambda:self.db.close())
        self.recipe={'compose_project':'app','compose_files':['/managed/databases.yml'],'services':['development-db','production-db']}
        registry.import_legacy(self.db,{'app':{'repository':'TPCT/app','repository_id':12,'branch':'main','container_prefixes':['app-'],'recipe':self.recipe}},[])
        self.rows=[]
        for env,c in [('development','a'),('production','b')]:
            self.rows.append({'name':'app-'+env+'-db-1','id':c*64,'image_id':'sha256:'+c*64,'compose_project':'app','compose_files':['/managed/databases.yml'],'compose_service':env+'-db','running':True,'database_endpoint':{},'direct_database':{'engine':'pgsql','database':env,'port':5432},'database_data_directory':'/data','database_port':5432,'mounts':[{'type':'volume','source':'/vol/'+env,'destination':'/data'}],'networks':{'app_default':{'id':'c'*64,'aliases':[env+'-db'],'ip':'172.20.0.2'}}})
        self.inventory={'observed':1000,'containers':self.rows}

    def accept(self,env,**options):
        preview=adoption.preview(self.db,'app',env,self.inventory,now=1001,**options)
        adoption.adopt(self.db,'app',env,preview['fingerprint'],self.inventory,'owner',now=1002,**options)
        return preview

    def test_independent_service_selections_persist_across_reconnect_and_replay(self):
        dev=self.accept('development',services=['development-db'])
        dev_before=adopted_workloads.get(self.db,'app','development')
        prod=self.accept('production',services=['production-db'])
        self.assertEqual(dev_before['recipe_revision'],adopted_workloads.get(self.db,'app','development')['recipe_revision'])
        self.assertEqual(registry.get_project(self.db,'app')['legacy_binding'],'development')
        self.db.close();self.db=sqlite3.connect(self.path,isolation_level=None);self.db.row_factory=sqlite3.Row
        for env,proof in [('development',dev),('production',prod)]:
            owner=adopted_workloads.get(self.db,'app',env)
            self.assertEqual(owner['recipe']['services'],[env+'-db'])
            self.assertTrue(adopted_workloads.database_only(self.db,'app',env))
            adoption.adopt(self.db,'app',env,proof['fingerprint'],self.inventory,'owner',now=9999,services=[env+'-db'])
            self.assertEqual(adopted_workloads._evidence(self.db,'app',env),proof)
        self.assertEqual(self.db.execute('SELECT count(*) FROM workload_adoptions').fetchone()[0],2)
        with self.assertRaises(ValueError):
            adoption.adopt(self.db,'app','development',dev['fingerprint'],self.inventory,'owner',services=['production-db'])

    def test_independent_registered_compose_groups(self):
        dev=self.accept('development',services=['development-db'])
        recipe={'compose_project':'app-production','compose_files':['/managed/production.yml'],'services':['production-db']}
        result=self.db.execute('INSERT INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,0)',('app',adoption._digest(recipe),registry.canonical(recipe)))
        revision=result.lastrowid
        self.rows[1].update(compose_project='app-production',compose_files=['/managed/production.yml'])
        prod=self.accept('production',recipe_revision=revision)
        self.assertEqual(prod['selected_recipe_revision'],revision)
        self.assertEqual(adopted_workloads.get(self.db,'app','production')['recipe']['compose_project'],'app-production')
        self.assertEqual(adopted_workloads._evidence(self.db,'app','development'),dev)

    def test_same_container_and_overlapping_storage_rejected_even_when_peer_missing_from_live_selection(self):
        self.accept('development',services=['development-db'])
        for field,value in [('id',self.rows[0]['id']),('mounts',copy.deepcopy(self.rows[0]['mounts']))]:
            original=copy.deepcopy(self.rows[1]);self.rows[1][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):
                adoption.preview(self.db,'app','production',{'observed':1000,'containers':[self.rows[1]]},now=1001,services=['production-db'])
            self.rows[1]=original
        self.assertEqual(self.db.execute('SELECT count(*) FROM workload_adoptions').fetchone()[0],1)

    def test_scoped_candidate_resolution_uses_correct_environment_evidence(self):
        for env in registry.ENVIRONMENTS:self.accept(env,services=[env+'-db'])
        containers=[];volumes={}
        for row in self.rows:
            env=row['direct_database']['database'];name=env+'-data'
            containers.append({'Id':row['id'],'Image':row['image_id'],'Config':{'Labels':{'com.docker.compose.project':'app','com.docker.compose.service':env+'-db','com.docker.compose.project.config_files':'/managed/databases.yml'}},'State':{'Running':True,'StartedAt':'2026-10-11T00:00:00Z'},'HostConfig':{'NetworkMode':'app_default'},'Mounts':[{'Type':'volume','Name':name,'Source':'/vol/'+env,'Destination':'/data','RW':True}],'NetworkSettings':{'Networks':{'app_default':{'NetworkID':'c'*64,'EndpointID':row['id'],'Aliases':[env+'-db']}}}})
            volumes[name]={'Name':name,'Driver':'local','Mountpoint':'/vol/'+env,'CreatedAt':'2026-10-11'}
        inventory={'observed':1000,'containers':containers,'volumes':volumes}
        for env in registry.ENVIRONMENTS:
            result=database_candidates.resolve(self.db,'app',env,env+'-db',inventory=lambda:inventory,now=1001)
            self.assertEqual(result['database'],env)
        with self.assertRaises(ValueError):database_candidates.resolve(self.db,'app','development','production-db',inventory=lambda:inventory,now=1001)

    def test_legacy_schema_migration_preserves_every_byte_and_is_atomic(self):
        self.db.execute('CREATE TABLE workload_adoptions(project TEXT PRIMARY KEY,environment TEXT NOT NULL,fingerprint TEXT NOT NULL,evidence TEXT NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL)')
        original=('app','development','a'*64,'{ "kept" : true }','owner',1.5)
        self.db.execute('INSERT INTO workload_adoptions VALUES(?,?,?,?,?,?)',original)
        with self.assertRaises(RuntimeError),registry.transaction(self.db):
            adoption.initialize(self.db)
            raise RuntimeError('rollback outer transaction')
        self.assertEqual([r['name'] for r in self.db.execute('PRAGMA table_info(workload_adoptions)') if r['pk']],['project'])
        adoption.initialize(self.db);adoption.initialize(self.db)
        self.assertEqual(tuple(self.db.execute('SELECT * FROM workload_adoptions').fetchone()),original)
        self.assertEqual([r['name'] for r in self.db.execute('PRAGMA table_info(workload_adoptions)') if r['pk']],['project','environment'])

    def test_invalid_service_selection_and_foreign_recipe_fail_without_adoption(self):
        for selected in ([],['unknown'],['development-db','development-db'],'development-db'):
            with self.subTest(selected=selected),self.assertRaises(ValueError):adoption.preview(self.db,'app','development',self.inventory,now=1001,services=selected)
        with self.assertRaises(ValueError):adoption.preview(self.db,'app','development',self.inventory,now=1001,recipe_revision=9999)

if __name__=='__main__':unittest.main()
