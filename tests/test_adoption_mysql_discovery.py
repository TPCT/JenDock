import copy
import json
import sqlite3
import unittest

import adoption
import registry


class MySQLDiscoveryTests(unittest.TestCase):
    def collect(self, engine='mysql', output='/var/lib/mysql/\t3306\tbusiness\n', *, database='business', running=True, digest=True):
        prefix='MARIADB' if engine=='mariadb' else 'MYSQL'
        self.raw={'Id':'a'*64,'Name':'/app-db-1','Image':'sha256:'+'b'*64,
            'Config':{'Image':'docker.io/library/'+engine+('@sha256:'+'c'*64 if digest else ':8.4'),
                'Labels':{'com.docker.compose.project':'app','com.docker.compose.service':'db','com.docker.compose.project.config_files':'/managed/compose.yml'},
                'Env':[prefix+'_ROOT_PASSWORD=private-secret']+([prefix+'_DATABASE='+database] if database is not None else [])},
            'State':{'Running':running},'NetworkSettings':{'Networks':{}},
            'Mounts':[{'Type':'volume','Source':'/docker/volumes/app-db','Destination':'/var/lib/mysql'}]}
        self.calls=[]
        def command(argv):
            self.calls.append(argv)
            if argv[1]=='ps':return 'a'*64
            if argv[1]=='inspect':return json.dumps([self.raw])
            if isinstance(output,Exception):raise output
            return output
        return adoption.collect('/usr/bin/docker',command)

    def test_named_clients_digest_images_and_authenticated_database(self):
        for engine in ('mysql','mariadb'):
            for digest in (True,False):
                with self.subTest(engine=engine,digest=digest):
                    result=self.collect(engine,digest=digest)
                    row=result['containers'][0]
                    self.assertEqual(row['direct_database'],{'engine':'mysql','database':'business','port':3306})
                    self.assertEqual(row['database_data_directory'],'/var/lib/mysql')
                    script=self.calls[-1][-1]
                    self.assertIn('exec '+engine+' -uroot',script)
                    self.assertIn('SELECT @@datadir, @@port, DATABASE()',script)
                    self.assertIn('"--database=$database"',script)
                    self.assertNotIn('private-secret',json.dumps(self.calls)+json.dumps(result))

    def test_environment_alone_or_failed_authentication_never_becomes_database_proof(self):
        for output in (RuntimeError('private-secret'), '/var/lib/mysql/\t3306\tother\n',
                       '/var/lib/mysql/\t3306\tNULL\n','/var/lib/mysql/\t99999\tbusiness\n'):
            with self.subTest(output=type(output).__name__):
                row=self.collect(output=output)['containers'][0]
                self.assertIsNone(row['direct_database'])
        self.assertIsNone(self.collect(database=None,output='/var/lib/mysql/\t3306\tNULL\n')['containers'][0]['direct_database'])
        self.assertIsNone(self.collect(running=False)['containers'][0]['direct_database'])
        self.assertFalse(any(c[1]=='exec' for c in self.calls))

    def test_untrusted_database_name_is_not_interpolated_into_shell_or_sql(self):
        database='odd;$(touch marker)\"name'
        result=self.collect(database=database,output='/var/lib/mysql/\t3306\t'+database+'\n')
        self.assertEqual(result['containers'][0]['direct_database']['database'],database)
        self.assertNotIn(database,json.dumps(self.calls))

    def test_actual_collect_result_can_preview_database_only_workload_with_storage(self):
        for engine in ('mysql','mariadb'):
            db=sqlite3.connect(':memory:',isolation_level=None);db.row_factory=sqlite3.Row
            self.addCleanup(db.close)
            registry.import_legacy(db,{'app':{'repository':'TPCT/app','repository_id':12,'branch':'main','container_prefixes':['app-'],
                'recipe':{'compose_project':'app','compose_files':['/managed/compose.yml'],'services':['db']}}},[])
            inventory=self.collect(engine)
            result=adoption.preview(db,'app','development',inventory,now=inventory['observed'])
            self.assertEqual(len(result['databases']),1)
            self.assertEqual(result['databases'][0]['database'],'business')
            self.assertEqual(result['databases'][0]['engine'],'mysql')
            self.assertEqual(result['databases'][0]['container_id'],'a'*64)
            no_storage=copy.deepcopy(inventory);no_storage['containers'][0]['mounts']=[]
            with self.assertRaisesRegex(ValueError,'persistent storage'):
                adoption.preview(db,'app','development',no_storage,now=inventory['observed'])
            for name in ('x'*65,'bad\x7fname'):
                invalid=copy.deepcopy(inventory);invalid['containers'][0]['direct_database']['database']=name
                with self.assertRaisesRegex(ValueError,'engine identity'):
                    adoption.preview(db,'app','development',invalid,now=inventory['observed'])


if __name__=='__main__':unittest.main()
