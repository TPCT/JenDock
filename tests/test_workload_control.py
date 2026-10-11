import copy
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import workload_control as c

TARGET=dict(project='demo',environment='development',service_id='12345678-1234-1234-1234-123456789abc',container_id='a'*64,definition_digest='b'*64,adapter_revision=1)
RETRY=dict(TARGET,request_id='r'*32,connection_id='pgsql',queue='default',failed_job_id='87654321-1234-1234-1234-123456789abc',failure_revision='c'*64)

class Authority:
    actor='console:owner'
    def __init__(self):self.scopes={'workloads:read','workloads:retry'}
    def authorize(self,actor,request,phase):
        if actor!=self.actor or request['project']!='demo' or request['environment']!='development' or 'workloads:read' not in self.scopes:raise ValueError('denied')
        if phase not in ('read','reconcile','reconcile_commit') and 'workloads:retry' not in self.scopes:raise ValueError('denied')
        return SimpleNamespace(scopes=self.scopes)

class Transport:
    def __init__(self):self.calls=[];self.lost=False;self.checked=0;self.db=None
    def check_target(self,*args,authorize,**kw):
        assert not self.db.in_transaction
        authorize();self.checked+=1
    def call(self,project,env,service,container,digest,operation,arguments,*,authorize,adapter_revision):
        authorize();self.calls.append(operation)
        result=dict(connection_id='pgsql',driver='database',queues=[dict(name='default',ready=1,delayed=0,reserved=0)],workers=[])
        if operation in ('queue.retry','queue.receipt'):
            result=dict(request_id=arguments['request_id'],status='requeued',queue='default',failed_job_id=RETRY['failed_job_id'],queue_job_id='42',committed_at='2026-10-11T12:00:00Z')
        if self.lost and operation=='queue.retry':raise TimeoutError('secret')
        return dict(protocol=1,status='ok',scope=dict(project=project,environment=env,service_id=service,definition_digest=digest),observed_at='2026-10-11T12:00:00Z',result=result)

class ControlTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:',isolation_level=None);self.db.row_factory=sqlite3.Row
        self.authority=Authority();self.transport=Transport();self.transport.db=self.db
        self.patch=patch.object(c,'_target',return_value={'role':'queue','configuration':{'connection':'pgsql','queues':['default']}});self.patch.start();self.addCleanup(self.patch.stop);self.addCleanup(self.db.close)
    def execute(self,action,request):return c.execute(self.db,dict(request,action=action),transport=self.transport,authority=self.authority)
    def test_read_closed_fields_and_identity_envelope(self):
        result=self.execute('workload-role-details',TARGET)['workload']
        self.assertEqual(result['container_id'],TARGET['container_id']);self.assertTrue(result['capabilities']['retry'])
        self.authority.scopes.remove('workloads:retry')
        self.assertFalse(self.execute('workload-role-details',TARGET)['workload']['capabilities']['retry'])
        with self.assertRaises(c.OperationError):self.execute('workload-role-details',dict(TARGET,command='shell'))
    def test_lost_retry_readonly_recovery_uses_original_receipt(self):
        self.transport.lost=True
        result=self.execute('queue-retry',RETRY)['operation'];self.assertEqual(result['status'],'recovery-required');self.assertEqual(self.transport.checked,1)
        self.authority.scopes.remove('workloads:retry')
        lookup={k:RETRY[k] for k in ('project','environment','service_id','request_id')}
        result=self.execute('workload-operation',lookup)['operation']
        self.assertEqual(result['status'],'requeued');self.assertEqual(self.transport.calls,['queue.retry','queue.receipt'])
        self.assertEqual(result['target']['container_id'],TARGET['container_id'])
    def test_retry_denied_without_grant(self):
        self.authority.scopes.remove('workloads:retry')
        with self.assertRaises(ValueError):self.execute('queue-retry',RETRY)
        self.assertEqual(self.transport.calls,[])
    def test_original_scope_cannot_be_retargeted_for_lookup(self):
        self.execute('queue-retry',RETRY)
        lookup={k:RETRY[k] for k in ('project','environment','service_id','request_id')};lookup['service_id']='another'
        with self.assertRaises(c.OperationError):self.execute('workload-operation',lookup)
        self.assertEqual(self.transport.calls,['queue.retry'])
    def test_untrusted_response_never_reaches_console(self):
        original=self.transport.call
        def bad(*a,**kw):
            value=original(*a,**kw);value['result']['payload']='SECRET';return value
        self.transport.call=bad
        with self.assertRaises(c.workload_protocol.ProtocolError):self.execute('workload-role-details',TARGET)

if __name__=='__main__':unittest.main()
