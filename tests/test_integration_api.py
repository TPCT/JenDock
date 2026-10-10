import json
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet
import test_receiver as receiver_helpers
from store import connect

class IntegrationApiTests(unittest.TestCase):
    tearDown=receiver_helpers.ReceiverTests.tearDown
    def setUp(self):
        receiver_helpers.ReceiverTests.setUp(self)
        self.key=self.root/'integration.key';self.key.write_bytes(Fernet.generate_key());self.key.chmod(0o600)
        self.app.integration_key=str(self.key)
        _,_,token=self.app.login({'username':'admin','password':'local-test-only'},'fixture')
        self.session=self.app.session(self.app.cookie_name+'='+token)

    def action(self,action,**args):
        return self.app.action(self.session,dict(action=action,**args))

    def test_encrypted_replace_test_disable_and_public_redaction(self):
        metadata={'api_access':'verified','identity':'fixture-user','repositories':[],'clone_access':'unverified','webhook_management':'unverified'}
        with patch('integrations.verify',return_value=metadata):
            code,result=self.action('integrations-save',provider='github',revision=0,credential='synthetic-credential-unique')
            self.assertEqual(code,200)
            self.assertTrue(result['connection']['configured'])
            self.assertNotIn('synthetic-credential-unique',json.dumps(result))
            self.assertEqual(self.action('integrations-test',provider='github')[0],200)
        with patch('integrations.verify',side_effect=RuntimeError('synthetic-credential-unique')):
            failed=self.action('integrations-save',provider='github',revision=1,credential='replacement-unique')
            self.assertEqual(failed[0],409)
            self.assertNotIn('synthetic-credential-unique',json.dumps(failed))
        self.assertEqual(self.action('integrations-list')[1]['connections']['github']['revision'],1)
        code,result=self.action('integrations-disable',provider='github',revision=1)
        self.assertEqual(code,200);self.assertFalse(result['connection']['enabled'])
        self.assertNotIn('synthetic-credential-unique',json.dumps(self.app.state()))
        with connect(self.app.database) as db:
            self.assertNotIn('synthetic-credential-unique',db.execute('SELECT ciphertext FROM integration_connections').fetchone()[0].decode())
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)

    def test_missing_key_and_bad_provider_fail_without_secret_echo(self):
        self.key.unlink()
        for provider in ('github','unknown'):
            code,result=self.action('integrations-save',provider=provider,revision=0,credential='synthetic-credential-unique')
            self.assertEqual(code,409)
            self.assertNotIn('synthetic-credential-unique',json.dumps(result))
        self.assertFalse(self.action('integrations-list')[1]['connections']['github']['configured'])
