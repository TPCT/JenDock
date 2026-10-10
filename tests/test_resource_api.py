"""Resource policy requests use authenticated management and real durable state."""
import json
import unittest
import test_receiver as receiver_helpers
from store import connect


class ResourceApiTests(unittest.TestCase):
    tearDown=receiver_helpers.ReceiverTests.tearDown
    def setUp(self):
        receiver_helpers.ReceiverTests.setUp(self)
        policy=json.loads(self.config.read_text())
        policy['test']['container_prefixes']=['test-']
        self.config.write_text(json.dumps(policy))
        _,_,token=self.app.login({'username':'admin','password':'local-test-only'},'fixture')
        self.session=self.app.session(self.app.cookie_name+'='+token)

    def request(self,action,**values):
        return self.app.action(self.session,dict(action=action,project='test',**values))

    def test_policy_persists_without_enqueuing_or_operating_container(self):
        code,current=self.request('resource-policy')
        self.assertEqual(code,200)
        doc=current['document'];doc['budget']={'memory_mib':256,'cpu':1}
        doc['environments']['development']={'budget':{'memory_mib':128,'cpu':.5},'services':{'test-app-1':{'memory_mib':128,'cpu':.5}}}
        code,saved=self.request('resource-policy-save',revision=0,document=doc)
        self.assertEqual(code,200)
        self.assertEqual(self.request('resource-policy')[1]['document'],doc)
        self.assertEqual(self.request('resource-policy-save',revision=0,document=doc)[0],409)
        with connect(self.app.database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM control_runs').fetchone()[0],0)
        self.assertEqual(saved['revision'],1)

    def test_foreign_container_rejected_without_policy_save(self):
        doc=self.request('resource-policy')[1]['document']
        doc['environments']['production']['services']={'other-app-1':{'memory_mib':128,'cpu':.5}}
        self.assertEqual(self.request('resource-policy-save',revision=0,document=doc)[0],400)
        self.assertEqual(self.request('resource-policy')[1]['revision'],0)

    def test_malformed_documents_return_validation_response(self):
        for doc in (None, [], 'bad', {'environments':[]}, {'environments':{'production':[]}}):
            with self.subTest(doc=doc):
                self.assertEqual(self.request('resource-policy-save',revision=0,document=doc)[0],409)
        self.assertEqual(self.request('resource-policy')[1]['revision'],0)
