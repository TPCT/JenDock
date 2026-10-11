import time
import unittest
import test_receiver as fixtures
from store import connect
import mcp_credentials

class McpAccessApiTests(unittest.TestCase):
    tearDown = fixtures.ReceiverTests.tearDown
    def setUp(self):
        fixtures.ReceiverTests.setUp(self)
        _, _, token = self.app.login({'username':'admin','password':'local-test-only'},'local')
        self.session = self.app.session(self.app.cookie_name+'='+token)

    def test_create_list_revoke_owner_from_session_and_no_secret_replay(self):
        values={'label':'Test client','scopes':['projects:read'],'allowed_projects':['test'],
                'allowed_repository_ids':[12],'allowed_environments':[['test','development']],
                'expires_at':time.time()+3600}
        code, issued=self.app.action(self.session,{'action':'mcp-access-create','values':values,'owner':'attacker'})
        self.assertEqual(code,201); self.assertEqual(issued['credential']['owner'],'admin')
        code, result=self.app.action(self.session,{'action':'mcp-access-list'})
        self.assertEqual(code,200); self.assertEqual(len(result['credentials']),1)
        self.assertNotIn(issued['token'],str(result)); self.assertNotIn('token_digest',str(result))
        code,result=self.app.action(self.session,{'action':'mcp-access-revoke','credential_id':issued['credential']['id']})
        self.assertEqual(code,200); self.assertTrue(result['credential']['revoked_at'])
        with connect(self.app.database) as db:
            with self.assertRaises(ValueError):mcp_credentials.authenticate(db,issued['token'])
