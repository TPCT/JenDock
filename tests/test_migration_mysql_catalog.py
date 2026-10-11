"""Read-only catalog unit evidence; these fakes do not establish live DDL support."""
import unittest
from migration_mysql_catalog import (Catalog, CatalogError, IDENTITY, SETTINGS, DEFAULTS,
                                     TABLE, COLUMNS, INDEXES, INDEX_VISIBILITY, TRIGGERS, CONSTRAINTS, REFERENCES,
                                     PARTIAL_REVOKES, GLOBAL_TRIGGER, SCHEMA_TRIGGER, TABLE_TRIGGER, FOREIGN_KEY_CHECKS)


class Cursor:
    def __init__(self, engine='mysql'):
        self.rows = {
            IDENTITY: [('business', 'migrator@%', '8.4.3' if engine == 'mysql' else '11.4.5-MariaDB')],
            SETTINGS: [(1, 'InnoDB', 'STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION', 'utf8mb4', 'utf8mb4', 'utf8mb4_unicode_ci', 0)],
            DEFAULTS: [('utf8mb4', 'utf8mb4_unicode_ci')],
            TABLE: [('BASE TABLE', 'InnoDB', 'utf8mb4_unicode_ci', '')],
            COLUMNS: [('id', 1, 'bigint', 'bigint unsigned', 'NO', None, 'auto_increment', None, 20, 0, None, None, None, '')],
            INDEXES: [('PRIMARY', 0, 1, 'id', 'A', None, 'BTREE')],
            CONSTRAINTS: [('PRIMARY', 'PRIMARY KEY')], TRIGGERS: [], REFERENCES: [],
            PARTIAL_REVOKES: [(0,)], GLOBAL_TRIGGER: [('TRIGGER',)], SCHEMA_TRIGGER: [], TABLE_TRIGGER: [],
            FOREIGN_KEY_CHECKS: [(1,)],
        }
        self.calls = []; self.error = None
    def execute(self, sql, args):
        self.calls.append((sql,args))
        if self.error: raise self.error
        if 'IS_VISIBLE FROM' in sql or 'IGNORED FROM' in sql:
            self.current = [(row[0], 'YES' if 'IS_VISIBLE FROM' in sql else 'NO') for row in self.rows[INDEXES]]
        else: self.current = self.rows[sql]
    def fetchmany(self, limit): return self.current[:limit]


