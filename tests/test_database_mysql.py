import unittest
from cryptography.fernet import Fernet
from database_mysql import Inspector, DatabaseError, classify_grants, quote_identifier


class AdapterTests(unittest.TestCase):
    def test_closed_grants(self):
        grants=["GRANT USAGE ON *.* TO `reader`@`%`", "GRANT SELECT ON `app`.* TO `reader`@`%`"]
        self.assertFalse(classify_grants(grants, 'reader@%', 'app')['lock_tables'])
        for extra in ["GRANT UPDATE ON `app`.* TO `reader`@`%`", "GRANT `role`@`%` TO `reader`@`%`", "GRANT SELECT ON *.* TO `reader`@`%`", "GRANT SELECT ON `other`.* TO `reader`@`%`", "GRANT SELECT ON `app`.* TO `reader`@`%` WITH GRANT OPTION"]:
            with self.subTest(extra=extra), self.assertRaises(DatabaseError):classify_grants(grants+[extra], 'reader@%', 'app')

    def test_lock_grant_is_explicit(self):
        result=classify_grants(["GRANT SELECT, LOCK TABLES ON `app`.* TO `reader`@`%`"], 'reader@%', 'app')
        self.assertTrue(result['lock_tables'])

    def test_identifier_escaping(self):
        self.assertEqual(quote_identifier('a`b'), '`a``b`')
        for invalid in ['', 'a\0b', 'x'*65]:
            with self.assertRaises(DatabaseError):quote_identifier(invalid)

    def test_requests_rejected_before_connect(self):
        def connect():self.fail('must not connect')
        inspector=Inspector(connect,database='app',user='reader',schemas=['app'],cursor_key=Fernet.generate_key(),cursor_context={})
        for request in [{'action':'sql'},{'action':'schemas','sql':'SELECT 1'},{'action':'rows','schema':'other','table':'x'},{'action':'schemas','limit':True}]:
            with self.subTest(request=request),self.assertRaises(DatabaseError):inspector.inspect(request)

    def test_scope_bound_cursor(self):
        key=Fernet.generate_key()
        a=Inspector(lambda:None,database='app',user='reader',schemas=['app'],cursor_key=key,cursor_context={'environment':'a'})
        b=Inspector(lambda:None,database='app',user='reader',schemas=['app'],cursor_key=key,cursor_context={'environment':'b'})
        token=a._next('one',['tables','app',None,25])
        with self.assertRaises(DatabaseError):b._cursor(token,['tables','app',None,25])


class ScriptedConnection:
    def __init__(self, *, version='8.4.6', lock=True, swapped=False, role='NONE', error=None):
        self.version=version;self.lock=lock;self.swapped=swapped;self.role=role;self.error=error
        self.statements=[];self.rows=[];self.locked=False;self.closed=False;self.rolled_back=False
    def cursor(self):return self
    def execute(self, sql, args=()):
        self.statements.append((sql,args));self.rows=[]
        if self.error:raise self.error
        if sql=='SELECT DATABASE(),CURRENT_USER(),VERSION()':self.rows=[('app','reader@%',self.version)]
        elif sql=='SELECT CURRENT_ROLE()':self.rows=[(self.role,)]
        elif sql=='SHOW GRANTS':self.rows=[('GRANT SELECT'+(', LOCK TABLES' if self.lock else '')+' ON `app`.* TO `reader`@`%`',)]
        elif sql.startswith('LOCK TABLES'):self.locked=True
        elif 'FROM information_schema.TABLES' in sql:self.rows=[('VIEW',None)] if self.locked and self.swapped else [('BASE TABLE','InnoDB')]
        elif 'FROM information_schema.COLUMNS' in sql:self.rows=[('id','bigint','NO','',1,'bigint unsigned',''),('text','varchar','YES','',2,'varchar(512)','')]
        elif 'FROM information_schema.STATISTICS' in sql:self.rows=[('PRIMARY',0,1,'id','BTREE',None)]
        elif sql.startswith('SELECT `id`,'):self.rows=[(2**63,'x'*257),(2**63+1,None)]
        elif 'FROM information_schema.SCHEMATA' in sql:self.rows=[('app',)]
    def fetchmany(self,limit):return self.rows[:limit]
    def close(self):self.closed=True
    def rollback(self):self.rolled_back=True


