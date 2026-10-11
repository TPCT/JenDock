"""Independent domain-intent authorization and stale-evidence regressions."""
import copy
import dataclasses
import unittest
from unittest.mock import patch

import application_operations as ops
import ingress
import managed_services as services
import registry
import test_ingress as fixture


class IngressReviewTests(unittest.TestCase):
    setUp = fixture.IngressTests.setUp
    tearDown = fixture.IngressTests.tearDown
    preview = fixture.IngressTests.preview
    accepted_values = fixture.IngressTests.accepted_values
    accept = fixture.IngressTests.accept

    def test_replay_rechecks_captured_source_environment_grant(self):
        self.artifact['source_environment']='production'
        self.service['binding']['definition_digest']=services.digest(self.definition)
        self.principal=dataclasses.replace(self.principal,allowed_environments=frozenset({('app','development'),('app','production')}))
        values=self.accepted_values();self.accept(values)
        restricted=dataclasses.replace(self.principal,allowed_environments=frozenset({('app','development')}))
        with self.assertRaises(ops.OperationError) as raised:self.accept(values,principal=restricted)
        self.assertEqual(raised.exception.status,403)
        self.assertEqual(self.db.execute('SELECT count(*) FROM ingress_routes').fetchone()[0],1)

    def test_boolean_revision_pins_are_not_integer_evidence(self):
        values=self.accepted_values()
        self.assertEqual(values['expected_environment_revision'],1)
        with self.assertRaises(ops.OperationError):self.accept(dict(values,expected_environment_revision=True))
        self.assertEqual(self.db.execute('SELECT count(*) FROM ingress_routes').fetchone()[0],0)

    def test_same_revision_zone_status_change_blocks_acceptance(self):
        values=self.accepted_values()
        self.metadata['zones'][0]['status']='pending'
        self.db.execute("UPDATE integration_connections SET metadata=? WHERE provider='cloudflare'",(registry.canonical(self.metadata),))
        with self.assertRaises(ops.OperationError):self.accept(values)
        self.assertEqual(self.db.execute('SELECT count(*) FROM ingress_routes').fetchone()[0],0)

    def test_hostname_reservation_is_global_across_environments(self):
        development_values=self.accepted_values()
        original=copy.deepcopy(self.service)
        self.service['environment']='production';self.service['definition']['environment']='production'
        self.service['binding']['definition_digest']=services.digest(self.service['definition'])
        preview=ingress.preview(self.db,self.principal,'app','production',self.values)
        production_values=dict(self.values,**{key:preview[key] for key in ingress.PINS})
        production=copy.deepcopy(self.service)
        self.service=original;self.accept(development_values)
        self.service=production
        with self.assertRaisesRegex(ops.OperationError,'reserved'):
            ingress.accept(self.db,self.principal,'app','production',production_values,'production-domain-12345')
        self.assertEqual(self.db.execute('SELECT count(*) FROM ingress_routes').fetchone()[0],1)

    def test_revocation_between_initial_authorization_and_transaction_rejects(self):
        values=self.accepted_values()
        calls=[]
        def recheck(*args):
            calls.append(1)
            if len(calls)>1:raise ops.OperationError('credential_unavailable','Revoked.',403)
        with patch.object(ops,'recheck_credential',side_effect=recheck):
            with self.assertRaises(ops.OperationError):self.accept(values)
        self.assertEqual(self.db.execute('SELECT count(*) FROM ingress_routes').fetchone()[0],0)