class CatalogTests(unittest.TestCase):
    def catalog(self, engine='mysql'):
        c = Cursor(engine)
        return c, Catalog(c, database='business', user='migrator', engine=engine)

    def test_identity_precise_engine_database_user_and_stable_settings(self):
        for engine in ('mysql','mariadb'):
            cursor, catalog = self.catalog(engine)
            proof = catalog.identity()
            self.assertEqual(proof['engine'], engine)
            self.assertFalse(proof['cluster_identity_verified'])
            cursor.rows[SETTINGS] = [(1,'InnoDB','STRICT_TRANS_TABLES','utf8mb4','utf8mb4','utf8mb4_unicode_ci',0)]
            with self.assertRaisesRegex(CatalogError,'identity_changed'): catalog.identity()
        for row, code in ((('other','migrator@%','8.4.3'),'identity_changed'),
                          (('business','other@%','8.4.3'),'identity_changed'),
                          (('business','migrator@%','11.4.5-MariaDB'),'identity_changed'),
                          (('business','migrator@%','5.7.44'),'unsupported_engine')):
            cursor, catalog = self.catalog(); cursor.rows[IDENTITY] = [row]
            with self.assertRaisesRegex(CatalogError,code): catalog.identity()

    def test_unsafe_session_rejected_and_sql_modes_canonicalized(self):
        for index, value in ((0,0),(1,'MyISAM'),(2,''),(6,1),(6,2)):
            cursor,catalog=self.catalog(); values=list(cursor.rows[SETTINGS][0]);values[index]=value;cursor.rows[SETTINGS]=[tuple(values)]
            with self.assertRaisesRegex(CatalogError,'unsafe_session'):catalog.identity()
        cursor,catalog=self.catalog(); first=catalog.identity()
        values=list(cursor.rows[SETTINGS][0]); values[2]='NO_ENGINE_SUBSTITUTION,STRICT_TRANS_TABLES';cursor.rows[SETTINGS]=[tuple(values)]
        self.assertEqual(first,catalog.identity())
        cursor.rows[FOREIGN_KEY_CHECKS] = [(0,)]
        with self.assertRaisesRegex(CatalogError, 'unsafe_session'): catalog.identity()

    def test_metadata_visibility_and_partial_revokes(self):
        cursor, catalog = self.catalog()
        cursor.rows[GLOBAL_TRIGGER] = []
        with self.assertRaisesRegex(CatalogError, 'metadata_visibility_unverified'):
            catalog.schema_state(('events',))
        cursor.rows[SCHEMA_TRIGGER] = [('bus_ness',)]
        with self.assertRaisesRegex(CatalogError, 'metadata_visibility_unverified'):
            catalog.schema_state(('events',))
        cursor.rows[SCHEMA_TRIGGER] = [('business',)]
        self.assertTrue(catalog.schema_state(('events',)))
        cursor.rows[PARTIAL_REVOKES] = [(1,)]
        cursor.rows[SCHEMA_TRIGGER] = [('bus_ness',)]
        cursor.rows[GLOBAL_TRIGGER] = [('TRIGGER',)]
        with self.assertRaisesRegex(CatalogError, 'metadata_visibility_unverified'):
            catalog.schema_state(('events',))
        cursor.rows[SCHEMA_TRIGGER] = [('business',)]
        self.assertTrue(catalog.schema_state(('events',)))
        cursor.rows[SCHEMA_TRIGGER] = []
        cursor.rows[TABLE_TRIGGER] = [('events',)]
        self.assertTrue(catalog.schema_state(('events',)))

    def test_query_bounded_immutable_sanitized_and_parameterized(self):
        cursor,catalog=self.catalog(); self.assertIsInstance(catalog.query(IDENTITY),tuple)
        cursor.rows[IDENTITY] *= 2
        with self.assertRaisesRegex(CatalogError,'result_limit'):catalog.query(IDENTITY,limit=2)
        cursor.error=RuntimeError('password=DO_NOT_DISCLOSE')
        with self.assertRaises(CatalogError) as caught:catalog.query(IDENTITY)
        self.assertNotIn('DO_NOT_DISCLOSE',str(caught.exception))
        for sql in ('DROP TABLE x','SELECT 1; DROP TABLE x'):
            with self.assertRaisesRegex(CatalogError,'invalid_query'):catalog.query(sql)
        cursor,catalog=self.catalog();catalog.schema_state(('events',))
        self.assertIn((TABLE,('business','events')),cursor.calls)
        self.assertIn((REFERENCES,('business','events','business','events')),cursor.calls)
        self.assertTrue(all(sql.startswith('SELECT ') for sql,args in cursor.calls))

    def test_schema_absence_and_unsafe_metadata(self):
        cursor,catalog=self.catalog();cursor.rows[TABLE]=[]
        self.assertEqual(catalog.schema_state(('missing',)),(('missing',None,(),(),()),))
        cases=((TABLE,[('VIEW',None,None,'')]),(TABLE,[('BASE TABLE','MyISAM','utf8mb4_unicode_ci','')]),
               (TRIGGERS,[('hidden_trigger',)]),(REFERENCES,[('other','source','fk','id','business','events','id')]),
               (CONSTRAINTS,[('check_positive','CHECK')]),(INDEXES,[('functional',1,1,None,'A',None,'BTREE')]))
        for sql,rows in cases:
            cursor,catalog=self.catalog();cursor.rows[sql]=rows
            with self.subTest(sql=sql),self.assertRaisesRegex(CatalogError,'unsafe_relation'):catalog.schema_state(('events',))

    def test_create_exact_identity_semantics_and_index_shape(self):
        operation=('create_table','public','events',(('id',('bigint',),False,True),))
        semantic=((0,'public','events','id','id',True,True),)
        for engine in ('mysql','mariadb'):
            cursor,catalog=self.catalog(engine)
            self.assertTrue(catalog.verify_operation(operation,semantic))
        for field,value in ((3,'bigint'),(6,''),(5,'1'),(13,'id + 1')):
            cursor,catalog=self.catalog();col=list(cursor.rows[COLUMNS][0]);col[field]=value;cursor.rows[COLUMNS]=[tuple(col)]
            with self.subTest(field=field),self.assertRaisesRegex(CatalogError,'postcondition_failed'):catalog.verify_operation(operation,semantic)
        cursor,catalog=self.catalog();cursor.rows[INDEXES]=[]
        with self.assertRaisesRegex(CatalogError,'postcondition_failed'):catalog.verify_operation(operation,semantic)

    def test_transition_rejects_replay_and_unrelated_schema_changes(self):
        cursor, catalog = self.catalog()
        create = ('create_table','public','events',(('id',('bigint',),False,True),))
        semantic = ((0,'public','events','id','id',True,True),)
        absent = (('events',None,(),(),()),)
        present = catalog.verify_transition(create, absent, semantic)
        with self.assertRaisesRegex(CatalogError, 'schema_changed'):
            catalog.verify_transition(create, present, semantic)
        operation = ('add_column','public','events',('enabled',('boolean',),True,False))
        semantic = ((0,'public','events','enabled','boolean',False,False),)
        cursor.rows[COLUMNS].append(('enabled',2,'tinyint','tinyint(1)','YES',None,'',None,3,0,None,None,None,''))
        added = catalog.verify_transition(operation, present, semantic)
        altered = list(cursor.rows[COLUMNS][0]); altered[5] = '1'; cursor.rows[COLUMNS][0] = tuple(altered)
        with self.assertRaisesRegex(CatalogError, 'schema_changed'):
            catalog.verify_transition(operation, present, semantic)
        cursor.rows[COLUMNS][0] = present[0][2][0]
        index = ('create_index','public','events','events_enabled_index',('enabled',))
        cursor.rows[INDEXES].append(('events_enabled_index',1,1,'enabled','A',None,'BTREE'))
        catalog.verify_transition(index, added)
        cursor.rows[INDEXES][0] = ('PRIMARY',0,1,'enabled','A',None,'BTREE')
        with self.assertRaisesRegex(CatalogError, 'schema_changed'):
            catalog.verify_transition(index, added)

    def test_add_bool_datetime_and_index_postconditions(self):
        cases=(('enabled',('boolean',),'boolean','tinyint','tinyint(1)',None),
               ('opened',('timestamp',),'dateTime','datetime','datetime',0),
               ('closed',('timestamp',),'timestamp','timestamp','timestamp',0))
        for name,kind,source,datatype,coltype,precision in cases:
            cursor,catalog=self.catalog()
            column=(name,2,datatype,coltype,'YES',None,'',None,3 if datatype=='tinyint' else None,0 if datatype=='tinyint' else None,precision,None,None,'')
            cursor.rows[COLUMNS].append(column)
            operation=('add_column','public','events',(name,kind,True,False))
            semantic=((0,'public','events',name,source,False,False),)
            self.assertTrue(catalog.verify_operation(operation,semantic))
            broken=list(column);broken[3]='tinyint(2)' if datatype=='tinyint' else datatype+'(6)';cursor.rows[COLUMNS][-1]=tuple(broken)
            with self.assertRaisesRegex(CatalogError,'postcondition_failed'):catalog.verify_operation(operation,semantic)
        cursor,catalog=self.catalog();cursor.rows[INDEXES].append(('events_id_index',1,1,'id','A',None,'BTREE'))
        operation=('create_index','public','events','events_id_index',('id',))
        self.assertTrue(catalog.verify_operation(operation))
        cursor.rows[INDEXES][-1]=('events_id_index',0,1,'id','A',None,'BTREE')
        with self.assertRaisesRegex(CatalogError,'postcondition_failed'):catalog.verify_operation(operation)


if __name__=='__main__':unittest.main()
