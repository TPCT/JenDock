import copy
import json
import sqlite3
import unittest

import adoption
import registry


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        registry.import_legacy(self.db, {'app': {'repository': 'TPCT/app', 'repository_id': 12,
            'branch': 'main', 'automatic': True, 'container_prefixes': ['app-'],
            'recipe': {'services': ['web'], 'compose_project': 'shared', 'compose_files': ['/managed/compose.yml', '/managed/override.yml']}}}, [])
        self.rows = []
        for name, char in [('web', 'a'), ('db', 'b')]:
            self.rows.append({'name': 'app-'+name+'-1', 'id': char*64, 'image_id': 'sha256:'+char*64,
                'compose_project': 'shared', 'compose_files': ['/managed/compose.yml', '/managed/override.yml'], 'compose_service': name,
                'running': True, 'database_endpoint': {}, 'mounts': [],
                'networks': {'net': {'id': 'network-id', 'aliases': [name], 'ip': '10.0.0.'+str(len(self.rows)+1)}}})
        self.rows[0]['database_endpoint'] = {'DB_CONNECTION': 'pgsql', 'DB_HOST': 'db', 'DB_DATABASE': 'business'}
        self.rows[1]['mounts'] = [{'type': 'volume', 'source': '/docker/volumes/app-db', 'destination': '/var/lib/postgresql/data'}]
        self.rows[1].update(database_data_directory='/var/lib/postgresql/data', database_port=5432)
        self.inventory = {'observed': 1000, 'containers': self.rows}

    def tearDown(self):
        self.db.close()

    def preview(self):
        return adoption.preview(self.db, 'app', 'production', self.inventory, now=1001)

    def test_adoption_preserves_preferences_other_environment_and_deploy_services(self):
        registry.update_environment(self.db, 'app', 'production', 1, {'branch': 'production', 'automatic': False})
        before = registry.get_environment(self.db, 'app', 'development')
        review = self.preview()
        result = adoption.adopt(self.db, 'app', 'production', review['fingerprint'], self.inventory, 'admin', now=1002)
        self.assertEqual(result['legacy_binding'], 'production')
        self.assertEqual(result['environments']['development'], before)
        env = result['environments']['production']
        self.assertFalse(env['automatic'])
        self.assertEqual(env['branch'], 'production')
        recipe = json.loads(self.db.execute('SELECT document FROM registry_recipes WHERE id=?', (env['recipe_revision'],)).fetchone()[0])
        self.assertEqual(recipe['services'], ['web'])
        self.assertEqual(recipe['owned_services'], ['db', 'web'])
        with self.assertRaisesRegex(ValueError, 'already adopted'):
            self.preview()

    def test_changed_identity_or_revision_rejects_review_without_binding(self):
        review = self.preview()
        self.rows[0]['id'] = 'c'*64
        with self.assertRaisesRegex(ValueError, 'changed'):
            adoption.adopt(self.db, 'app', 'production', review['fingerprint'], self.inventory, 'admin', now=1002)
        self.assertIsNone(registry.get_project(self.db, 'app')['legacy_binding'])

    def test_wrong_owner_shared_storage_and_external_database_rejected(self):
        original = copy.deepcopy(self.rows)
        cases = [lambda: self.rows[0].update(compose_project='other'),
                 lambda: self.rows[0]['database_endpoint'].update(DB_HOST='external.example'),
                 lambda: self.rows.append(dict(copy.deepcopy(self.rows[1]), name='other-db-1', id='d'*64))]
        for change in cases:
            self.rows[:] = copy.deepcopy(original)
            change()
            with self.assertRaises(ValueError): self.preview()

    def test_stale_observation_and_missing_deployment_service_rejected(self):
        self.inventory['observed'] = 1
        with self.assertRaisesRegex(ValueError, 'Refresh'): self.preview()
        self.inventory['observed'] = 1000
        self.rows.pop(0)
        with self.assertRaisesRegex(ValueError, 'missing'): self.preview()

    def test_sqlite_requires_persistent_normalized_path(self):
        self.rows[0]['database_endpoint'] = {'DB_CONNECTION': 'sqlite', 'DB_DATABASE': '/app/storage/db.sqlite'}
        with self.assertRaisesRegex(ValueError, 'persistent'): self.preview()
        self.rows[0]['mounts'] = [{'type': 'volume', 'source': '/docker/volumes/sqlite', 'destination': '/app/storage'}]
        self.assertEqual(self.preview()['databases'][0]['engine'], 'sqlite')

    def test_database_port_other_clients_and_nondata_mount_rejected(self):
        original = copy.deepcopy(self.rows)
        self.rows[0]['database_endpoint']['DB_PORT'] = '6543'
        with self.assertRaises(ValueError): self.preview()
        self.rows[:] = copy.deepcopy(original)
        self.rows.append(dict(copy.deepcopy(self.rows[0]), name='unrelated-client', id='e'*64))
        with self.assertRaisesRegex(ValueError, 'Another workload'): self.preview()
        self.rows[:] = copy.deepcopy(original)
        self.rows[1]['mounts'][0]['destination'] = '/var/log'
        with self.assertRaisesRegex(ValueError, 'persistent storage'): self.preview()

    def test_same_review_retry_is_idempotent_and_actor_is_bound(self):
        review = self.preview()
        first = adoption.adopt(self.db, 'app', 'production', review['fingerprint'], self.inventory, 'admin', now=1001)
        second = adoption.adopt(self.db, 'app', 'production', review['fingerprint'], self.inventory, 'admin', now=1002)
        self.assertEqual(first, second)
        with self.assertRaises(ValueError):
            adoption.adopt(self.db, 'app', 'production', review['fingerprint'], self.inventory, 'other', now=1002)

    def test_collector_omits_secrets_and_queries_engine_directory(self):
        raw = {'Id': 'a'*64, 'Name': '/app-db-1', 'Image': 'sha256:'+'b'*64,
               'Config': {'Image': 'postgres:16', 'Labels': {}, 'Env': [
                   'DATABASE_URL=postgresql://user:secret-sentinel@db:6543/business',
                   'PRIVATE_KEY=secret-sentinel', 'POSTGRES_PASSWORD=secret-sentinel']},
               'State': {'Running': True}, 'NetworkSettings': {'Networks': {}}, 'Mounts': []}
        calls = []
        def command(args):
            calls.append(args)
            if args[1] == 'ps': return 'a'*64
            if args[1] == 'inspect': return json.dumps([raw])
            return '/var/lib/postgresql/data|6543\n'
        result = adoption.collect('docker', command)
        self.assertNotIn('secret-sentinel', json.dumps(result))
        self.assertNotIn('secret-sentinel', json.dumps(calls))
        self.assertEqual(result['containers'][0]['database_port'], 6543)
        self.assertEqual(result['containers'][0]['database_endpoint']['DATABASE_URL']['port'], 6543)

    def test_reversed_or_duplicate_configuration_rejects_adoption(self):
        for files in [['/managed/override.yml', '/managed/compose.yml'],
                      ['/managed/compose.yml', '/managed/override.yml', '/managed/compose.yml']]:
            self.rows[0]['compose_files'] = files
            with self.subTest(files=files), self.assertRaises(ValueError): self.preview()
            self.assertIsNone(registry.get_project(self.db, 'app')['legacy_binding'])
