"""Console service adapters retain authentication attribution and structured failures."""
import unittest
from unittest.mock import patch
import application_operations as operations
import test_receiver as helpers


class ServiceApiTests(unittest.TestCase):
    tearDown = helpers.ReceiverTests.tearDown
    def setUp(self):
        helpers.ReceiverTests.setUp(self)
        _, _, token = self.app.login({'username': 'admin', 'password': 'local-test-only'}, 'fixture')
        self.session = self.app.session(self.app.cookie_name + '=' + token)

    def test_console_actor_cannot_be_replaced_by_payload(self):
        values = {'name': 'web'}
        result = {'service': {'id': 'service'}, 'operation': {'status': 'queued'}}
        with patch.object(operations, 'create_service', return_value=result) as create:
            code, actual = self.app.action(self.session, {'action': 'service-create', 'project': 'test',
                'environment': 'development', 'values': values, 'request_id': 'request-1234567890',
                'actor': 'attacker', 'scopes': ['anything']})
        self.assertEqual((code, actual), (202, result))
        principal = create.call_args.args[1]
        self.assertEqual(principal.subject_id, 'admin')
        self.assertEqual(principal.actor_kind, 'console')
        self.assertIn('services:start', principal.scopes)
        self.assertNotIn('anything', principal.scopes)

    def test_template_and_list_target_environment_is_explicit(self):
        for action, function, result, expected in (
            ('service-templates', 'service_templates', {'templates': []}, {'templates': []}),
            ('service-list', 'list_services', [], {'services': []}),
        ):
            with self.subTest(action=action), patch.object(operations, function, return_value=result) as call:
                code, body = self.app.action(self.session, {'action': action, 'project': 'test', 'environment': 'production'})
                self.assertEqual((code, body), (200, expected))
                self.assertEqual(call.call_args.args[-2:], ('test', 'production'))

    def test_version_history_forwards_scoped_pagination(self):
        result = {'versions': [], 'next_cursor': None, 'evidence': 'recorded'}
        with patch.object(operations, 'service_versions', return_value=result) as call:
            code, body = self.app.action(self.session, {'action': 'service-versions', 'project': 'test',
                'environment': 'production', 'limit': 10, 'cursor': 'opaque-cursor'})
        self.assertEqual((code, body), (200, result))
        self.assertEqual(call.call_args.args[-2:], ('test', 'production'))
        self.assertEqual(call.call_args.kwargs, {'limit': 10, 'cursor': 'opaque-cursor'})
        self.assertIn('services:read', call.call_args.args[1].scopes)

    def test_typed_failure_preserves_recoverable_code(self):
        with patch.object(operations, 'create_service', side_effect=operations.OperationError('capacity_unavailable', 'Choose a smaller allocation.')):
            code, body = self.app.action(self.session, {'action': 'service-create', 'project': 'test',
                'environment': 'development', 'values': {}, 'request_id': 'request-1234567890'})
        self.assertEqual(code, 409)
        self.assertEqual(body['code'], 'capacity_unavailable')

    def test_invalid_environment_is_rejected_before_queuing(self):
        with patch.object(operations, 'create_service') as create:
            code, _ = self.app.action(self.session, {'action': 'service-create', 'project': 'test',
                'environment': 'staging', 'values': {}, 'request_id': 'request-1234567890'})
        self.assertGreaterEqual(code, 400)
        create.assert_not_called()
