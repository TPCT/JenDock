import base64
import hashlib
import hmac
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from server import Application, handler
from store import connect


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'projects.json'
        self.config.write_text(json.dumps({'test': {'repository': 'TPCT/test', 'repository_id': 123,
            'branch': 'main', 'enabled': True}}))
        self.credentials = self.root / 'credentials.json'
        self.credentials.write_text(json.dumps({'username': 'admin', 'salt': 'ab' * 16,
            'password_hash': hashlib.pbkdf2_hmac('sha256', b'local-test-only', bytes.fromhex('ab' * 16), 260000).hex(),
            'webhook_secret': 'test-secret-only'}))
        self.app = Application(self.config, self.root / 'state.sqlite', self.credentials)
        self.payload = {'repository': {'full_name': 'TPCT/test', 'id': 123},
                        'ref': 'refs/heads/main', 'after': 'a' * 40}

    def tearDown(self):
        self.temp.cleanup()

    def deliver(self, payload=None, delivery='delivery-1', **headers):
        body = json.dumps(self.payload if payload is None else payload).encode()
        signed = {'X-Hub-Signature-256': 'sha256=' + hmac.new(b'test-secret-only', body, hashlib.sha256).hexdigest(),
                  'X-GitHub-Delivery': delivery, 'X-GitHub-Event': 'push'}
        signed.update(headers)
        return self.app.webhook(signed, body)

    def test_valid_delivery_persists_and_duplicate_does_not_enqueue_again(self):
        first = self.deliver()
        second = self.deliver()
        self.assertEqual(first[0], 202)
        self.assertTrue(first[1]['new'])
        self.assertFalse(second[1]['new'])
        self.assertEqual(first[1]['jobs'], second[1]['jobs'])
        reopened = Application(self.config, self.root / 'state.sqlite', self.credentials)
        self.assertEqual(reopened.state()['jobs'][0]['sha'], 'a' * 40)

    def test_invalid_signature_cannot_enqueue(self):
        self.assertEqual(self.deliver(**{'X-Hub-Signature-256': 'sha256=wrong'})[0], 401)
        self.assertEqual(self.app.state()['jobs'], [])

    def test_wrong_branch_repo_identity_and_deleted_ref_are_ignored(self):
        for field, value in [('ref', 'refs/heads/other'), ('repository', {'full_name': 'TPCT/test', 'id': 456}),
                              ('repository', {'full_name': 'someone/test', 'id': 123}), ('deleted', True)]:
            body = dict(self.payload)
            body[field] = value
            self.assertIn('ignored', self.deliver(body)[1])
        self.assertEqual(self.app.state()['jobs'], [])

    def test_disabled_project_never_enters_deploy_queue(self):
        config = json.loads(self.config.read_text())
        config['test']['enabled'] = False
        self.config.write_text(json.dumps(config))
        self.deliver()
        self.assertEqual(self.app.state()['jobs'][0]['status'], 'awaiting-validation')

    def test_newer_push_supersedes_only_pending_job(self):
        self.deliver()
        body = dict(self.payload, after='b' * 40)
        self.deliver(body, delivery='delivery-2')
        jobs = self.app.state()['jobs']
        self.assertEqual([j['status'] for j in jobs], ['queued', 'superseded'])

    def test_concurrent_delivery_is_deduplicated(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.deliver())) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(r[1]['new'] for r in results), 1)

    def test_invalid_payload_and_ping(self):
        self.assertEqual(self.deliver({'after': '$(bad)'})[0], 400)
        self.assertEqual(self.deliver(**{'X-GitHub-Event': 'ping'})[1], {'accepted': 'ping'})

    def test_auth_and_read_only_http_contract(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = 'http://127.0.0.1:%d/_deploy/api/state' % server.server_port
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(url)
            self.assertEqual(err.exception.code, 401)
            err.exception.close()
            auth = 'Basic ' + base64.b64encode(b'admin:local-test-only').decode()
            request = urllib.request.Request(url, headers={'Authorization': auth})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                value = json.load(response)
                self.assertNotIn('password_hash', json.dumps(value))
                self.assertNotIn('webhook_secret', json.dumps(value))
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(urllib.request.Request(url, data=b'{}', headers={'Authorization': auth}))
            self.assertEqual(err.exception.code, 404)
            err.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
