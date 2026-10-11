"""Shared ownership resolution; no credential or engine policy substitution."""
import copy
import unittest
import database_target as target
import test_database_inspection as inspection_tests


class DatabaseTargetTests(unittest.TestCase):
    def setUp(self):
        self.fixture=inspection_tests.InspectionTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_resolution_matches_inspection_except_private_verifier_identity(self):
        f=self.fixture
        resolved=target.resolve_ownership(f.binding,resolve_candidate=f.resolve)
        inspected=f.service._candidate(f.binding)
        self.assertEqual({k:v for k,v in inspected.items() if k not in ('fingerprint','verifier')},
                         {k:v for k,v in resolved.items() if k!='fingerprint'})
        self.assertNotIn('verifier',resolved)
        self.assertNotEqual(resolved['fingerprint'],inspected['fingerprint'])
        target.check_registered(f.db,resolved)
        self.assertEqual(f.helper.calls,0)

    def test_changed_endpoint_or_ownership_rejected_and_fresh_storage_changes_digest(self):
        f=self.fixture
        before=target.resolve_ownership(f.binding,resolve_candidate=f.resolve)
        for key,value in [('database','other'),('port',9999),('recipe_revision',-1),('engine','mysql')]:
            changed=dict(f.candidate,**{key:value})
            with self.subTest(key=key),self.assertRaises(target.TargetError):
                target.resolve_ownership(f.binding,resolve_candidate=lambda *a,**kw:changed)
        f.candidate['container_id']='b'*64
        after=target.resolve_ownership(f.binding,resolve_candidate=f.resolve)
        self.assertNotEqual(before['fingerprint'],after['fingerprint'])
        f.db.execute("UPDATE registry_environments SET revision=revision+1 WHERE project='app' AND name='development'")
        with self.assertRaises(target.TargetError):target.check_registered(f.db,after)

    def test_mysql_family_normalization_does_not_mutate_resolver_snapshot(self):
        f=self.fixture;row=copy.deepcopy(f.binding)
        row['definition']['engine']='mariadb';snapshot=dict(f.candidate,engine='mysql')
        resolved=target.resolve_ownership(row,resolve_candidate=lambda *a,**kw:snapshot)
        self.assertEqual(resolved['engine'],'mariadb');self.assertEqual(snapshot['engine'],'mysql')
