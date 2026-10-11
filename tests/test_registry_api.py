"""Actual API/database acceptance for dynamic metadata, without Docker side effects."""
import json
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet
import integrations
import test_receiver as receiver_helpers
from store import connect, effective_projects, valid_branch

class RegistryApiTests(unittest.TestCase):
    tearDown=receiver_helpers.ReceiverTests.tearDown
    def setUp(self):
        receiver_helpers.ReceiverTests.setUp(self)
        _,_,token=self.app.login({'username':'admin','password':'local-test-only'},'fixture')
        self.session=self.app.session(self.app.cookie_name+'='+token)

    def request(self,action,**values):
        return self.app.action(self.session,dict(action=action,**values))

    def test_dynamic_creation_two_environments_reload_and_no_json_dependency(self):
        key=self.root/'key';key.write_bytes(Fernet.generate_key());key.chmod(0o600);self.app.integration_key=str(key)
        db=connect(self.app.database)
        integrations.save_connection(db,'github',0,'fixture-token',key,verifier=lambda *args: {'api_access':'verified','identity':'fixture','repositories':[],'clone_access':'unverified','webhook_management':'unverified'})
        db.close()
        with patch('integrations.get_json',return_value={'full_name':'TPCT/new-app','id':42}):
            code,result=self.request('registry-create',project='new-app',request_id='new-project-request-123',values={'repository':'TPCT/new-app','label':'New application'})
        self.assertEqual(code,200)
        self.assertEqual(set(result['project']['environments']),{'development','production'})
        self.config.unlink()
        code,result=self.request('environment-settings',project='new-app',environment='development',revision=1,changes={'branch':'feature/preview','automatic':False})
        self.assertEqual(code,200)
        self.assertEqual(result['environment']['revision'],2)
        self.assertEqual(self.request('environment-settings',project='new-app',environment='development',revision=1,changes={'automatic':True})[0],409)
        saved=self.request('registry-get',project='new-app')[1]['project']
        self.assertEqual(saved['environments']['development']['branch'],'feature/preview')
        self.assertEqual(saved['environments']['production']['branch'],'production')
        state=self.app.state()
        self.assertIn('new-app',state['projects'])
        self.assertFalse(state['projects']['new-app']['build_ready'])
        self.assertFalse(state['projects']['new-app']['has_legacy_workload'])
        self.assertEqual(state['jobs'],[])
        self.assertEqual(state['control_runs'],[])

    def test_public_mutations_cannot_forge_provisioning_or_adoption(self):
        for changes in ({'state':'running'},{'recipe_revision':1},{'wake_on_push':True}):
            self.assertEqual(self.request('environment-settings',project='test',environment='production',revision=1,changes=changes)[0],400)
        self.assertEqual(self.request('registry-update',project='test',revision=1,changes={'legacy_binding':'production'})[0],400)
        self.assertIsNone(self.request('registry-get',project='test')[1]['project']['legacy_binding'])

    def test_import_preserves_saved_opt_out_once_and_public_state_has_no_recipe(self):
        db=connect(self.app.database)
        db.execute('INSERT INTO settings VALUES(?,?,?)',('test','saved-branch',0));db.close()
        initial=self.request('registry-get',project='test')[1]['project']
        self.assertEqual(initial['legacy_settings']['branch'],'saved-branch')
        self.assertFalse(initial['legacy_settings']['automatic'])
        self.config.write_text('{}')
        self.assertEqual(self.app.state()['projects']['test']['branch'],'saved-branch')
        self.assertNotIn('legacy_document',initial)
        self.assertNotIn('recipe',self.app.state()['projects']['test'])

    def test_nested_invalid_git_branch_components_rejected(self):
        for branch in ('feature/.hidden','feature.lock/child','a//b','branch..name'):
            self.assertFalse(valid_branch(branch))
            self.assertEqual(self.request('environment-settings',project='test',environment='development',revision=1,changes={'branch':branch})[0],409)
