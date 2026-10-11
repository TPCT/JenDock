import json
import sys
import unittest
import tempfile
import database_helper as helper

class ExchangeTests(unittest.TestCase):
    def test_private_input_and_output(self):
        value=helper.exchange([sys.executable,'-c','import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())'],b'private')
        self.assertEqual(value,b'private')
    def test_output_and_error_limits(self):
        for stream in ('stdout','stderr'):
            with self.subTest(stream=stream),self.assertRaises(helper.HelperError):
                helper.exchange([sys.executable,'-c',f'import sys;sys.{stream}.write("x"*300000)'])
    def test_deadline(self):
        with self.assertRaisesRegex(helper.HelperError,'timeout'):
            helper.exchange([sys.executable,'-c','import time;time.sleep(20)'],timeout=.1)

    def test_closed_stream_child_timeout_is_sanitized(self):
        with self.assertRaisesRegex(helper.HelperError,'timeout'):
            helper.exchange([sys.executable,'-c','import os,time;os.close(0);os.close(1);os.close(2);time.sleep(20)'],timeout=.1)

class HelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.calls=[];self.image='sha256:'+'a'*64;self.db='b'*64;self.id='c'*64
        self.client=helper.InspectorHelper(docker='/usr/bin/docker',socket='unix:///tmp/fixture.sock',image=self.image,state_dir=self.tmp.name,runner=self.fake_run)
    def fake_run(self,argv,payload,**kwargs):
        self.calls.append((argv,payload))
        if argv[1]=='create':
            self.name=argv[argv.index('--name')+1];self.nonce=argv[argv.index('--label')+1].split('=',1)[1]
            return self.id.encode()
        if argv[1]=='inspect':return json.dumps([{'Id':self.id,'Name':'/'+self.name,'Image':self.image,'State':{'Status':'exited','Running':False,'ExitCode':0},'Mounts':[],'HostConfig':{'NetworkMode':'container:'+self.db,'ReadonlyRootfs':True,'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges:true'],'Memory':128*1024*1024,'NanoCpus':250000000,'PidsLimit':32,'LogConfig':{'Type':'none'}},'Config':{'User':'65534:65534','Entrypoint':['python'],'Cmd':['/app/database_inspector_entry.py'],'Labels':{'net.shiftcodes.database-inspection':self.nonce}}}]).encode()
        if argv[1]=='start':return b'{"ok":true}'
        return b''
    def test_secret_only_in_private_stdin_and_exact_cleanup(self):
        checks=[]
        self.assertEqual(self.client.inspect(self.db,{'password':'secret'},revalidate=checks.append),{'ok':True})
        self.assertEqual(checks,[self.db]*3)
        self.assertNotIn('secret',json.dumps([a for a,p in self.calls]))
        self.assertEqual(self.calls[-2][0],['/usr/bin/docker','rm','--force',self.id])
        create=self.calls[0][0]
        self.assertIn('container:'+self.db,create);self.assertIn('--read-only',create)
    def test_changed_authority_cleans_helper_without_start(self):
        calls=[]
        def check(value):
            calls.append(value)
            if len(calls)>1:raise ValueError('stale')
        with self.assertRaisesRegex(ValueError,'stale'):self.client.inspect(self.db,{},revalidate=check)
        self.assertFalse(any(a[1]=='start' for a,p in self.calls))
        self.assertEqual(self.calls[-2][0][1:3],['rm','--force'])
    def test_attach_timeout_removes_actual_helper(self):
        original=self.client.runner
        def failing(argv,payload,**kwargs):
            if argv[1]=='start':raise helper.HelperError('timeout')
            return original(argv,payload,**kwargs)
        self.client.runner=failing
        with self.assertRaisesRegex(helper.HelperError,'timeout'):
            self.client.inspect(self.db,{},revalidate=lambda _:None)
        self.assertEqual(self.calls[-2][0],['/usr/bin/docker','rm','--force',self.id])
    def test_ambiguous_create_reconciles_nonce_before_cleanup(self):
        original=self.client.runner
        def uncertain(argv,payload,**kwargs):
            result=original(argv,payload,**kwargs)
            if argv[1]=='create':raise helper.HelperError('timeout')
            return result
        self.client.runner=uncertain
        with self.assertRaises(helper.HelperError):self.client.inspect(self.db,{},revalidate=lambda _:None)
        self.assertFalse(any(a[1]=='start' for a,p in self.calls))
        self.assertEqual(self.calls[-2][0],['/usr/bin/docker','rm','--force',self.id])
    def test_oversized_request_does_not_create(self):
        with self.assertRaisesRegex(helper.HelperError,'request_limit'):
            self.client.inspect(self.db,{'secret':'x'*40000},revalidate=lambda _:None)
        self.assertEqual(self.calls,[])

    def test_duplicate_and_nonfinite_results_rejected(self):
        for raw in (b'{"x":NaN}',b'{"x":1,"x":2}'):
            original=self.fake_run
            def malformed(argv,payload,**kwargs):
                result=original(argv,payload,**kwargs)
                return raw if argv[1]=='start' else result
            self.client.runner=malformed
            with self.assertRaisesRegex(helper.HelperError,'invalid_response'):
                self.client.inspect(self.db,{},revalidate=lambda _:None)
    def test_failed_cleanup_retains_recoverable_intent(self):
        original=self.fake_run
        def failed(argv,payload,**kwargs):
            if argv[1]=='rm':raise helper.HelperError('timeout')
            return original(argv,payload,**kwargs)
        self.client.runner=failed
        with self.assertRaises(helper.HelperError):self.client.inspect(self.db,{},revalidate=lambda _:None)
        with self.assertRaisesRegex(helper.HelperError,'recovery_required'):
            self.client.inspect(self.db,{},revalidate=lambda _:None)
        self.client.runner=original
        self.assertTrue(self.client.reconcile_pending())
        self.assertFalse(self.client.reconcile_pending())
    def test_wrong_execution_configuration_never_receives_secret(self):
        original=self.fake_run
        def unsafe(argv,payload,**kwargs):
            result=original(argv,payload,**kwargs)
            if argv[1]=='inspect':
                rows=json.loads(result);rows[0]['HostConfig']['NetworkMode']='host';return json.dumps(rows).encode()
            return result
        self.client.runner=unsafe
        with self.assertRaises(helper.HelperError):self.client.inspect(self.db,{'password':'secret'},revalidate=lambda _:None)
        self.assertFalse(any(payload for argv,payload in self.calls))
        self.assertFalse(any(argv[1]=='start' for argv,payload in self.calls))
    def test_json_result_does_not_override_failed_helper_exit(self):
        original=self.fake_run
        def failed(argv,payload,**kwargs):
            result=original(argv,payload,**kwargs)
            if argv[1]=='inspect':
                rows=json.loads(result);rows[0]['State']['ExitCode']=1;return json.dumps(rows).encode()
            return result
        self.client.runner=failed
        with self.assertRaises(helper.HelperError):self.client.inspect(self.db,{},revalidate=lambda _:None)
