import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from database_inventory import DockerInventory, InventoryError


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.data=self.root/'data';self.data.mkdir()
        self.row={'Id':'a'*64,'Image':'sha256:'+'b'*64,'State':{'Running':True,'Paused':False,'Restarting':False,'StartedAt':'stamp'},'HostConfig':{'NetworkMode':'app'},'Config':{'Env':['PASSWORD=secret'],'Labels':{'private':'secret','com.docker.compose.project':'app'}},'Mounts':[{'Type':'bind','Source':str(self.data),'Destination':'/data','RW':True},{'Type':'volume','Name':'db_data','Source':'/vol/db','Destination':'/other','RW':True}],'NetworkSettings':{'Networks':{'app':{'NetworkID':'c'*64,'EndpointID':'d'*64,'IPAddress':'10.0.0.2','Aliases':['db']}}}}
        self.volume={'Name':'db_data','Driver':'local','Mountpoint':'/vol/db','CreatedAt':'stamp','Options':{'device':'private-option'},'Labels':{'secret':'hidden'}}
    def collect(self,runner,**kwargs):return DockerInventory(docker='/usr/bin/docker',socket='unix:///var/run/docker.sock',bind_roots=[str(self.root)],runner=runner,**kwargs)()
    def runner(self,args,**kwargs):
        if args[1]=='ps':return ('a'*64+'\n').encode()
        return (json.dumps(self.volume if args[1]=='volume' else self.row)+'\n').encode()
    def test_projection_private_options_and_descriptor_proof(self):
        result=self.collect(self.runner)
        self.assertNotIn('PASSWORD',json.dumps(result));self.assertNotIn('"private"',json.dumps(result))
        self.assertNotIn('Labels',result['volumes']['db_data'])
        self.assertEqual(result['bind_sources'][str(self.data)]['inode'],self.data.stat().st_ino)
        self.assertEqual(result['volumes']['db_data']['Options'],self.volume['Options'])
    def test_outside_and_symlink_bind_proofs_omitted(self):
        alias=self.root/'alias';alias.symlink_to(self.data,target_is_directory=True)
        for path in (str(alias),'/etc',str(self.data)+'/../data'):
            self.row['Mounts'][0]['Source']=path
            self.assertEqual(self.collect(self.runner)['bind_sources'],{})
    def test_bounds_identity_and_sanitized_failure(self):
        for runner in (lambda *a,**k:b'x'*2048,lambda *a,**k:('\n'.join(['a'*64]*1001)).encode(),lambda *a,**k:b'not-json\n'):
            with self.assertRaises(InventoryError) as error:self.collect(runner,max_bytes=1024)
            self.assertNotIn('not-json',str(error.exception))
        def failure(*args,**kwargs):raise RuntimeError('PASSWORD=private-error')
        with self.assertRaises(InventoryError) as error:self.collect(failure)
        self.assertNotIn('private-error',str(error.exception))
    def test_real_subprocess_transport_bounded_output_and_deadline(self):
        executable=self.root/'docker'
        executable.write_text('#!'+sys.executable+'\nimport sys,json\nrow='+repr(self.row)+'\nvolume='+repr(self.volume)+'\nprint("a"*64 if sys.argv[1]=="ps" else json.dumps(volume if sys.argv[1]=="volume" else row))\n')
        executable.chmod(0o700)
        result=DockerInventory(docker=str(executable),socket='unix:///var/run/docker.sock',bind_roots=[str(self.root)])()
        self.assertEqual(len(result['containers']),1)
        for script in ('print("sensitive"*10000)','import time;time.sleep(2)'):
            executable.write_text('#!'+sys.executable+'\n'+script+'\n')
            with self.assertRaises(InventoryError):DockerInventory(docker=str(executable),socket='unix:///var/run/docker.sock',max_bytes=1024,timeout=.1)()
    def test_strict_trusted_configuration(self):
        for changes in ({'docker':'docker'},{'socket':'tcp://example:2375'},{'bind_roots':['/']},{'bind_roots':['/tmp/../etc']},{'timeout':float('nan')}):
            args={'docker':'/usr/bin/docker','socket':'unix:///var/run/docker.sock'};args.update(changes)
            with self.assertRaises(InventoryError):DockerInventory(**args)
