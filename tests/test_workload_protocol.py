import copy
import unittest
import workload_protocol as p

SCOPE = dict(project='demo', environment='development', service_id='12345678-1234-1234-1234-123456789abc', definition_digest='a'*64)

def request(operation='queue.observe', **extra):
    return dict(protocol=1, operation=operation, scope=SCOPE.copy(), **extra)

def response(result, req=None):
    return dict(protocol=1, status='ok', observed_at='2026-10-11T12:00:00+00:00', scope=SCOPE.copy(), result=result)

class ProtocolTests(unittest.TestCase):
    def test_closed_request_rejects_unknown_and_bool_version(self):
        for value in [dict(request(), command='cat /etc/passwd'), dict(request(), protocol=True), request('unknown')]:
            with self.assertRaises(p.ProtocolError): p.validate_request(value)

    def test_request_copies_and_rejects_cross_environment(self):
        value=request('queue.failures', queue='default', limit=25)
        safe=p.validate_request(value)
        value['scope']['project']='changed'
        self.assertEqual(safe['scope']['project'],'demo')
        with self.assertRaises(p.ProtocolError): p.validate_request(dict(request(), scope={}))

    def test_queue_counts_and_untrusted_nested_fields(self):
        result=dict(connection_id='pgsql', driver='database', queues=[dict(name='default',ready=2,delayed=1,reserved=0)],workers=[])
        self.assertEqual(p.validate_response(response(result),request())['result'],result)
        for field,value in [('payload','secret'),('ready',True),('ready',-1)]:
            bad=copy.deepcopy(result);bad['queues'][0][field]=value
            with self.assertRaises(p.ProtocolError): p.validate_response(response(bad),request())

    def test_response_scope_date_and_protocol_are_checked(self):
        result=dict(connection_id='pgsql',driver='database',queues=[],workers=[])
        for field,value in [('scope',dict(SCOPE,environment='production')),('observed_at','yesterday'),('observed_at','2026-10-11T12:00:00'),('protocol',True)]:
            bad=response(result);bad[field]=value
            with self.assertRaises(p.ProtocolError): p.validate_response(bad,request())

    def test_error_never_reflects_raw_application_text(self):
        for error in [{'code':'database_password=secret'}, {'code':'dependency_unavailable','trace':'SECRET'}]:
            bad=dict(protocol=1,status='error',observed_at='2026-10-11T12:00:00Z',error=error)
            with self.assertRaises(p.ProtocolError) as caught:p.validate_response(bad,request())
            self.assertNotIn('secret',str(caught.exception).lower())

    def test_retry_receipt_matches_request_and_does_not_claim_completed(self):
        req=request('queue.retry',request_id='r'*32,queue='default',failed_job_id='12345678-1234-1234-1234-123456789abc',failure_revision='b'*64)
        result=dict(request_id='r'*32,status='requeued',queue='default',failed_job_id=req['failed_job_id'],queue_job_id='123',committed_at='2026-10-11T12:00:00Z')
        self.assertEqual(p.validate_response(response(result),req)['result']['status'],'requeued')
        for field,value in [('request_id','z'*32),('queue','other'),('status','completed')]:
            with self.assertRaises(p.ProtocolError):p.validate_response(response(dict(result,**{field:value})),req)

    def test_absent_only_on_receipt(self):
        req=request('queue.receipt',request_id='r'*32)
        result=dict(request_id='r'*32,status='absent')
        self.assertEqual(p.validate_response(response(result),req)['result'],result)
        with self.assertRaises(p.ProtocolError):p.validate_response(response(result),request())

    def test_page_bound_and_queue_filter(self):
        req=request('queue.failures',queue='default',limit=1)
        item=dict(failed_job_id='12345678-1234-1234-1234-123456789abc',queue='default',failed_at='2026-10-11T12:00:00Z',classification='job_failed',failure_revision='b'*64,retry_supported=True)
        self.assertEqual(len(p.validate_response(response(dict(items=[item],next_cursor=None)),req)['result']['items']),1)
        for items in [[item,item],[dict(item,queue='other')]]:
            with self.assertRaises(p.ProtocolError):p.validate_response(response(dict(items=items,next_cursor=None)),req)

    def test_scheduler_registry_bound_to_release(self):
        req=request('scheduler.observe')
        result=dict(registry_revision='a'*64,items=[],next_cursor=None)
        p.validate_response(response(result),req)
        with self.assertRaises(p.ProtocolError):p.validate_response(response(dict(result,registry_revision='b'*64)),req)

    def test_all_operations_reject_malformed_shapes_without_raw_errors(self):
        extras={'queue.failures':dict(queue='default'), 'queue.retry':dict(request_id='r'*32,queue='default',failed_job_id=SCOPE['service_id'],failure_revision='b'*64),'queue.receipt':dict(request_id='r'*32)}
        for operation in p.OPERATIONS:
            req=request(operation,**extras.get(operation,{}))
            for result in [None, [], 'secret', {'payload':'secret'}]:
                with self.subTest(operation=operation,result=result):
                    with self.assertRaises(p.ProtocolError):p.validate_response(response(result),req)
            with self.assertRaises(p.ProtocolError):p.validate_request(dict(req,secret='secret'))
    def test_runtime_scope_and_unhashable_schedule_outcomes(self):
        for scope in [dict(SCOPE,service_id='svc_123'),dict(SCOPE,project='bad_project'),dict(SCOPE,service_id=SCOPE['service_id'].upper())]:
            with self.assertRaises(p.ProtocolError):p.validate_request(dict(request(),scope=scope))
        for value in [[],{},True]:
            result=dict(items=[dict(event_id='1',schedule_id='a'*64,outcome=value,observed_at='2026-10-11T12:00:00Z')],next_cursor=None)
            with self.assertRaises(p.ProtocolError):p.validate_response(response(result),request('scheduler.history'))

if __name__=='__main__':unittest.main()