class QueryTests(unittest.TestCase):
    def inspector(self, connection):
        return Inspector(lambda:connection,database='app',user='reader',schemas=['app'],row_columns={'app':{'items':['id','text']}},cursor_key=Fernet.generate_key(),cursor_context={'binding':'one'})
    def test_rows_lock_fresh_catalog_bound_projection_and_cleanup(self):
        conn=ScriptedConnection();obj=self.inspector(conn)
        result=obj.inspect({'action':'rows','schema':'app','table':'items','limit':1})
        self.assertEqual(result['engine'],'mysql');self.assertFalse(result['identity']['cluster_identity_verified'])
        self.assertTrue(result['data']['rows'][0][0]['truncated'])
        self.assertEqual(len(result['data']['rows'][0][0]['value']),256)
        cursor=obj._cursor(result['next_cursor'],['rows','app','items',1]);self.assertEqual(cursor['key'],2**63)
        statements=[s for s,a in conn.statements]
        lock=statements.index('LOCK TABLES `app`.`items` READ')
        read=next(i for i,s in enumerate(statements) if s.startswith('SELECT `id`,'))
        self.assertGreater(read,lock)
        self.assertTrue(any('information_schema.TABLES' in s for s in statements[lock+1:read]))
        self.assertIn('LEFT(CAST(`text` AS CHAR CHARACTER SET utf8mb4),257)',statements[read])
        self.assertEqual(statements[-1],'UNLOCK TABLES');self.assertTrue(conn.closed and conn.rolled_back)
    def test_missing_lock_grant_keeps_metadata_but_denies_rows(self):
        conn=ScriptedConnection(lock=False);obj=self.inspector(conn)
        self.assertEqual(obj.inspect({'action':'schemas'})['data'],['app'])
        with self.assertRaises(DatabaseError) as error:obj.inspect({'action':'rows','schema':'app','table':'items'})
        self.assertEqual(error.exception.code,'row_lock_privilege_required')
        self.assertFalse(any(s.startswith('LOCK TABLES') for s,a in conn.statements))
    def test_swapped_view_is_rejected_after_lock_before_data_sql(self):
        conn=ScriptedConnection(swapped=True)
        with self.assertRaises(DatabaseError) as error:self.inspector(conn).inspect({'action':'rows','schema':'app','table':'items'})
        self.assertEqual(error.exception.code,'unsupported_relation')
        self.assertFalse(any(s.startswith('SELECT `id`,') for s,a in conn.statements));self.assertEqual(conn.statements[-1][0],'UNLOCK TABLES')
    def test_mariadb_timeout_and_account_identity(self):
        conn=ScriptedConnection(version='11.4.8-MariaDB')
        result=self.inspector(conn).inspect({'action':'schemas'})
        self.assertEqual(result['engine'],'mariadb');self.assertEqual(result['identity']['account'],'reader@%')
        self.assertIn(('SET SESSION max_statement_time=3',()),conn.statements)
    def test_roles_and_unknown_versions_fail_closed(self):
        for conn in (ScriptedConnection(role='admin'),ScriptedConnection(version='5.7.1')):
            with self.assertRaises(DatabaseError):self.inspector(conn).inspect({'action':'schemas'})
            self.assertFalse(any('FROM information_schema.' in s for s,a in conn.statements))
    def test_timeout_sanitized_and_connection_closed(self):
        conn=ScriptedConnection(error=Exception(1205,'private password SQL details'))
        with self.assertRaises(DatabaseError) as error:self.inspector(conn).inspect({'action':'schemas'})
        self.assertEqual(error.exception.code,'timeout');self.assertNotIn('private',str(error.exception));self.assertTrue(conn.closed)
    def test_native_mariadb_usage_supported_but_plugin_unknown_denied(self):
        base=['GRANT SELECT ON `app`.* TO `reader`@`%`']
        self.assertFalse(classify_grants(base+["GRANT USAGE ON *.* TO `reader`@`%` IDENTIFIED BY PASSWORD '*"+'A'*40+"'"],'reader@%','app')['lock_tables'])
        with self.assertRaises(DatabaseError):classify_grants(base+["GRANT USAGE ON *.* TO `reader`@`%` IDENTIFIED VIA unknown"],'reader@%','app')


class GrantPatternTests(unittest.TestCase):
    def test_database_wildcards_are_not_exact_authority(self):
        with self.assertRaises(DatabaseError):
            classify_grants(['GRANT SELECT ON `app_db`.* TO `reader`@`%`'],'reader@%','app_db')
        self.assertEqual(classify_grants([r'GRANT SELECT ON `app\_db`.* TO `reader`@`%`'],'reader@%','app_db')['tables'], {'*'})
    def test_column_grants_and_resource_options_fail_closed(self):
        for grant in ['GRANT SELECT (`id`) ON `app`.`items` TO `reader`@`%`','GRANT SELECT ON `app`.* TO `reader`@`%` WITH MAX_QUERIES_PER_HOUR 100']:
            with self.assertRaises(DatabaseError):classify_grants([grant],'reader@%','app')
    def test_catalog_names_use_binary_identity_comparison(self):
        conn=ScriptedConnection();obj=QueryTests().inspector(conn)
        obj.inspect({'action':'rows','schema':'app','table':'items'})
        catalog=[sql for sql,args in conn.statements if 'WHERE' in sql and 'information_schema' in sql]
        self.assertTrue(catalog)
        self.assertTrue(all('BINARY TABLE_SCHEMA=BINARY %s' in sql for sql in catalog))
