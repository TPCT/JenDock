import io
import json
import sys
import types
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet
import database_inspector_entry as entry
import database_mysql
import database_postgres


class EntryTests(unittest.TestCase):
    def payload(self,engine=None):
        value={'database':'fixture','user':'reader','password':'private-fixture-password','port':3306,'schemas':['fixture'],'row_columns':{},'cursor_key':Fernet.generate_key().decode(),'cursor_context':{},'request':{'action':'schemas'}}
        if engine is not None:value['engine']=engine
        return value
    def invoke(self,value,raw=None):
        stdout=types.SimpleNamespace(buffer=io.BytesIO())
        stdin=types.SimpleNamespace(buffer=io.BytesIO(raw if raw is not None else json.dumps(value).encode()))
        with patch.object(sys,'stdin',stdin),patch.object(sys,'stdout',stdout):entry.main()
        return json.loads(stdout.buffer.getvalue())
    def adapter(self,calls):
        class Adapter:
            def __init__(self,connect,**kwargs):calls.append(kwargs);connect()
            def inspect(self,request):return {'data':[]}
        return Adapter
    def test_mysql_closed_dispatch_fixed_driver_options(self):
        calls=[];connections=[]
        driver=types.SimpleNamespace(connect=lambda **kwargs:connections.append(kwargs))
        with patch.dict(sys.modules,{'pymysql':driver}),patch.object(database_mysql,'Inspector',self.adapter(calls)):
            self.assertEqual(self.invoke(self.payload('mariadb')),{'result':{'data':[]}})
        self.assertEqual(calls[0]['expected_engine'],'mariadb')
        options=connections[0]
        self.assertEqual(options['host'],'127.0.0.1');self.assertFalse(options['local_infile']);self.assertEqual(options['client_flag'],0)
        self.assertEqual(options['connect_timeout'],3);self.assertLessEqual(options['read_timeout'],5)
        self.assertNotIn('init_command',options);self.assertNotIn('unix_socket',options)
    def test_legacy_and_explicit_pg_dispatch(self):
        for engine in (None,'postgresql'):
            calls=[];connections=[]
            with patch.dict(sys.modules,{'psycopg':types.SimpleNamespace(connect=lambda **kwargs:connections.append(kwargs))}),patch.object(database_postgres,'Inspector',self.adapter(calls)):
                self.assertEqual(self.invoke(self.payload(engine)),{'result':{'data':[]}})
            self.assertEqual(connections[0]['host'],'127.0.0.1');self.assertNotIn('expected_engine',calls[0])
    def test_invalid_envelope_never_connects(self):
        values=[dict(self.payload('mysql'),host='attacker'),self.payload('sqlite'),dict(self.payload('mysql'),port=True)]
        missing=self.payload('mysql');missing.pop('request');values.append(missing)
        with patch.object(database_mysql,'Inspector',side_effect=AssertionError('must not construct')):
            for value in values:self.assertEqual(self.invoke(value),{'error':'invalid_request'})
            self.assertEqual(self.invoke(None,raw=b'{"engine":"mysql","engine":"mariadb"}'),{'error':'invalid_request'})
            self.assertEqual(self.invoke(None,raw=b'x'*32769),{'error':'invalid_request'})
    def test_typed_failure_and_driver_secrets_are_sanitized(self):
        with patch.dict(sys.modules,{'pymysql':types.SimpleNamespace()}),patch.object(database_mysql,'Inspector',side_effect=database_mysql.DatabaseError('identity_changed')):
            self.assertEqual(self.invoke(self.payload('mysql')),{'error':'identity_changed'})
        with patch.dict(sys.modules,{'pymysql':types.SimpleNamespace()}),patch.object(database_mysql,'Inspector',side_effect=RuntimeError('private-fixture-password')):
            self.assertEqual(self.invoke(self.payload('mysql')),{'error':'invalid_request'})


class EngineIdentityTests(unittest.TestCase):
    def test_mismatch_rejected_before_roles_grants_or_data(self):
        for selected, version in [('mysql','11.4.13-MariaDB'),('mariadb','8.4.11')]:
            statements=[]
            class Connection:
                def cursor(self):return self
                def execute(self,sql,args=()):statements.append(sql)
                def fetchmany(self,limit):return [('fixture','reader@%',version)]
                def close(self):pass
                def rollback(self):pass
            adapter=database_mysql.Inspector(Connection,database='fixture',user='reader',schemas=['fixture'],cursor_key=Fernet.generate_key(),cursor_context={},expected_engine=selected)
            with self.assertRaises(database_mysql.DatabaseError) as error:adapter.inspect({'action':'schemas'})
            self.assertEqual(error.exception.code,'identity_changed')
            self.assertEqual(statements,['SELECT DATABASE(),CURRENT_USER(),VERSION()'])
