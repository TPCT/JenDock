"""Independent DNS ownership, fencing and transport regressions; no live provider."""
import copy
import json
import unittest
from unittest.mock import patch
import urllib.error

import cloudflare_dns as dns
import test_cloudflare_dns as fixture


class CloudflareDNSReviewTests(unittest.TestCase):
    setUp=fixture.DNSOwnershipTests.setUp
    tearDown=fixture.DNSOwnershipTests.tearDown
    authorize=fixture.DNSOwnershipTests.authorize
    transport=fixture.DNSOwnershipTests.transport
    preview=fixture.DNSOwnershipTests.preview
    apply=fixture.DNSOwnershipTests.apply
    foreign=fixture.DNSOwnershipTests.foreign

    def test_concurrent_foreign_identical_record_after_lost_write_is_not_owned(self):
        preview=self.preview();original=self.transport
        def foreign_write(method,path,token,body=None):
            if method=='POST':
                self.writes.append((method,path,body))
                self.records=[self.foreign()]
                raise TimeoutError('fixture-secret-token')
            return original(method,path,token,body)
        with patch.object(self,'transport',side_effect=foreign_write):
            for _ in range(2):
                with self.assertRaises(dns.DNSError) as error:self.apply(preview)
                self.assertNotIn('fixture-secret-token',str(error.exception))
        self.assertEqual(len(self.writes),1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM ingress_dns_ownership').fetchone()[0],0)

    def test_foreign_record_alongside_our_marker_prevents_verified_ownership(self):
        preview=self.preview();original=self.transport
        def duplicate(method,path,token,body=None):
            response=original(method,path,token,body)
            if method=='POST':self.records.append(self.foreign())
            return response
        with patch.object(self,'transport',side_effect=duplicate):
            with self.assertRaises(dns.DNSError):self.apply(preview)
        self.assertEqual(len(self.writes),1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM ingress_dns_ownership').fetchone()[0],0)

    def test_integration_disable_during_final_read_prevents_ownership_commit(self):
        preview=self.preview();original=self.transport
        def disable(method,path,token,body=None):
            response=original(method,path,token,body)
            if self.writes and method=='GET' and '/dns_records' in path:
                self.db.execute("UPDATE integration_connections SET enabled=0 WHERE provider='cloudflare'")
            return response
        with patch.object(self,'transport',side_effect=disable):
            with self.assertRaises(dns.DNSError):self.apply(preview)
        self.assertEqual(len(self.writes),1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM ingress_dns_ownership').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT state FROM ingress_dns_intents').fetchone()[0],'write-intent')

    def test_adoption_preview_pins_private_comment_but_never_exposes_it(self):
        self.records=[dict(self.foreign(),comment='private-provider-note')]
        preview=self.preview();self.assertNotIn('private-provider-note',json.dumps(preview))
        self.records[0]['comment']='different-provider-note'
        with self.assertRaises(dns.DNSError):self.apply(preview,adopt='d'*32)
        self.assertEqual(self.writes,[])

    def test_duplicate_identity_across_pages_fails_closed(self):
        self.records=[self.foreign()];original=self.transport;pages=[]
        def paged(method,path,token,body=None):
            response=original(method,path,token,body)
            if '/dns_records' in path:
                page=2 if 'page=2&' in path else 1;pages.append(page)
                response['result_info']={'page':page,'total_pages':2}
            return response
        with patch.object(self,'transport',side_effect=paged):
            with self.assertRaises(dns.DNSError):self.preview()
        self.assertEqual(pages,[1,2]);self.assertEqual(self.writes,[])

    def test_transport_denies_redirect_and_redacts_provider_exception(self):
        secret='fixture-secret-token'
        with patch.object(dns.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=urllib.error.HTTPError('https://evil.example/'+secret,302,secret,{},None)
            with self.assertRaises(dns.DNSError) as error:dns.request('GET','/zones/'+'b'*32,secret)
            self.assertNotIn(secret,str(error.exception))
            self.assertIsInstance(opener.call_args.args[0],dns.integrations._NoRedirect)
            request=opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url,'https://api.cloudflare.com/client/v4/zones/'+'b'*32)
            self.assertEqual(opener.return_value.open.call_count,1)
