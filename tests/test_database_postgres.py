import unittest
from unittest.mock import Mock
from cryptography.fernet import Fernet
from database_postgres import Inspector,DatabaseError

class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.connect=Mock(side_effect=AssertionError('invalid request reached database'))
        self.key=Fernet.generate_key()
        self.adapter=Inspector(self.connect,database='test',user='reader',schemas=['public'],cursor_key=self.key,cursor_context={'binding':1,'principal':'owner'})
    def test_no_sql_or_network_parameters_in_read_protocol(self):
        for extra in ({'sql':'DROP TABLE x'},{'host':'example.com'},{'password':'value'},{'options':'anything'}):
            with self.subTest(extra=extra),self.assertRaises(DatabaseError):self.adapter.inspect({'action':'schemas',**extra})
        self.connect.assert_not_called()
    def test_schema_and_page_limits_before_connection(self):
        for request in ({'action':'tables','schema':'other'},{'action':'schemas','limit':True},{'action':'schemas','limit':101},{'action':'rows','schema':'public','table':''}):
            with self.subTest(request=request),self.assertRaises(DatabaseError):self.adapter.inspect(request)
        self.connect.assert_not_called()
    def test_cursor_is_private_and_scope_bound(self):
        token=self.adapter._next({'key':123456789},['rows','public','x',25])
        self.assertNotIn('123456789',token)
        self.assertEqual(self.adapter._cursor(token,['rows','public','x',25]),{'key':123456789})
        other=Inspector(self.connect,database='test',user='reader',schemas=['public'],cursor_key=self.key,cursor_context={'binding':2,'principal':'owner'})
        for adapter,shape in ((other,['rows','public','x',25]),(self.adapter,['rows','public','other',25])):
            with self.assertRaisesRegex(DatabaseError,'cursor_stale'):adapter._cursor(token,shape)
    def test_system_schemas_are_not_application_policy(self):
        for name in ('pg_catalog','pg_temp_1','information_schema'):
            with self.assertRaises(DatabaseError):Inspector(self.connect,database='test',user='reader',schemas=[name],cursor_key=self.key,cursor_context={})
