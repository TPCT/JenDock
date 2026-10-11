"""Application attachment dispatch uses real scoped immutable domain records."""
import unittest
from unittest.mock import patch
import database_control
import test_application_database_bindings as fixtures


class ApplicationDatabaseControlTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.AttachmentTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        patcher=patch.object(database_control,'ConsoleAuthority')
        self.authority=patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.authority.principal=self.fixture.principal
        self.authority.recheck.return_value=self.fixture.principal

    def action(self,name,**values):
        return database_control.execute(self.fixture.db,dict(
            action='database-application-'+name,project='example',
            environment='development',authentication={},**values),key_path=self.fixture.key)

    def test_binding_receipts_current_state_and_revoke(self):
        payload=dict(values=self.fixture.values,expected_revision=0,request_id='api-binding-create')
        receipt=self.action('binding-save',**payload)
        self.assertTrue(receipt['state_requires_reload'])
        row=receipt['binding']
        self.assertEqual(row['application_status'],'not-observed')
        self.assertEqual(self.action('binding-save',**payload),receipt)
        self.assertEqual(self.action('binding-get',binding_id=row['id'])['binding'],row)
        page=self.action('bindings',limit=1)
        self.assertEqual(page['bindings'],[row]);self.assertEqual(page['next_cursor'],row['id'])
        self.assertEqual(self.action('bindings',cursor=row['id'])['bindings'],[])
        disabled=self.action('binding-disable',binding_id=row['id'],expected_revision=1)['binding']
        self.assertEqual(disabled['status'],'disabled')
        self.assertEqual(self.action('binding-save',**payload),receipt)
        self.assertEqual(self.action('binding-get',binding_id=row['id'])['binding']['status'],'disabled')
        self.assertGreater(self.authority.recheck.call_count,8)

    def test_closed_fields_missing_receipt_and_wrong_scope(self):
        for name,values in [('bindings',{'unknown':True}),('binding-save',{'values':self.fixture.values,'expected_revision':0})]:
            with self.assertRaises(ValueError):self.action(name,**values)
        with self.assertRaises(ValueError):
            database_control.execute(self.fixture.db,dict(action='database-application-binding-save',
                project='example',environment='production',authentication={},values=self.fixture.values,
                expected_revision=0,request_id='wrong-environment'),key_path=self.fixture.key)
