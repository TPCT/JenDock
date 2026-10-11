import dataclasses
import json
import unittest

import application_operations as ops
import managed_services
import test_managed_services as fixtures


class OperationStatusTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagedServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.db, self.principal = self.fixture.db, self.fixture.principal
        self.created = self.fixture.create()
        self.operation_id = self.created['operation']['operation_id']

    def status(self, **kwargs):
        return ops.operation_status(self.db, self.principal, self.operation_id, **kwargs)

    def test_paginated_safe_history_stage_and_console_compatibility(self):
        managed_services._update(self.db, self.operation_id, 'provisioning', 'SECRET worker command')
        managed_services._update(self.db, self.operation_id, 'recovery-required', 'SECRET environment')
        for event in ('recovery-authority:SECRET->SECRET', 'SECRET shell output'):
            self.db.execute('INSERT INTO application_operation_events(operation_id,actor,event,created) VALUES(?,?,?,?)',
                            (self.operation_id, 'SECRET actor', event, 1.0))
        self.db.execute('CREATE TABLE service_provisioning_journals(operation_id TEXT PRIMARY KEY, document TEXT, updated REAL)')
        self.db.execute('INSERT INTO service_provisioning_journals VALUES(?,?,?)', (self.operation_id,
            json.dumps({'phase':'start-intent','command':'SECRET','env':'SECRET','path':'SECRET'}), 2.0))
        first = self.status(limit=2)
        self.assertEqual([event['code'] for event in first['events']], ['service-queued', 'provisioning'])
        self.assertEqual(first['next_after_event'], first['last_event'])
        second = self.status(after_event=first['next_after_event'], limit=2)
        self.assertEqual([event['code'] for event in second['events']], ['recovery-required', 'recovery-authority-updated'])
        third = self.status(after_event=second['next_after_event'], limit=2)
        self.assertEqual([event['code'] for event in third['events']], ['operation-updated'])
        self.assertIsNone(third['next_after_event'])
        self.assertEqual(third['stage']['code'], 'start-intent')
        self.assertTrue(third['recovery']['required'])
        self.assertFalse(third['recovery']['mcp_available'])
        self.assertNotIn('SECRET', json.dumps([first, second, third]))
        empty = self.status(after_event=third['last_event'])
        self.assertEqual(empty['events'], [])
        self.assertEqual(empty['last_event'], third['last_event'])
        # Existing console API still receives its existing operation object.
        self.assertEqual(ops.get_operation(self.db,self.principal,self.operation_id)['detail'], 'SECRET environment')

    def test_unauthorized_owner_project_environment_and_scope_are_denied(self):
        for principal in (dataclasses.replace(self.principal, subject_id='other'),
                          dataclasses.replace(self.principal, scopes=frozenset({'services:read'})),
                          dataclasses.replace(self.principal, allowed_projects=frozenset({'other'})),
                          dataclasses.replace(self.principal, allowed_environments=frozenset({('app','production')}))):
            with self.subTest(principal=principal):
                with self.assertRaises(ops.OperationError):
                    ops.operation_status(self.db,principal,self.operation_id)

    def test_bounds_unknown_stage_and_foreign_events(self):
        for kwargs in ({'limit':0},{'limit':101},{'limit':True},{'after_event':-1},{'after_event':True},{'after_event':2**63}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ops.OperationError): self.status(**kwargs)
        self.db.execute('INSERT INTO application_operation_events(operation_id,actor,event,created) VALUES(?,?,?,?)',
                        ('foreign', 'owner','SECRET foreign',1.0))
        self.db.execute('CREATE TABLE service_provisioning_journals(operation_id TEXT PRIMARY KEY, document TEXT, updated REAL)')
        self.db.execute('INSERT INTO service_provisioning_journals VALUES(?,?,?)',
                        (self.operation_id, json.dumps({'phase':'SECRET unknown phase'}),2.0))
        result = self.status()
        self.assertEqual(len(result['events']),1)
        self.assertEqual(result['stage']['code'],'unknown')
        self.assertNotIn('SECRET',json.dumps(result))
        self.assertFalse(result['recovery']['required'])

if __name__ == '__main__': unittest.main()
