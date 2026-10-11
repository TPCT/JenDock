import tempfile
import unittest
from pathlib import Path
import migration_jobs as jobs
import test_migration_bindings as fixtures

class MigrationJobsTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.MigrationBindingsTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db;self.f.save()
    def test_immutable_capture_source_and_uncertain_state(self):
        target=jobs.capture(self.db,12,'app','development','sha256:'+'a'*64)
        self.assertEqual(target,jobs.capture(self.db,12,'app','development','sha256:'+'a'*64))
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'alembic/versions';p.mkdir(parents=True);(p/'one.py').write_text('pass\n')
            bundle=jobs.collect_sources(root,target['binding']['definition'])
        jobs.save_sources(self.db,12,bundle)
        jobs.save_sources(self.db,12,bundle)
        with self.assertRaises(ValueError):jobs.save_sources(self.db,12,{'alembic/versions/one.py':'different'})
        jobs.record_plan(self.db,12,{'plan_digest':'b'*64,'expected':[]})
        jobs.begin_apply(self.db,12)
        with self.assertRaises(ValueError):jobs.begin_apply(self.db,12)
        self.assertEqual(jobs.load(self.db,12)['state'],'applying')
        jobs.mark_uncertain(self.db,12)
        self.assertEqual(jobs.load(self.db,12)['state'],'recovery-required')
        with self.assertRaises(ValueError):jobs.record_plan(self.db,12,{'plan_digest':'c'*64})
    def test_existing_empty_directory_is_valid_source_bundle(self):
        target=jobs.capture(self.db,15,'app','development','sha256:'+'a'*64)
        with tempfile.TemporaryDirectory() as root:
            (Path(root)/'alembic/versions').mkdir(parents=True)
            bundle=jobs.collect_sources(root,target['binding']['definition'])
        self.assertEqual(bundle,{})
        jobs.save_sources(self.db,15,bundle)
        self.assertEqual(jobs.load(self.db,15)['sources'],{})

    def test_sources_reject_symlinks_and_unselected_files(self):
        definition=self.f.values
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'alembic/versions';p.mkdir(parents=True);(Path(root)/'other.py').write_text('secret')
            (p/'bad.py').symlink_to(Path(root)/'other.py')
            with self.assertRaises(ValueError):jobs.collect_sources(root,definition)
    def test_capture_no_binding_is_explicit_and_immutable(self):
        self.assertIsNone(jobs.capture(self.db,9,'app','production','sha256:'+'a'*64)['binding'])
        with self.assertRaises(ValueError):jobs.capture(self.db,9,'app','development','sha256:'+'a'*64)
    def test_enqueue_transaction_rollback_removes_capture(self):
        self.db.execute('BEGIN IMMEDIATE')
        jobs.capture(self.db,44,'app','development','sha256:'+'a'*64)
        self.db.rollback()
        with self.assertRaises(ValueError):jobs.load(self.db,44)

    def test_revocation_before_apply_keeps_prepared(self):
        import migration_credentials
        jobs.capture(self.db,2,'app','development','sha256:'+'a'*64)
        jobs.save_sources(self.db,2,{'alembic/versions/a.py':'pass'})
        jobs.record_plan(self.db,2,{'plan_digest':'b'*64})
        migration_credentials.revoke_credential(self.db,self.f.principal,'app','development',self.f.secret['id'],expected_revision=1)
        with self.assertRaises(ValueError):jobs.begin_apply(self.db,2)
        self.assertEqual(jobs.load(self.db,2)['state'],'planned')

class ManagedEnqueueCaptureTests(unittest.TestCase):
    def test_capture_is_atomic_and_validation_build_does_not_require_helper(self):
        import dataclasses
        import os
        from unittest.mock import patch
        import test_managed_source_delivery as fixtures
        import migration_credentials
        import migration_bindings
        from cryptography.fernet import Fernet
        f=fixtures.ManagedSourceDeliveryTests();f.setUp();self.addCleanup(f.tearDown)
        principal=dataclasses.replace(f.fixture.principal,scopes=frozenset({'databases:manage'}))
        key=Path(f.fixture.tmp.name)/'migration-key';key.write_bytes(Fernet.generate_key());key.chmod(0o600)
        secret=migration_credentials.save_credential(f.db,principal,'app','development',{'username':'migrator','password':'secret'},expected_revision=0,key_path=key)
        migration_bindings.save_binding(f.db,principal,'app','development',dict(engine='postgresql',adapter_version=1,ownership={'kind':'adopted-compose','reference':'app','revision':1},database_name='app',database_port=5432,secret_id=secret['id'],secret_revision=1,framework='alembic',grammar=None,migration_paths=['alembic/versions'],ledger_schema='public'),expected_revision=0)
        before=f.db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(ValueError):f.enqueue_source()
            self.assertEqual(f.db.execute('SELECT count(*) FROM jobs').fetchone()[0],before)
            validate=f.enqueue_source(mode='validate',sha='e'*40)
            self.assertIsNone(jobs.optional(f.db,validate))
        with patch.dict(os.environ,{'MIGRATION_HELPER_IMAGE':'sha256:'+'a'*64}):
            job=f.enqueue_source()
            captured=jobs.load(f.db,job)['target']
            self.assertEqual(captured['binding']['definition']['secret_id'],secret['id'])
            self.assertEqual(captured['helper_image'],'sha256:'+'a'*64)
