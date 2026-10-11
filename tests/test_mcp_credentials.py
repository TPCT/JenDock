import hashlib
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

import application_operations as operations
import mcp_credentials as credentials


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.owner = operations.Principal('admin', 'console', credentials.SCOPES)
        self.values = {'label': 'Test client', 'scopes': ['projects:read', 'services:create'],
                       'allowed_projects': ['demo'], 'allowed_repository_ids': [42],
                       'allowed_environments': [['demo', 'development']], 'expires_at': time.time() + 3600}

    def tearDown(self):
        self.db.close()

    def test_token_returned_once_hashed_and_authenticates_immutable_principal(self):
        created = credentials.create(self.db, self.owner, self.values)
        token = created['token']
        self.assertGreaterEqual(len(token), 43)
        listed = credentials.list_credentials(self.db, self.owner)
        self.assertNotIn('token', listed[0])
        principal = credentials.authenticate(self.db, token)
        self.assertEqual(principal.subject_id, 'admin')
        self.assertEqual(principal.credential_id, created['credential']['id'])
        self.assertEqual(principal.allowed_environments, frozenset({('demo', 'development')}))
        dump = '\n'.join(self.db.iterdump())
        self.assertNotIn(token, dump)
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.assertIn(digest, dump)
        self.assertNotIn(digest, json.dumps(listed))
        with self.assertRaises(operations.OperationError):
            credentials.authenticate(self.db, digest)

    def test_revocation_and_expiry_block_request_and_worker(self):
        created = credentials.create(self.db, self.owner, self.values)
        credentials.revoke(self.db, self.owner, created['credential']['id'])
        for function, value in ((credentials.authenticate, created['token']),
                                (credentials.principal_for_credential, created['credential']['id'])):
            with self.assertRaises(operations.OperationError): function(self.db, value)
        second = credentials.create(self.db, self.owner, self.values)
        with patch.object(credentials.time, 'time', return_value=self.values['expires_at'] + 1):
            with self.assertRaises(operations.OperationError): credentials.authenticate(self.db, second['token'])

    def test_other_owners_and_mcp_cannot_manage_credentials(self):
        created = credentials.create(self.db, self.owner, self.values)
        stranger = operations.Principal('other', 'console', credentials.SCOPES)
        self.assertEqual(credentials.list_credentials(self.db, stranger), [])
        with self.assertRaises(operations.OperationError): credentials.revoke(self.db, stranger, created['credential']['id'])
        principal = credentials.authenticate(self.db, created['token'])
        with self.assertRaises(operations.OperationError): credentials.create(self.db, principal, self.values)

    def test_invalid_or_elevated_grants_are_rejected(self):
        for field, invalid in [('scopes', ['admin']), ('allowed_projects', None),
                               ('allowed_repository_ids', [True]), ('allowed_environments', [['other', 'production']]),
                               ('expires_at', float('nan')), ('expires_at', 10**400), ('label', 'bad\nlabel')]:
            with self.subTest(field=field), self.assertRaises(operations.OperationError):
                credentials.create(self.db, self.owner, dict(self.values, **{field: invalid}))
        limited = operations.Principal('admin', 'console', frozenset({'projects:read'}))
        with self.assertRaises(operations.OperationError): credentials.create(self.db, limited, self.values)

    def test_live_owner_permission_reduction_intersects_all_targets(self):
        created = credentials.create(self.db, self.owner, self.values)
        reduced = operations.Principal('admin', 'console', frozenset({'projects:read'}),
                                       allowed_projects=frozenset(), allowed_repository_ids=frozenset(),
                                       allowed_environments=frozenset())
        principal = credentials.authenticate(self.db, created['token'], owner_resolver=lambda subject: reduced)
        self.assertEqual(principal.scopes, reduced.scopes)
        self.assertEqual(principal.allowed_projects, frozenset())
        with self.assertRaises(operations.OperationError):
            credentials.principal_for_credential(self.db, created['credential']['id'], owner_resolver=lambda subject: None)

    def test_grants_immutable_and_audit_sanitized(self):
        created = credentials.create(self.db, self.owner, self.values)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE mcp_credentials SET scopes=? WHERE id=?', ('[]', created['credential']['id']))
        credentials.revoke(self.db, self.owner, created['credential']['id'])
        credentials.revoke(self.db, self.owner, created['credential']['id'])
        events = [dict(row) for row in self.db.execute('SELECT * FROM mcp_credential_events')]
        self.assertEqual([row['event'] for row in events], ['created', 'revoked'])
        self.assertNotIn(created['token'], json.dumps(events))

    def test_wrong_secret_and_target_elevation_are_denied(self):
        created = credentials.create(self.db, self.owner, self.values)
        wrong = created['token'][:-1] + ('A' if created['token'][-1] != 'A' else 'B')
        with self.assertRaises(operations.OperationError): credentials.authenticate(self.db, wrong)
        for restriction in ({'allowed_projects': frozenset({'other'})},
                            {'allowed_repository_ids': frozenset({43})},
                            {'allowed_environments': frozenset({('demo', 'production')})}):
            limited = operations.Principal('admin', 'console', credentials.SCOPES, **restriction)
            with self.subTest(restriction=restriction), self.assertRaises(operations.OperationError):
                credentials.create(self.db, limited, self.values)

    def test_revocation_cannot_be_undone_in_database(self):
        created = credentials.create(self.db, self.owner, self.values)
        credentials.revoke(self.db, self.owner, created['credential']['id'])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE mcp_credentials SET revoked_at=NULL WHERE id=?', (created['credential']['id'],))

    def test_live_console_owner_replacement_and_removed_config_fence_credentials(self):
        import os
        import tempfile
        from pathlib import Path
        created = credentials.create(self.db, self.owner, self.values)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'credentials.json'
            config.write_text(json.dumps({'username': 'admin', 'password_hash': 'not-returned'}))
            with patch.dict(os.environ, {'CREDENTIALS_FILE': str(config)}):
                self.assertEqual(credentials.authenticate(self.db, created['token']).subject_id, 'admin')
                config.write_text(json.dumps({'username': 'replacement'}))
                for call, argument in ((credentials.authenticate, created['token']),
                                       (credentials.principal_for_credential, created['credential']['id'])):
                    with self.assertRaises(operations.OperationError): call(self.db, argument)
                with self.assertRaises(operations.OperationError): credentials.create(self.db, self.owner, self.values)
                config.write_text('invalid-json-do-not-expose')
                with self.assertRaises(operations.OperationError) as invalid:
                    credentials.authenticate(self.db, created['token'])
                self.assertNotIn('invalid-json-do-not-expose', str(invalid.exception))
                config.unlink()
                with self.assertRaises(operations.OperationError):
                    credentials.principal_for_credential(self.db, created['credential']['id'])

    def test_runtime_config_requires_valid_current_username_and_sanitizes_errors(self):
        import os
        import tempfile
        from pathlib import Path
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            credentials.validate_owner_configuration()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'credentials.json'
            for document in ('not-json-private-data', '[]', '{}', '{"username":""}', '{"username":42}'):
                config.write_text(document)
                with patch.dict(os.environ, {'CREDENTIALS_FILE': str(config)}), self.assertRaises(ValueError) as error:
                    credentials.validate_owner_configuration()
                self.assertNotIn('private-data', str(error.exception))
                self.assertNotIn(str(config), str(error.exception))
