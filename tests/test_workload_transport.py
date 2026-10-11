import copy
import json
import sqlite3
import sys
import unittest
from unittest.mock import patch

import managed_services
import registry
import resource_policy
import service_provisioning
import workload_environment
from workload_transport import WorkloadTransport, TransportError, exchange, MAX_RESPONSE, COMMAND
import test_workload_role_services as fixtures
from test_workload_role_profiles import role_values


class WorkloadTransportTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.WorkloadRoleServicesTests();self.f.setUp();self.addCleanup(self.f.doCleanups);self.db=self.f.db
        values=role_values();values['framework_version']='12.69.3'
        with patch('test_workload_role_services.role_values',return_value=values):artifact=self.f.publish('queue')
        definition=self.f.definition(artifact,name='queue');definition['desired_state']='running'
        with registry.transaction(self.db):service=managed_services.accept(self.db,'app','development',definition,'role-create-1')
        self.definition=service['definition'];self.sid=service['id'];self.cid='c'*64;self.nid='d'*64;self.endpoint='e'*64
        self.digest=managed_services.digest(self.definition)
        self.binding={'container_id':self.cid,'container_name':'app-development-queue-1','compose_project':'app','compose_service':'development-queue',
                      'compose_files':['/managed/app/development/compose.json'],'state':'running','network_ids':[self.nid]}
        self.db.execute('INSERT INTO service_bindings VALUES(?,?,?)',(self.sid,registry.canonical(self.binding),0))
        self.db.execute("UPDATE managed_services SET state='running' WHERE id=?",(self.sid,))
        policy=resource_policy.read(self.db,'app');policy['document']['environments']['development']['services'][self.binding['container_name']]=definition['resources']
        self.db.execute('UPDATE resource_policies SET document=? WHERE project=?',(registry.canonical(policy['document']),'app'))
        spec=service_provisioning.service_config(self.definition,'role-create-1')
        self.actual={'Id':self.cid,'Image':artifact['image'],'Name':'/'+self.binding['container_name'],'State':{'Running':True},
            'Config':{'WorkingDir':'/app','Labels':{**spec['labels'],'com.docker.compose.project':'app','com.docker.compose.service':'development-queue','com.docker.compose.project.config_files':self.binding['compose_files'][0]},
                      'User':artifact['user'],'Entrypoint':artifact['command'][:1],'Cmd':artifact['command'][1:],'Healthcheck':{'Test':['CMD',*artifact['healthcheck']]},'StopTimeout':70,
                      'Env':['PATH=/usr/bin',*[k+'='+v for k,v in spec['environment'].items()]]},
            'HostConfig':{'Tmpfs':{'/tmp':'rw,noexec,nosuid,size=16m'},'ReadonlyRootfs':True,'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges:true'],'IpcMode':'private','Memory':32*1048576,'MemorySwap':32*1048576,'NanoCpus':100000000,'PidsLimit':128,'NetworkMode':'app-development-managed'},
            'Mounts':[],'NetworkSettings':{'Networks':{'app-development-managed':{'NetworkID':self.nid,'EndpointID':self.endpoint}}}}
        self.network={'Id':self.nid,'Name':'app-development-managed','Driver':'bridge','Internal':True,'Labels':{'net.shiftcodes.managed.project':'app','net.shiftcodes.managed.environment':'development'},'Containers':{self.cid:{'EndpointID':self.endpoint}}}
        self.image={'Id':artifact['image'],'Config':{'WorkingDir':'/app','Env':['PATH=/usr/bin']}}
        self.calls=[];self.hook=None
        self.transport=WorkloadTransport(self.db,docker='/usr/bin/docker',socket='unix:///tmp/fixture-docker.sock',runner=self.runner)
    def runner(self,argv,payload,**kwargs):
        self.calls.append((argv,payload,kwargs))
        if self.hook:
            value=self.hook(argv,payload)
            if value is not None:return value
        if argv[1]=='inspect':return json.dumps([self.actual]).encode()
        if argv[1:3]==['image','inspect']:return json.dumps([self.image]).encode()
        if argv[1:3]==['network','inspect']:return json.dumps([self.network]).encode()
        self.assertEqual(argv,['/usr/bin/docker','exec','--user','65534:65534','-i',self.cid,*COMMAND])
        request=json.loads(payload);workload=self.definition['artifact']['workload']
        if request['operation']=='capabilities':
            result={'package_version':'1.0.0','package_digest':workload['instrumentation']['digest'],'framework_version':'12.69.3','adapter_revision':1,'role':'queue','operations':['queue.observe','queue.failures','queue.retry','queue.receipt'],'retry_policy':'registered_plain_jobs_without_retry_until'}
        elif request['operation']=='queue.retry':
            result={k:request[k] for k in ('request_id','queue','failed_job_id')};result.update(status='requeued',queue_job_id='1',committed_at='2026-10-11T00:00:00+00:00')
        else:result={'connection_id':'pgsql','driver':'database','queues':[{'name':'default','ready':0,'delayed':0,'reserved':0},{'name':'mail','ready':0,'delayed':0,'reserved':0}],'workers':[]}
        return json.dumps({'protocol':1,'status':'ok','scope':request['scope'],'observed_at':'2026-10-11T00:00:00+00:00','result':result}).encode()
    def call(self,operation='queue.observe',arguments=None,**changes):
        return self.transport.call('app',changes.get('environment','development'),self.sid,changes.get('container_id',self.cid),self.digest,operation,arguments,authorize=changes.get('authorize',lambda:None))
    def retry(self):return self.call('queue.retry',{'request_id':'retry-fixture-0001','queue':'default','failed_job_id':'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa','failure_revision':'f'*64})
    def execs(self):return [call for call in self.calls if call[0][1]=='exec']
    def test_real_registry_fixed_argv_and_generated_scope_with_pre_post_guards(self):
        seen=[];response=self.call(authorize=lambda:seen.append(1))
        self.assertEqual(response['status'],'ok');self.assertGreaterEqual(len(seen),4)
        self.assertEqual(len(self.execs()),2)
        self.assertEqual(json.loads(self.execs()[1][1])['scope'],{'project':'app','environment':'development','service_id':self.sid,'definition_digest':self.digest})
        self.assertTrue(all(row[2]['limit']<=1048576 and row[2]['timeout']<=12 for row in self.calls))
        self.assertEqual(self.calls[0][2]['env'],{'PATH':'/usr/local/bin:/usr/bin:/bin','DOCKER_HOST':'unix:///tmp/fixture-docker.sock'})
    def test_scope_stopped_replacement_and_runtime_metadata_reject_before_exec(self):
        for mutate in [lambda:self.actual.update(Id='f'*64),lambda:self.actual['State'].update(Running=False),lambda:self.actual['Config'].update(User='0:0'),lambda:self.actual['Config'].update(WorkingDir='/other'),lambda:self.actual['HostConfig'].update(Tmpfs={'/tmp':'rw,noexec,nosuid,size=16m','/app':'rw'}),lambda:self.actual['HostConfig'].update(Tmpfs={'/tmp':'rw,exec,nosuid,size=16m'}),lambda:self.actual['Config'].update(Env=['DEPLOY_MONITOR_PROJECT=foreign']),lambda:self.actual['Config'].update(StopTimeout=5),lambda:self.actual['HostConfig'].update(Memory=1),lambda:self.network.update(Id='f'*64)]:
            before=copy.deepcopy((self.actual,self.network));self.calls=[];mutate()
            with self.assertRaises(TransportError):self.call()
            self.assertEqual(self.execs(),[]);self.actual,self.network=before
        for changes in [{'environment':'production'},{'container_id':'f'*64},{'authorize':None}]:
            with self.assertRaises(TransportError):self.call(**changes)
    def test_unknown_operation_and_scope_injection_never_execute(self):
        for operation,arguments in [('shell',{}),('queue.observe',{'scope':{}}),('queue.observe',{'command':['sh']})]:
            with self.assertRaises(TransportError):self.call(operation,arguments)
        self.assertEqual(self.calls,[])
    def test_replacement_after_capability_blocks_retry_dispatch(self):
        def hook(argv,payload):
            if argv[1]=='exec' and json.loads(payload)['operation']=='capabilities':self.actual['Id']='f'*64
        self.hook=hook
        with self.assertRaises(TransportError) as caught:self.retry()
        self.assertFalse(caught.exception.outcome_unknown);self.assertEqual(len(self.execs()),1)
    def test_timeout_overflow_invalid_json_and_post_effect_drift_are_unknown_without_replay(self):
        for failure in ['timeout','overflow','json','drift']:
            self.actual['Id']=self.cid;self.calls=[]
            def hook(argv,payload):
                if argv[1]=='exec' and json.loads(payload)['operation']=='queue.retry':
                    if failure=='timeout':raise TimeoutError('SECRET')
                    if failure=='overflow':return b'x'*(MAX_RESPONSE+1)
                    if failure=='json':return b'{"secret":"SECRET",'
                    self.actual['Id']='f'*64
            self.hook=hook
            with self.subTest(failure=failure),self.assertRaises(TransportError) as caught:self.retry()
            self.assertTrue(caught.exception.outcome_unknown);self.assertNotIn('SECRET',str(caught.exception))
            self.assertEqual(sum(json.loads(row[1])['operation']=='queue.retry' for row in self.execs()),1)
    def test_target_check_does_not_bootstrap_and_rejects_adapter_revision(self):
        target=self.transport.check_target('app','development',self.sid,self.cid,self.digest,adapter_revision=1,authorize=lambda:None)
        self.assertEqual(target['workload'],self.definition['artifact']['workload'])
        self.assertEqual(self.execs(),[])
        with self.assertRaises(TransportError):self.transport.check_target('app','development',self.sid,self.cid,self.digest,adapter_revision=2,authorize=lambda:None)
        with self.assertRaises(TransportError):self.call('queue.failures',{'queue':'foreign'})
        self.assertEqual(self.execs(),[])

    def test_controller_uses_real_registry_and_transport_boundary(self):
        import workload_control
        from types import SimpleNamespace
        self.binding['definition_digest']=self.digest
        self.db.execute('UPDATE service_bindings SET document=? WHERE service_id=?',(registry.canonical(self.binding),self.sid))
        class Authority:
            actor='console:owner'
            def authorize(inner,actor,request,phase):
                if actor!=inner.actor or request['project']!='app' or request['environment']!='development':raise ValueError('denied')
                return SimpleNamespace(scopes={'workloads:read','workloads:retry'})
        body=dict(action='workload-role-details',project='app',environment='development',service_id=self.sid,container_id=self.cid,definition_digest=self.digest,adapter_revision=1)
        catalog=workload_control.execute(self.db,{'action':'workload-targets','project':'app','environment':'development'},transport=self.transport,authority=Authority())
        self.assertEqual(len(catalog['items']),1)
        self.assertEqual(catalog['items'][0]['target'],{k:v for k,v in body.items() if k!='action'})
        self.assertTrue(catalog['items'][0]['artifact_verified'])
        self.assertFalse(catalog['items'][0]['runtime_observed']);self.assertEqual(self.execs(),[])
        self.assertNotIn('command',str(catalog));self.assertNotIn('password',str(catalog))
        metadata=workload_control.recorded_workload(self.db,managed_services.get(self.db,self.sid))
        self.assertEqual(metadata['configuration']['queues'],['default','mail'])
        self.assertEqual(metadata['target'],catalog['items'][0]['target'])
        self.assertFalse(metadata['runtime_observed'])
        result=workload_control.execute(self.db,body,transport=self.transport,authority=Authority())
        self.assertEqual(result['workload']['observation']['result']['connection_id'],'pgsql')
        body.update(action='queue-retry',request_id='retry-controller-0001',connection_id='pgsql',queue='default',failed_job_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',failure_revision='f'*64)
        result=workload_control.execute(self.db,body,transport=self.transport,authority=Authority())
        self.assertEqual(result['operation']['status'],'requeued')
        self.assertEqual(sum(json.loads(row[1])['operation']=='queue.retry' for row in self.execs()),1)

    def test_transaction_fence(self):
        self.db.execute('BEGIN')
        try:
            with self.assertRaises(TransportError):self.call()
            self.assertEqual(self.calls,[])
        finally:self.db.rollback()

    def test_recorded_metadata_never_retargets_stale_or_missing_binding(self):
        import workload_control
        service = managed_services.get(self.db, self.sid)
        service['binding']['definition_digest'] = self.digest
        current = workload_control.recorded_workload(self.db, service)
        self.assertEqual(current['target']['container_id'], self.cid)
        self.assertTrue(current['artifact_verified'])
        for binding in (None, {}, {**service['binding'], 'definition_digest': 'f'*64},
                        {**service['binding'], 'container_id': 'short-id'}):
            with self.subTest(binding=binding):
                result = workload_control.recorded_workload(self.db, {**service, 'binding': binding})
                self.assertIsNone(result['target'])
                self.assertFalse(result['runtime_observed'])
        current['configuration']['queues'].append('not-authorized')
        self.assertNotIn('not-authorized', service['definition']['artifact']['workload']['configuration']['queues'])
        self.assertEqual(self.execs(), [])


class BoundedExchangeTests(unittest.TestCase):
    def test_actual_process_timeout_stdout_and_stderr_limits(self):
        cases=[('import time; time.sleep(3)',.05,1024),('import sys; sys.stdout.write("x"*4096)',2,128),('import sys; sys.stderr.write("SECRET"*20000)',2,1024)]
        for code,timeout,limit in cases:
            with self.subTest(code=code),self.assertRaises(ValueError) as caught:exchange([sys.executable,'-c',code],timeout=timeout,limit=limit)
            self.assertNotIn('SECRET',str(caught.exception))
