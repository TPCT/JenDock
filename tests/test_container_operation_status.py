import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from server import Application
from store import connect

class ContainerOperationStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.app=Application.__new__(Application);self.app.database=str(Path(self.tmp.name)/'db');self.app.config='unused'
        self.db=connect(self.app.database);self.addCleanup(self.db.close)
        self.body={'action':'container-operation-status','request_id':'r'*20,'project':'app','container':'app-1','operation':'container-restart'}
        self.db.execute('INSERT INTO control_runs(request_id,created,updated,actor,project,container,operation,status,detail) VALUES(?,?,?,?,?,?,?,?,?)',('r'*20,1,2,'owner','app','app-1','container-restart','recovery-required','must not expose diagnostic secrets'))
    def read(self,body=None,user='owner'):
        with patch('server.effective_projects',return_value={'app':{'name':'app'}}),patch('server.socket.socket') as effect:
            result=self.app.action({'username':user,'token':'digest'},body or self.body)
            effect.assert_not_called()
            return result
    def test_reads_old_original_operation_without_dispatch_or_raw_diagnostics(self):
        for n in range(105):
            self.db.execute('INSERT INTO control_runs(request_id,created,updated,actor,project,container,operation,status,detail) VALUES(?,?,?,?,?,?,?,?,?)',(str(n),3,3,'owner','app','app-1','container-stop','succeeded',''))
        status,result=self.read()
        self.assertEqual(status,200);self.assertEqual(result['operation']['status'],'recovery-required')
        self.assertEqual(result['operation']['request_id'],'r'*20)
        self.assertNotIn('diagnostic secrets',str(result))
    def test_identity_and_actor_mismatch_cannot_read_another_request(self):
        for key,value in [('container','other'),('operation','container-stop'),('request_id','x'*20)]:
            with self.subTest(key=key):self.assertEqual(self.read(dict(self.body,**{key:value}))[0],404)
        self.assertEqual(self.read(user='other')[0],404)
    def test_closed_input(self):
        self.assertEqual(self.read(dict(self.body,changes={'SECRET':'x'}))[0],400)

    def test_state_feed_is_actor_scoped_and_sanitized(self):
        with patch('server.effective_projects',return_value={}):
            self.assertEqual(self.app.state()['control_runs'],[])
            self.assertEqual(self.app.state(actor='other')['control_runs'],[])
            rows=self.app.state(actor='owner')['control_runs']
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['request_id'],'r'*20)
            self.assertNotIn('diagnostic secrets',str(rows))
