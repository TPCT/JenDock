import unittest
from unittest.mock import Mock
import test_ingress_host_tls


class RenewalTests(unittest.TestCase):
    def setUp(self):
        self.f=test_ingress_host_tls.TLSHostTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.activate();self.f.executor.calls.clear()
        self.f.tls.authorize_certificate=Mock()
        self.f.cert.paths={'challenge_root':self.f.challenge}
        self.f.executor.verify_challenge=Mock(return_value=True)
        self.request=dict(self.f.request,renewal_id='e'*32,expected_generation='c'*64)
        self.metadata={k:v for k,v in self.f.metadata.items() if k!='hostname'}
        self.metadata['sans']=['dev.example.test']
        self.next=dict(self.metadata,generation='f'*64,fingerprint='a'*64)
        self.f.cert.request.return_value=self.next
        self.f.cert.renewal_status.return_value=dict(self.metadata,attempt=1,process_ended=True,command_succeeded=True)
        self.f.cert.renewal_status.side_effect=lambda *a,**k:dict(self.f.cert.renewal_status.return_value,owned_generation=self.f.cert.renewal_status.return_value.get('owned_generation','c'*64))
        self.oldbytes=(self.f.root/'managed'/('a'*32+'-tls.conf')).read_bytes()
    def test_renewal_replay_does_not_issue_again_or_replace_active_https(self):
        result=self.f.tls.renew_certificate(self.request)
        self.assertEqual(result['generation'],'f'*64)
        self.f.cert.renewal_status.return_value=self.next
        self.assertEqual(self.f.tls.renew_certificate(self.request)['generation'],'f'*64)
        self.assertEqual(self.f.cert.request.call_count,1)
        self.assertTrue(self.f.cert.request.call_args.kwargs['renew'])
        self.assertEqual((self.f.root/'managed'/('a'*32+'-tls.conf')).read_bytes(),self.oldbytes)
        self.assertEqual(self.f.executor.calls,[])
    def test_lost_reply_unknown_old_certificate_never_reissues(self):
        self.f.cert.request.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):self.f.tls.renew_certificate(self.request)
        self.f.cert.renewal_status.return_value=dict(self.metadata,attempt=2,process_ended=False,command_succeeded=False)
        result=self.f.tls.renew_certificate(self.request)
        self.assertEqual(result['status'],'reconciliation-required')
        self.assertEqual(self.f.cert.request.call_count,1)
        self.f.cert.renewal_status.return_value=self.next
        result=self.f.tls.reconcile_certificate(dict(self.f.request,renewal_id='e'*32))
        self.assertEqual(result['generation'],'f'*64)
        self.assertEqual(self.f.cert.request.call_count,1)
    def test_authority_stale_generation_and_extra_fields_block_effect(self):
        for change in ({'expected_generation':'a'*64},{'renewal_id':'bad'},{'path':'/etc'}):
            with self.assertRaises(ValueError):self.f.tls.renew_certificate(dict(self.request,**change))
        self.f.tls.authorize_certificate.side_effect=ValueError('revoked')
        with self.assertRaises(ValueError):self.f.tls.renew_certificate(self.request)
        self.assertEqual(self.f.cert.request.call_count,0)
    def test_new_generation_stages_same_owner_with_cas_and_failure_preserves_old(self):
        old=self.f.tls.inspect_tls(self.f.request)
        self.f.tls.renew_certificate(self.request)
        files=self.f.cert.generation_files.return_value
        self.f.cert.generation_files.return_value=dict(files,**self.next)
        stage=self.f.tls.stage_tls(dict(self.f.request,expected_digest=old['digest'],generation='f'*64,fingerprint='a'*64))
        self.f.executor.fail='test'
        with self.assertRaises(RuntimeError):self.f.tls.activate_tls(dict(self.f.request,digest=stage['digest']))
        self.assertEqual((self.f.root/'managed'/('a'*32+'-tls.conf')).read_bytes(),self.oldbytes)
        self.assertNotIn('reload',self.f.executor.calls)

    def test_known_ended_retry_requires_new_attempt_identity(self):
        self.f.cert.request.return_value={'status':'reconciliation-required'}
        self.f.cert.renewal_status.return_value={'status':'reconciliation-required','retryable':False,'retry_after':123,'attempt':1}
        self.assertFalse(self.f.tls.renew_certificate(self.request)['retryable'])
        with self.assertRaises(ValueError):self.f.tls.renew_certificate(dict(self.request,renewal_id='f'*32))
        self.f.cert.renewal_status.return_value={'status':'reconciliation-required','retryable':True,'retry_after':123,'attempt':1}
        self.f.tls.renew_certificate(self.request)
        self.assertEqual(self.f.cert.request.call_count,1)
        self.f.cert.request.return_value=self.next
        result=self.f.tls.renew_certificate(dict(self.request,renewal_id='f'*32))
        self.assertEqual(result['generation'],'f'*64)
        self.assertTrue(self.f.cert.request.call_args.kwargs['retry'])
    def test_replay_allows_new_observation_timestamp_only(self):
        self.f.tls.renew_certificate(self.request)
        self.f.cert.renewal_status.return_value=dict(self.next,observed_at=123)
        self.assertEqual(self.f.tls.renew_certificate(self.request)['generation'],'f'*64)
        self.assertEqual(self.f.cert.request.call_count,1)

    def test_lost_reply_successful_unchanged_renewal_is_attempt_bound(self):
        self.f.cert.request.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):self.f.tls.renew_certificate(self.request)
        self.f.cert.renewal_status.return_value=dict(self.metadata,attempt=2,process_ended=False,command_succeeded=False)
        self.assertEqual(self.f.tls.renew_certificate(self.request)['status'],'reconciliation-required')
        self.f.cert.renewal_status.return_value=dict(self.metadata,attempt=2,process_ended=True,command_succeeded=True)
        result=self.f.tls.reconcile_certificate(dict(self.f.request,renewal_id='e'*32))
        self.assertEqual(result['status'],'verified')
        self.assertEqual(result['generation'],'c'*64)
        self.assertEqual(self.f.cert.request.call_count,1)

    def test_real_unix_rpc_renew_and_reconcile_closed_facade(self):
        import os
        import threading
        from types import SimpleNamespace
        from ingress_host_service import HostService
        from ingress_host_transport import HostServer,HostClient,TransportError
        issuer=SimpleNamespace(adapter=self.f.http,authorize_route=Mock(),authorize_certificate=Mock(),issue=Mock())
        service=HostService(self.f.http,issuer,tls=self.f.tls)
        path=self.f.root/'renewal.sock'
        server=HostServer(path,service,allowed_uids={os.getuid()},owner_uid=os.getuid(),timeout=1).open()
        self.addCleanup(server.close)
        def call(method,request):
            result=[]
            def client():
                try:result.append(HostClient(path,expected_server_uid=os.getuid(),timeout=2).call(method,request))
                except Exception as error:result.append(error)
            thread=threading.Thread(target=client);thread.start()
            try:server.serve_once()
            finally:thread.join(3)
            self.assertFalse(thread.is_alive())
            if isinstance(result[0],Exception):raise result[0]
            return result[0]
        result=call('renew_certificate',self.request)
        self.assertEqual(result['generation'],'f'*64)
        self.f.cert.renewal_status.return_value=self.next
        self.assertEqual(call('reconcile_certificate',dict(self.f.request,renewal_id='e'*32))['generation'],'f'*64)
        self.assertEqual(self.f.cert.request.call_count,1)
        with self.assertRaises(TransportError):call('renew_certificate',dict(self.request,path='/etc'))

    def test_manager_shaped_result_has_sans_without_hostname(self):
        self.assertNotIn('hostname',self.next)
        result=self.f.tls.renew_certificate(self.request)
        self.assertEqual(result['sans'],['dev.example.test'])
        self.assertNotIn('hostname',result)

    def test_wrong_or_additional_san_is_rejected(self):
        for sans in ([],['other.example.test'],['dev.example.test','other.example.test']):
            result=dict(self.next,sans=sans)
            state={'hostname':'dev.example.test','status':'issuance-intent'}
            with self.assertRaises(ValueError):
                self.f.tls._renewal_finish(self.request,self.f.root/'unused.json',state,result,Mock())

    def test_authorized_absent_reconciliation_and_same_id_replay(self):
        request=dict(self.f.request,renewal_id='e'*32)
        self.assertEqual(self.f.tls.reconcile_certificate(request),dict(request,status='absent'))
        self.assertEqual(self.f.cert.request.call_count,0)
        self.f.tls.authorize_certificate.assert_called()
        self.assertEqual(self.f.tls.renew_certificate(self.request)['status'],'verified')
        self.assertEqual(self.f.tls.reconcile_certificate(dict(request,renewal_id='f'*32))['status'],'absent')
        self.assertEqual(self.f.cert.request.call_count,1)

    def test_absence_does_not_bypass_owner_or_revocation(self):
        request=dict(self.f.request,renewal_id='e'*32)
        with self.assertRaises(ValueError):self.f.tls.reconcile_certificate(dict(request,operation_id='f'*32))
        self.f.tls.authorize_certificate.side_effect=ValueError('revoked')
        with self.assertRaises(ValueError):self.f.tls.reconcile_certificate(request)

    def test_empty_or_null_journal_never_reports_absent(self):
        request=dict(self.f.request,renewal_id='e'*32)
        policy=self.f.http._policy()
        from pathlib import Path
        path=Path(policy['state_dir'])/(request['route_id']+'-renewal.json')
        for text in ('','null','{}'):
            self.f.http._write(path,text)
            with self.subTest(text=text),self.assertRaises(ValueError):
                self.f.tls.reconcile_certificate(request)

    def test_later_noop_cycle_absence_and_historical_replay_never_issue(self):
        self.f.cert.request.return_value=self.metadata
        self.f.tls.renew_certificate(self.request)
        self.f.cert.renewal_status.return_value=dict(self.metadata,attempt=2,process_ended=True,command_succeeded=True)
        later=dict(self.request,renewal_id='f'*32)
        reconcile={k:later[k] for k in ('route_id','operation_id','renewal_id')}
        self.assertEqual(self.f.tls.reconcile_certificate(reconcile)['status'],'absent')
        self.assertEqual(self.f.tls.renew_certificate(later)['status'],'verified')
        self.f.cert.renewal_status.return_value=dict(self.metadata,attempt=3,process_ended=True,command_succeeded=True)
        self.assertEqual(self.f.tls.renew_certificate(self.request)['status'],'verified')
        self.assertEqual(self.f.cert.request.call_count,2)

    def test_exact_intent_before_pointer_crash_is_observation_only(self):
        original=self.f.http._save
        def save(path,state):
            if path.name==self.request['route_id']+'-renewal.json':raise OSError('lost pointer write')
            return original(path,state)
        self.f.http._save=save
        with self.assertRaises(OSError):self.f.tls.renew_certificate(self.request)
        self.assertEqual(self.f.cert.request.call_count,0)
        self.f.http._save=original
        result=self.f.tls.reconcile_certificate({k:self.request[k] for k in ('route_id','operation_id','renewal_id')})
        self.assertEqual(result['status'],'not-started')
        self.assertEqual(self.f.cert.request.call_count,0)
        with self.assertRaises(ValueError):self.f.tls.renew_certificate(dict(self.request,renewal_id='f'*32))
        with self.assertRaises(ValueError):self.f.tls.reconcile_certificate(dict(self.f.request,renewal_id='f'*32))

    def test_legacy_journal_migrates_before_later_attempt(self):
        from pathlib import Path
        self.f.cert.request.return_value=self.metadata
        self.f.tls.renew_certificate(self.request)
        self.f.cert.renewal_status.return_value=dict(self.metadata,attempt=2,process_ended=True,command_succeeded=True)
        self.f.tls.renew_certificate(self.request)
        directory=Path(self.f.http._policy()['state_dir'])
        exact=directory/(self.request['route_id']+'-renewal-'+self.request['renewal_id']+'.json')
        exact.unlink()
        result=self.f.tls.reconcile_certificate(dict(self.f.request,renewal_id='f'*32))
        self.assertEqual(result['status'],'absent');self.assertTrue(exact.exists())
        self.assertEqual(self.f.tls.renew_certificate(self.request)['status'],'verified')
        self.assertEqual(self.f.cert.request.call_count,1)

    def test_unknown_current_attempt_blocks_other_identity(self):
        self.f.cert.request.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):self.f.tls.renew_certificate(self.request)
        with self.assertRaises(ValueError):self.f.tls.reconcile_certificate(dict(self.f.request,renewal_id='f'*32))
        with self.assertRaises(ValueError):self.f.tls.renew_certificate(dict(self.request,renewal_id='f'*32))
        self.assertEqual(self.f.cert.request.call_count,1)

    def test_failed_ended_history_does_not_block_third_cycle(self):
        self.f.cert.request.return_value={'status':'reconciliation-required'}
        failed={'status':'reconciliation-required','retryable':True,'retry_after':123,'attempt':2}
        self.f.cert.renewal_status.return_value=dict(failed,attempt=1,retryable=False)
        self.f.tls.renew_certificate(self.request)
        self.f.cert.renewal_status.return_value=failed
        retry=dict(self.request,renewal_id='f'*32)
        self.f.cert.request.return_value=self.metadata
        self.assertEqual(self.f.tls.renew_certificate(retry)['status'],'verified')
        self.f.cert.renewal_status.return_value=dict(self.metadata,attempt=3,process_ended=True,command_succeeded=True)
        third=dict(self.request,renewal_id='1'*32)
        self.assertEqual(self.f.tls.reconcile_certificate({k:third[k] for k in ('route_id','operation_id','renewal_id')})['status'],'absent')
        self.assertEqual(self.f.tls.renew_certificate(third)['status'],'verified')
        self.assertEqual(self.f.cert.request.call_count,3)
        old=self.f.tls.reconcile_certificate({k:self.request[k] for k in ('route_id','operation_id','renewal_id')})
        self.assertEqual(old['status'],'reconciliation-required');self.assertFalse(old['retryable'])
        self.assertEqual(self.f.cert.request.call_count,3)

    def test_interrupted_retry_after_archiving_failure_keeps_safe_retry_path(self):
        self.f.cert.request.return_value={'status':'reconciliation-required'}
        self.f.cert.renewal_status.return_value={'status':'reconciliation-required','retryable':False,'retry_after':123,'attempt':1}
        self.f.tls.renew_certificate(self.request)
        self.f.cert.renewal_status.return_value={'status':'reconciliation-required','retryable':True,'retry_after':123,'attempt':2}
        retry=dict(self.request,renewal_id='f'*32)
        save=self.f.http._save
        def interrupted(path,state):
            if path.name.endswith('-'+'f'*32+'.json'):raise OSError('before new attempt')
            save(path,state)
        self.f.http._save=interrupted
        with self.assertRaises(OSError):self.f.tls.renew_certificate(retry)
        self.f.http._save=save
        result=self.f.tls.reconcile_certificate({k:retry[k] for k in ('route_id','operation_id','renewal_id')})
        self.assertEqual(result['status'],'absent')
        self.f.cert.request.return_value=self.metadata
        self.assertEqual(self.f.tls.renew_certificate(retry)['status'],'verified')
        self.assertEqual(self.f.cert.request.call_count,2)

    def test_before_manager_intent_can_resume_same_id_only(self):
        save=self.f.http._save
        def interrupt(path,state):
            save(path,state)
            if path.name==self.request['route_id']+'-renewal.json':raise OSError('before manager call')
        self.f.http._save=interrupt
        with self.assertRaises(OSError):self.f.tls.renew_certificate(self.request)
        self.f.http._save=save
        request={k:self.request[k] for k in ('route_id','operation_id','renewal_id')}
        self.assertEqual(self.f.tls.reconcile_certificate(request)['status'],'not-started')
        self.assertEqual(self.f.cert.request.call_count,0)
        self.assertEqual(self.f.tls.renew_certificate(self.request)['status'],'verified')
        self.assertEqual(self.f.cert.request.call_count,1)

    def test_manager_intent_or_changed_owned_bytes_never_resume(self):
        self.f.cert.request.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):self.f.tls.renew_certificate(self.request)
        for changes in ({'attempt':2,'process_ended':False},{'owned_generation':'d'*64}, {'process_ended':False}):
            self.f.cert.renewal_status.return_value=dict(self.metadata,attempt=1,process_ended=True,command_succeeded=True,**{})
            self.f.cert.renewal_status.return_value.update(changes)
            with self.subTest(changes=changes):
                self.assertEqual(self.f.tls.renew_certificate(self.request)['status'],'reconciliation-required')
                self.assertEqual(self.f.cert.request.call_count,1)
