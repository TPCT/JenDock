import sys
import unittest
from migration_runner import dialogue, RunnerError

class MigrationDialogueTests(unittest.TestCase):
    def test_real_duplex_guards_and_terminal_result(self):
        code="import sys,json;json.loads(sys.stdin.readline());print(json.dumps({'event':'authorize','sequence':1}),flush=True);assert json.loads(sys.stdin.readline())=={'sequence':1,'authorized':True};print(json.dumps({'result':{'status':'committed'}}),flush=True)"
        calls=[]
        result=dialogue([sys.executable,'-c',code],b'{}\n',authorize=lambda:calls.append(1),timeout=3)
        self.assertEqual(result,{'status':'committed'});self.assertEqual(calls,[1])
    def test_no_ack_when_revoked_and_no_terminal_without_guard(self):
        code="import sys,json;sys.stdin.readline();print(json.dumps({'event':'authorize','sequence':1}),flush=True);sys.stdin.readline()"
        def deny():raise ValueError('private detail')
        with self.assertRaises(RunnerError) as caught:dialogue([sys.executable,'-c',code],b'{}\n',authorize=deny,timeout=3)
        self.assertNotIn('private detail',str(caught.exception))
        code="print('{\"result\":{\"status\":\"committed\"}}',flush=True)"
        with self.assertRaises(RunnerError):dialogue([sys.executable,'-c',code],b'{}\n',authorize=lambda:None,timeout=3)
    def test_closed_stream_timeout_still_sanitized(self):
        with self.assertRaisesRegex(RunnerError,'timeout'):
            dialogue([sys.executable,'-c','import os,time,sys;sys.stdin.readline();os.close(0);os.close(1);os.close(2);time.sleep(10)'],b'{}\n',authorize=lambda:None,timeout=.1)

    def test_wrong_sequence_and_truncated_reply_fail(self):
        for output in ['{"event":"authorize","sequence":2}\n','{"result":{} }']:
            with self.subTest(output=output),self.assertRaises(RunnerError):
                dialogue([sys.executable,'-c','import sys;sys.stdin.readline();sys.stdout.write('+repr(output)+');sys.stdout.flush()'],b'{}\n',authorize=lambda:None,timeout=3)

class MigrationContainerTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from migration_runner import MigrationHelper
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.calls=[];self.image='sha256:'+'a'*64;self.target='b'*64;self.identifier='c'*64
        self.helper=MigrationHelper(docker='/usr/bin/docker',socket='unix:///fixture.sock',image=self.image,state_dir=self.tmp.name,runner=self.run_command,session_runner=self.session)
    def run_command(self,argv,payload,**kwargs):
        import json
        self.calls.append((argv,payload))
        if argv[1]=='create':
            self.name=argv[argv.index('--name')+1];self.nonce=argv[argv.index('--label')+1].split('=',1)[1]
        if argv[1]=='inspect':return json.dumps([{'Id':self.identifier,'Name':'/'+self.name,'Image':self.image,'State':{'Status':'exited','Running':False,'ExitCode':0},'Mounts':[],'HostConfig':{'NetworkMode':'container:'+self.target,'ReadonlyRootfs':True,'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges:true'],'Memory':128*1024*1024,'NanoCpus':250000000,'PidsLimit':32,'LogConfig':{'Type':'none'}},'Config':{'User':'65534:65534','Entrypoint':['python'],'Cmd':['/app/migration_helper_entry.py'],'Labels':{'net.shiftcodes.database-migration':self.nonce}}}]).encode()
        return b''
    def session(self,argv,payload,**kwargs):
        self.calls.append((argv,payload));kwargs['authorize']();return {'status':'committed'}
    def test_secret_channel_and_exact_owned_cleanup(self):
        import json
        checks=[]
        self.helper.invoke(self.target,{'password':'private-value'},revalidate=checks.append,authorize=lambda:None)
        self.assertEqual(checks,[self.target]*3)
        self.assertNotIn('private-value',json.dumps([a for a,p in self.calls]))
        self.assertEqual(self.calls[-2][0],['/usr/bin/docker','rm','--force',self.identifier])
    def test_lost_reply_cleans_exact_helper_and_never_restarts_it(self):
        def lost(*a,**k):raise RunnerError('timeout')
        self.helper.session_runner=lost
        with self.assertRaises(RunnerError):self.helper.invoke(self.target,{},revalidate=lambda _:None,authorize=lambda:None)
        self.assertEqual(self.calls[-2][0],['/usr/bin/docker','rm','--force',self.identifier])
        self.assertEqual(sum(a[1]=='create' for a,p in self.calls),1)
