"""Expired serving evidence does not discard owned renewal authority."""
import json
import math
import time
import unittest
from unittest.mock import patch
import domain_execution as execution
import registry
import test_domain_observation_authority as fixtures


class LifecycleAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ObservationAuthorityTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.late(); self.f.fixture.replace()
        self.db = self.f.db

    def expire(self):
        row = execution._row(self.db, self.f.operation['id'])
        journal = json.loads(row['journal'])
        expired = time.time() - 60
        journal['certificate']['not_after'] = expired
        journal['tls_verified']['not_after'] = expired
        self.db.execute('UPDATE domain_executions SET journal=?', (registry.canonical(journal),))

    def lifecycle(self):
        operation = dict(self.f.operation)
        _, accepted = execution._definition(self.db, operation)
        return execution._current_authority(self.db, self.f.principal, operation, accepted,
                                            execution._row(self.db, operation['id']), allow_expired=True)

    def test_expired_owned_replacement_allows_renewal_but_not_observation(self):
        self.expire()
        with self.assertRaises(ValueError): self.f.authorize()
        self.assertEqual(self.lifecycle()['container_id'], 'd' * 64)
        with self.assertRaises(ValueError): self.f.authorize()

    def test_lifecycle_still_rejects_missing_tls_history(self):
        self.db.execute("UPDATE domain_executions SET journal='{}'")
        with self.assertRaises(ValueError): self.lifecycle()

    def test_lifecycle_still_rejects_changed_stable_authority(self):
        self.expire()
        import domain_authority
        with patch.object(domain_authority, 'validate_current', side_effect=ValueError('authority revoked')):
            with self.assertRaises(ValueError): self.lifecycle()

    def test_nonfinite_expiry_is_never_valid_even_for_lifecycle(self):
        cert = {'route_id': 'r', 'operation_id': 'o', 'status': 'verified', 'sans': ['host.test'],
                'generation': 'a'*64, 'fingerprint': 'b'*64}
        for expiry in (math.inf, -math.inf, math.nan, 0, -1):
            for historical in (False, True):
                with self.subTest(expiry=expiry, historical=historical), self.assertRaises(ValueError):
                    execution._certificate_evidence(dict(cert, not_after=expiry), 'r', 'o', 'host.test', allow_expired=historical)
