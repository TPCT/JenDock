import importlib.util
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.db = sqlite3.connect(self.path / 'state.sqlite', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.key = self.path / 'external.key'
        self.key.write_bytes(Fernet.generate_key()); self.key.chmod(0o600)

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def service(self):
        self.assertIsNotNone(importlib.util.find_spec('integrations'), 'Integration service is required')
        import integrations
        return integrations

    def verified(self, provider, token, options):
        return {'api_access': 'verified', 'identity': 'fixture-user', 'repository_access': 'metadata-verified',
                'clone_access': 'unverified', 'webhook_management': 'unverified'}

    def test_encrypted_persistence_and_no_secret_in_public_metadata(self):
        service = self.service()
        result = service.save_connection(self.db, 'github', 0, 'test-private-value', self.key, verifier=self.verified, actor='owner')
        self.assertEqual(result['revision'], 1)
        self.assertTrue(result['configured'])
        self.assertNotIn('test-private-value', json.dumps(service.list_connections(self.db)))
        self.assertNotIn(b'test-private-value', (self.path / 'state.sqlite').read_bytes())
        self.assertEqual(service.credential_for_use(self.db, 'github', self.key), 'test-private-value')

    def test_failed_replacement_preserves_previous_and_never_echoes_upstream_error(self):
        service = self.service()
        service.save_connection(self.db, 'github', 0, 'original-value', self.key, verifier=self.verified)
        def fail(*args): raise RuntimeError('Bearer candidate-secret upstream raw body')
        with self.assertRaises(ValueError) as caught:
            service.save_connection(self.db, 'github', 1, 'candidate-secret', self.key, verifier=fail)
        self.assertNotIn('candidate-secret', str(caught.exception))
        self.assertEqual(service.credential_for_use(self.db, 'github', self.key), 'original-value')
        self.assertEqual(service.list_connections(self.db)['github']['revision'], 1)
        audit = str([dict(row) for row in self.db.execute('SELECT * FROM integration_events')])
        self.assertNotIn('candidate-secret', audit)

    def test_wrong_or_missing_key_fails_closed_without_replacing_connections(self):
        service = self.service()
        service.save_connection(self.db, 'github', 0, 'original-value', self.key, verifier=self.verified)
        wrong = self.path / 'wrong.key'; wrong.write_bytes(Fernet.generate_key()); wrong.chmod(0o600)
        for path in (wrong, self.path / 'absent.key'):
            with self.assertRaises(ValueError): service.credential_for_use(self.db, 'github', path)
            with self.assertRaises(ValueError): service.save_connection(self.db, 'github', 1, 'new-value', path, verifier=self.verified)
        self.assertFalse((self.path / 'absent.key').exists())
        self.assertEqual(service.credential_for_use(self.db, 'github', self.key), 'original-value')

    def test_disable_is_local_and_revision_guarded(self):
        service = self.service()
        service.save_connection(self.db, 'github', 0, 'original-value', self.key, verifier=self.verified)
        result = service.disable_connection(self.db, 'github', 1, actor='owner')
        self.assertFalse(result['enabled'])
        self.assertTrue(result['configured'])
        with self.assertRaises(ValueError): service.credential_for_use(self.db, 'github', self.key)
        with self.assertRaises(ValueError): service.disable_connection(self.db, 'github', 1)

    def test_stale_replacement_during_verification_cannot_overwrite_newer_save(self):
        service = self.service()
        service.save_connection(self.db, 'github', 0, 'original-value', self.key, verifier=self.verified)
        def race(provider, token, options):
            service.disable_connection(self.db, 'github', 1)
            return self.verified(provider, token, options)
        with self.assertRaises(ValueError):
            service.save_connection(self.db, 'github', 1, 'candidate-value', self.key, verifier=race)
        self.assertEqual(service.list_connections(self.db)['github']['revision'], 2)
        self.assertFalse(service.list_connections(self.db)['github']['enabled'])

    def test_cloudflare_account_token_fallback_and_sanitized_zones(self):
        service = self.service(); calls = []
        account = 'a' * 32; zone = 'b' * 32
        def get(provider, path, token):
            calls.append(path)
            if path.startswith('/zones?'):
                return {'success': True, 'result': [{'id': zone, 'name': 'example.test', 'status': 'active', 'account': {'id': account}, 'untrusted': token}], 'result_info': {'total_pages': 1}}
            if path == '/user/tokens/verify': raise service.IntegrationError('Request rejected (HTTP 403).')
            if path == '/accounts/' + account + '/tokens/verify':
                return {'success': True, 'result': {'status': 'active'}}
            self.fail(path)
        result = service.verify_cloudflare('fixture-token', {}, get)
        self.assertEqual(result['token_kind'], 'account')
        self.assertEqual(result['dns_write'], 'unverified')
        self.assertNotIn('fixture-token', json.dumps(result))
        self.assertIn('/accounts/' + account + '/tokens/verify', calls)

    def test_github_reports_metadata_access_separately_from_clone_and_webhook(self):
        service = self.service()
        def get(provider, path, token):
            if path == '/user': return {'login': 'owner', 'id': 7, 'untrusted': token}
            if path.startswith('/user/repos?'): return [{'full_name': 'owner/project', 'id': 8, 'private': True, 'permissions': {'admin': True}}]
            self.fail(path)
        result = service.verify_github('fixture-token', {}, get)
        self.assertEqual(result['repository_access'], 'metadata-verified')
        self.assertEqual(result['clone_access'], 'unverified')
        self.assertEqual(result['webhook_management'], 'unverified')
        self.assertNotIn('fixture-token', json.dumps(result))

    def test_validation_and_metadata_secret_rejection(self):
        service = self.service()
        for token in ('', 'x\nAuthorization: extra', 'x' * 9000):
            with self.assertRaises(ValueError): service.save_connection(self.db, 'github', 0, token, self.key, verifier=self.verified)
        def injected(provider, token, options): return {'api_access': 'verified', 'identity': token}
        with self.assertRaises(ValueError):
            service.save_connection(self.db, 'github', 0, 'never-return-this-value', self.key, verifier=injected)

    def test_cross_provider_first_save_cannot_mix_encryption_keys(self):
        service = self.service()
        wrong = self.path / 'other.key'; wrong.write_bytes(Fernet.generate_key()); wrong.chmod(0o600)
        def race(provider, token, options):
            service.save_connection(self.db, 'cloudflare', 0, 'cloudflare-value', wrong,
                                    verifier=lambda *args: {'api_access': 'verified', 'token_kind': 'account'})
            return self.verified(provider, token, options)
        with self.assertRaises(ValueError):
            service.save_connection(self.db, 'github', 0, 'github-value', self.key, verifier=race)
        self.assertFalse(service.list_connections(self.db)['github']['configured'])

    def test_testing_disabled_connection_does_not_enable_or_reveal_it(self):
        service = self.service()
        service.save_connection(self.db, 'github', 0, 'fixture-credential', self.key, verifier=self.verified)
        service.disable_connection(self.db, 'github', 1)
        result = service.test_connection(self.db, 'github', self.key, verifier=self.verified)
        self.assertFalse(result['enabled'])
        self.assertEqual(result['revision'], 2)
        self.assertNotIn('fixture-credential', json.dumps(result))

    def test_ciphertext_cannot_be_substituted_between_providers(self):
        service = self.service()
        service.save_connection(self.db, 'github', 0, 'fixture-credential', self.key, verifier=self.verified)
        self.db.execute("UPDATE integration_connections SET provider='cloudflare'")
        with self.assertRaises(ValueError): service.credential_for_use(self.db, 'cloudflare', self.key)

    def test_http_transport_uses_fixed_get_timeout_and_rejects_redirects(self):
        service = self.service()
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit):
                self.limit = limit
                return b'{"login":"owner"}'
        class Opener:
            def open(inner, request, timeout):
                self.assertEqual(request.full_url, 'https://api.github.com/user')
                self.assertEqual(request.method, 'GET')
                self.assertEqual(timeout, 12)
                self.assertEqual(request.get_header('Authorization'), 'Bearer fixture-value')
                return Response()
        with patch('urllib.request.build_opener', return_value=Opener()):
            self.assertEqual(service.get_json('github', '/user', 'fixture-value'), {'login': 'owner'})
        self.assertIsNone(service._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.invalid/'))
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.side_effect = urllib.error.HTTPError('https://api.github.com/user', 403, 'fixture-value', {}, None)
            with self.assertRaises(ValueError) as caught:
                service.get_json('github', '/user', 'fixture-value')
            self.assertNotIn('fixture-value', str(caught.exception))

    def test_provider_pagination_is_bounded_and_marks_incomplete_catalog(self):
        service = self.service(); calls = []
        def get(provider, path, token):
            calls.append(path)
            if path == '/user': return {'login': 'owner'}
            return [{'id': index, 'full_name': 'owner/repo' + str(index)} for index in range(100)]
        result = service.verify_github('fixture-credential', {}, get)
        self.assertTrue(result['repositories_truncated'])
        self.assertEqual(len(calls), 6)

    def test_oversized_provider_response_has_sanitized_actionable_failure(self):
        service = self.service()
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return b'x' * limit
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.return_value = Response()
            with self.assertRaises(ValueError) as caught:
                service.get_json('github', '/user', 'fixture-value')
        self.assertIn('size', str(caught.exception))

    def test_cloudflare_user_failure_without_account_id_is_not_invalid_token_verdict(self):
        service = self.service()
        def get(provider, path, token):
            if path.startswith('/zones?'): return {'success': True, 'result': [], 'result_info': {'total_pages': 1}}
            raise service.IntegrationError('Request rejected (HTTP 403).')
        with self.assertRaises(ValueError) as caught:
            service.verify_cloudflare('fixture-value', {}, get)
        self.assertIn('account ID', str(caught.exception))
        self.assertNotIn('invalid token', str(caught.exception).lower())

    def test_save_preserves_fixed_recovery_hint_but_not_arbitrary_error_text(self):
        service = self.service()
        hint = 'Token activity could not be verified. For an account-owned token, provide its account ID and check token-verification access.'
        def known(*args): raise service.IntegrationError(hint)
        with self.assertRaises(ValueError) as caught:
            service.save_connection(self.db, 'cloudflare', 0, 'fixture-value', self.key, verifier=known)
        self.assertIn('account ID', str(caught.exception))
        def unknown(*args): raise service.IntegrationError('upstream Authorization: exposed-other-secret')
        with self.assertRaises(ValueError) as caught:
            service.save_connection(self.db, 'cloudflare', 0, 'fixture-value', self.key, verifier=unknown)
        self.assertNotIn('exposed-other-secret', str(caught.exception))


if __name__ == '__main__': unittest.main()
