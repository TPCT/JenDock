import copy
import json
import unittest
from unittest.mock import patch
import cloudflare_dns as dns
import test_ingress as fixtures

class DNSOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.IngressTests();self.fixture.setUp();self.db=self.fixture.db
        self.route=self.fixture.accept()['route']['id'];self.records=[];self.writes=[];self.lost=False;self.fail=False
        self.token=patch.object(dns.integrations,'credential_for_use',return_value='fixture-secret-token');self.token.start()
        self.actions=[]
    def tearDown(self):self.token.stop();self.fixture.tearDown()
    def authorize(self,action):self.actions.append(action)
    def transport(self,method,path,token,body=None):
        self.assertEqual(token,'fixture-secret-token')
        if method=='GET' and '/dns_records' not in path:return {'success':True,'result':{'id':'b'*32,'name':'example.test','status':'active','account':{'id':'a'*32}}}
        if method=='GET':return {'success':True,'result':copy.deepcopy(self.records),'result_info':{'page':1,'total_pages':1}}
        self.writes.append((method,path,copy.deepcopy(body)))
        if self.fail:raise TimeoutError('fixture-secret-token')
        if method=='POST':self.records.append(dict(body,id='c'*32))
        else:
            self.assertEqual(path.rsplit('/',1)[1],self.records[0]['id']);self.records[0].update(body)
        if self.lost:raise TimeoutError('fixture-secret-token')
        return {'success':True,'result':copy.deepcopy(self.records[0])}
    def preview(self):return dns.preview(self.db,self.route,'unused',transport=self.transport,authorize=self.authorize)
    def apply(self,preview,key='dns-operation-123456',adopt=None):return dns.apply(self.db,self.route,key,'unused',preview_digest=preview['preview_digest'],adopt_record_id=adopt,transport=self.transport,authorize=self.authorize)
    def foreign(self,**changes):return dict(id='d'*32,name='dev.example.test',type='A',content='192.0.2.10',proxied=True,ttl=1,comment='',**changes)
    def test_create_lost_reply_reconciles_without_second_write(self):
        preview=self.preview();self.lost=True
        result=self.apply(preview)
        self.assertEqual(result['dns'],'verified');self.assertFalse(result['ready']);self.assertEqual(result['tls'],'unverified')
        self.assertEqual(self.apply(preview),result);self.assertEqual(len(self.writes),1)
        intent=self.db.execute('SELECT * FROM ingress_dns_intents').fetchone()
        self.assertEqual(self.records[0]['comment'],intent['marker']);self.assertEqual(intent['state'],'verified')
    def test_uncertain_absent_result_never_retries_or_adopts_foreign_match(self):
        preview=self.preview();self.fail=True
        with self.assertRaises(dns.DNSError):self.apply(preview)
        self.fail=False;self.records=[self.foreign()]
        with self.assertRaises(dns.DNSError):self.apply(preview)
        self.assertEqual(len(self.writes),1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM ingress_dns_ownership').fetchone()[0],0)
    def test_explicit_adoption_and_owned_updates_use_exact_id(self):
        self.records=[self.foreign()];preview=self.preview()
        with self.assertRaises(dns.DNSError):self.apply(preview)
        self.assertEqual(self.writes,[])
        self.apply(preview,adopt='d'*32);self.assertIn('adopt',self.actions)
        self.assertEqual(self.writes[0][0],'PATCH');self.assertTrue(self.writes[0][1].endswith('/'+'d'*32))
        self.apply(self.preview(),key='dns-update-12345678')
        self.assertEqual(len(self.writes),2)
    def test_foreign_duplicate_records_and_changed_preview_fail(self):
        preview=self.preview();self.records=[self.foreign()]
        with self.assertRaises(dns.DNSError):self.apply(preview)
        self.records.append(dict(self.records[0],id='e'*32,type='TXT',content='private unrelated data'))
        preview=self.preview();self.assertNotIn('private unrelated data',json.dumps(preview))
        with self.assertRaises(dns.DNSError):self.apply(preview,adopt='d'*32)
        self.assertEqual(self.writes,[])
    def test_integration_replacement_permission_and_drift_fail_closed(self):
        preview=self.preview();self.db.execute("UPDATE integration_connections SET revision=2 WHERE provider='cloudflare'")
        with self.assertRaises(dns.DNSError):self.apply(preview)
        self.db.execute("UPDATE integration_connections SET revision=1 WHERE provider='cloudflare'")
        self.apply(preview);self.records[0]['comment']='foreign owner'
        with self.assertRaises(dns.DNSError):self.apply(self.preview(),key='dns-update-12345678')
        self.assertEqual(len(self.writes),1)
    def test_adoption_permission_denial_and_failure_errors_do_not_expose_secret(self):
        self.records=[self.foreign()];preview=self.preview()
        def denied(action):
            if action=='adopt':raise ValueError('Adoption forbidden')
        with self.assertRaises(ValueError):dns.apply(self.db,self.route,'dns-operation-123456','unused',preview_digest=preview['preview_digest'],adopt_record_id='d'*32,transport=self.transport,authorize=denied)
        self.assertFalse(self.writes)
        with self.assertRaises(dns.DNSError) as error:dns._call(lambda *args:(_ for _ in ()).throw(ValueError('fixture-secret-token')),'GET','/unused','fixture-secret-token')
        self.assertNotIn('fixture-secret-token',str(error.exception))
    def test_pagination_bound_and_fixed_transport_url(self):
        def oversized(method,path,token,body=None):
            result=self.transport(method,path,token,body)
            if '/dns_records' in path:result['result_info']['total_pages']=11
            return result
        with self.assertRaises(dns.DNSError):dns.preview(self.db,self.route,'unused',transport=oversized,authorize=self.authorize)
        with self.assertRaises(dns.DNSError):dns.request('GET','https://evil.test/','fixture-secret-token')

    def test_restart_reconciles_lost_write_before_ownership_commit(self):
        from store import connect
        preview=self.preview();original=dns._verified
        with patch.object(dns,'_verified',side_effect=RuntimeError('simulated process interruption')):
            with self.assertRaises(RuntimeError):self.apply(preview)
        self.assertEqual(len(self.writes),1)
        other=connect(self.fixture.fixture.path)
        try:
            result=dns.apply(other,self.route,'dns-operation-123456','unused',preview_digest=preview['preview_digest'],transport=self.transport,authorize=self.authorize)
            self.assertEqual(result['dns'],'verified');self.assertEqual(len(self.writes),1)
        finally:other.close()

    def test_disabled_integration_during_final_read_cannot_publish_verified(self):
        preview=self.preview()
        def racing(method,path,token,body=None):
            response=self.transport(method,path,token,body)
            if method=='GET' and '/dns_records' in path and self.writes:
                self.db.execute("UPDATE integration_connections SET enabled=0 WHERE provider='cloudflare'")
            return response
        with self.assertRaises(dns.DNSError):dns.apply(self.db,self.route,'dns-operation-123456','unused',preview_digest=preview['preview_digest'],transport=racing,authorize=self.authorize)
        self.assertEqual(len(self.writes),1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM ingress_dns_ownership').fetchone()[0],0)

    def test_http_transport_fixed_origin_redirect_rejection_and_size_limit(self):
        from unittest.mock import MagicMock
        response=MagicMock();response.status=200;response.read.return_value=b'{"success":true,"result":[]}'
        response.__enter__.return_value=response
        opener=MagicMock();opener.open.return_value=response
        with patch.object(dns.urllib.request,'build_opener',return_value=opener) as build:
            dns.request('GET','/zones/'+'b'*32,'fixture-secret-token')
            self.assertIsInstance(build.call_args.args[0],dns.integrations._NoRedirect)
            self.assertEqual(opener.open.call_args.args[0].full_url,'https://api.cloudflare.com/client/v4/zones/'+'b'*32)
            self.assertEqual(opener.open.call_args.kwargs['timeout'],10)
            response.read.assert_called_with(1048577)
            response.read.return_value=b'x'*1048577
            with self.assertRaises(dns.DNSError):dns.request('GET','/zones/'+'b'*32,'fixture-secret-token')
