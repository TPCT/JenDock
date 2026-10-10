import copy
import tempfile
import unittest
from pathlib import Path
from store import connect
from resource_policy import read, save, allocation

class ResourcePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db';self.db=connect(self.path)
        self.doc=read(self.db,'app')['document']
        self.doc['budget']={'cpu':2,'memory_mib':1024}
        self.doc['environments']['production']={'budget':{'cpu':1,'memory_mib':512},'services':{'app-1':{'cpu':1,'memory_mib':512}}}
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def test_persistence_and_stale_revision(self):
        save(self.db,'app',0,self.doc);self.db.close();self.db=connect(self.path)
        self.assertEqual(allocation(self.db,'app','production','app-1',1),{'cpu':1,'memory_mib':512})
        with self.assertRaises(ValueError):save(self.db,'app',0,self.doc)
    def test_sibling_environment_budget_is_reserved(self):
        self.doc['environments']['development']['budget']={'cpu':1.5}
        with self.assertRaises(ValueError):save(self.db,'app',0,self.doc)
    def test_service_overallocation_rejected(self):
        self.doc['environments']['production']['services']['app-2']={'cpu':.1,'memory_mib':16}
        with self.assertRaises(ValueError):save(self.db,'app',0,self.doc)
    def test_invalid_and_nonfinite_limits_rejected(self):
        for value in (True,-1,float('nan'),float('inf'),'4'):
            doc=copy.deepcopy(self.doc);doc['budget']['cpu']=value
            with self.assertRaises(ValueError):save(self.db,'app',0,doc)
    def test_cross_environment_duplicate_rejected(self):
        self.doc['environments']['development']=copy.deepcopy(self.doc['environments']['production'])
        with self.assertRaises(ValueError):save(self.db,'app',0,self.doc)
    def test_missing_service_and_stale_apply_rejected(self):
        save(self.db,'app',0,self.doc)
        for env,name,revision in [('development','app-1',1),('production','app-1',0)]:
            with self.assertRaises(ValueError):allocation(self.db,'app',env,name,revision)

    def test_apply_requires_integer_revision_and_valid_target(self):
        save(self.db, 'app', 0, self.doc)
        for revision in (True, 1.0, '1', None, -1):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                allocation(self.db, 'app', 'production', 'app-1', revision)
        for environment, container in (([], 'app-1'), ({}, 'app-1'), ('staging', 'app-1'), ('production', []), ('production', None)):
            with self.subTest(environment=environment, container=container), self.assertRaises(ValueError):
                allocation(self.db, 'app', environment, container, 1)

    def test_extreme_integer_and_non_string_service_rejected_as_validation_errors(self):
        for field in ('cpu', 'memory_mib'):
            doc = copy.deepcopy(self.doc)
            doc['budget'][field] = 10 ** 1000
            with self.subTest(field=field), self.assertRaises(ValueError):
                save(self.db, 'app', 0, doc)
        doc = copy.deepcopy(self.doc)
        doc['environments']['production']['services'] = {1: {'cpu': 1, 'memory_mib': 512}}
        with self.assertRaises(ValueError):
            save(self.db, 'app', 0, doc)

    def test_disk_quotas_are_not_part_of_any_policy_level(self):
        for level in ('project', 'environment', 'service'):
            doc = copy.deepcopy(self.doc)
            target = doc['budget'] if level == 'project' else doc['environments']['production']['budget'] if level == 'environment' else doc['environments']['production']['services']['app-1']
            target['disk_gib'] = 10
            with self.subTest(level=level), self.assertRaises(ValueError):
                save(self.db, 'app', 0, doc)
        self.assertEqual(read(self.db, 'app')['revision'], 0)

    def test_successful_save_returns_its_own_committed_revision(self):
        # Another operator may save after our COMMIT, before our caller returns.
        db = self.db
        path = self.path
        next_document = copy.deepcopy(self.doc)
        next_document['budget']['cpu'] = 3
        class CommitRace:
            def execute(self, statement, parameters=()):
                cursor = db.execute(statement, parameters)
                if statement == 'COMMIT':
                    other = connect(path)
                    try:
                        save(other, 'app', 1, next_document)
                    finally:
                        other.close()
                return cursor
        result = save(CommitRace(), 'app', 0, self.doc)
        self.assertEqual(result['revision'], 1)
        self.assertEqual(result['document'], self.doc)
        self.assertEqual(read(self.db, 'app')['revision'], 2)
        self.assertEqual(read(self.db, 'app')['document'], next_document)

    def test_save_is_blocked_only_while_this_projects_resource_apply_is_running(self):
        self.db.execute("INSERT INTO control_runs(project,operation,status) VALUES('app','resource-apply','running')")
        with self.assertRaisesRegex(ValueError, 'application is running'):
            save(self.db, 'app', 0, self.doc)
        self.assertEqual(read(self.db, 'app')['revision'], 0)
        self.db.execute("UPDATE control_runs SET status='succeeded'")
        save(self.db, 'app', 0, self.doc)
        self.db.execute("INSERT INTO control_runs(project,operation,status) VALUES('other','resource-apply','running')")
        self.assertEqual(save(self.db, 'app', 1, self.doc)['revision'], 2)

    def test_two_concurrent_saves_cannot_overwrite_same_revision(self):
        import threading
        barrier = threading.Barrier(2)
        results = []
        def writer(cpu):
            db = connect(self.path)
            document = copy.deepcopy(self.doc)
            document['budget']['cpu'] = cpu
            try:
                barrier.wait(timeout=5)
                try:
                    result = save(db, 'app', 0, document)
                    results.append(('saved', result))
                except ValueError as error:
                    results.append(('rejected', str(error)))
            finally:
                db.close()
        threads = [threading.Thread(target=writer, args=(cpu,)) for cpu in (2, 3)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(result[0] for result in results), ['rejected', 'saved'])
        saved = next(value for status, value in results if status == 'saved')
        self.assertEqual(read(self.db, 'app'), saved)

    def test_each_resource_is_checked_independently_at_each_level(self):
        for field in ('cpu', 'memory_mib'):
            for level in ('service', 'environment'):
                doc = copy.deepcopy(self.doc)
                if level == 'service':
                    ceiling = doc['environments']['production']['budget'][field]
                    doc['environments']['production']['services']['app-1'][field] = ceiling + 1
                else:
                    doc['environments']['development']['budget'][field] = doc['budget'][field]
                with self.subTest(field=field, level=level), self.assertRaises(ValueError):
                    save(self.db, 'app', 0, doc)
        self.assertEqual(read(self.db, 'app')['revision'], 0)

    def test_single_character_container_and_detached_return_values(self):
        services = self.doc['environments']['production']['services']
        services['a'] = services.pop('app-1')
        saved = save(self.db, 'app', 0, self.doc)
        self.doc['budget']['cpu'] = 999
        saved['document']['budget']['cpu'] = 888
        limits = allocation(self.db, 'app', 'production', 'a', 1)
        limits['cpu'] = 777
        self.assertEqual(read(self.db, 'app')['document']['budget']['cpu'], 2)
        self.assertEqual(allocation(self.db, 'app', 'production', 'a', 1)['cpu'], 1)
