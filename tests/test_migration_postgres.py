import unittest
from migration_plan import alembic_plan
from migration_postgres import MigrationError, compile_operation, validate_plan

SOURCE="""from alembic import op
import sqlalchemy as sa
revision = 'one'
down_revision = None
def upgrade():
    op.create_table('items',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('label',sa.String(80),nullable=True))
"""

class CompilerTests(unittest.TestCase):
    def test_eligible_integer_primary_key_autoincrements(self):
        plan=alembic_plan({'versions/one.py':SOURCE})
        sql=compile_operation(plan.migrations[0][6][0])
        self.assertIn('"id" SERIAL NOT NULL',sql)
        self.assertIn('PRIMARY KEY ("id")',sql)
        self.assertIn('"label" varchar(80)',sql)
    def test_composite_keys_do_not_acquire_implicit_sequences(self):
        op=('create_table','public','items',(('a',('integer',),False,True),('b',('bigint',),False,True)))
        sql=compile_operation(op)
        self.assertNotIn('SERIAL',sql);self.assertIn('PRIMARY KEY ("a","b")',sql)
    def test_hostile_typed_operations_are_rejected(self):
        operations=[('create_table','other','items',(('a',('integer',),True,False),)),('add_column','public','items',('x',('text',),False,False)),('create_index','public','items','idx',('a;DROP TABLE x',)),('drop_table','public','items'),('create_table','public','alembic_version',(('a',('integer',),True,False),))]
        for op in operations:
            with self.subTest(op=op),self.assertRaises(MigrationError):compile_operation(op)
    def test_source_hash_and_plan_digest_rechecked(self):
        sources={'versions/one.py':SOURCE};plan=alembic_plan(sources)
        self.assertEqual(validate_plan(plan,sources,(),{}),plan)
        with self.assertRaises(MigrationError):validate_plan(plan,{'versions/one.py':SOURCE+'\n'},(),{})
        with self.assertRaises(MigrationError):validate_plan(plan,sources,('unknown',),{})


class FakeConnection:
    """Transaction/control-plane script; not engine SQL acceptance."""
    def __init__(self, *, heads=(), hashes=None, busy=False, commit_error=False):
        self.heads=list(heads);self.hashes=dict(hashes or {});self.busy=busy;self.commit_error=commit_error
        self.statements=[];self.rows=[];self.closed=False;self.autocommit=False
    def cursor(self):return self
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def execute(self,sql,args=()):
        self.statements.append((sql,args));self.rows=[]
        if sql=='SELECT current_database(),current_user':self.rows=[('fixture','migrator')]
        elif 'pg_try_advisory_xact_lock' in sql:self.rows=[(not self.busy,)]
        elif sql=='SELECT version_num FROM "public"."alembic_version"':self.rows=[(h,) for h in self.heads]
        elif sql.startswith('SELECT revision,source_sha256'):self.rows=list(self.hashes.items())
        elif sql.startswith('INSERT INTO "public"."deploy_monitor_migration_hashes"'):self.hashes[args[0]]=args[1]
        elif sql.startswith('DELETE FROM "public"."alembic_version"'):self.heads=[]
        elif sql.startswith('INSERT INTO "public"."alembic_version"'):self.heads.append(args[0])
        elif sql=='COMMIT' and self.commit_error:raise RuntimeError('sensitive driver detail')
    def fetchmany(self,limit):return self.rows[:limit]
    def close(self):self.closed=True


class TransactionTests(unittest.TestCase):
    def fixture(self,**kwargs):
        from migration_postgres import Executor
        from unittest.mock import Mock
        source=SOURCE.replace("    op.create_table('items',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('label',sa.String(80),nullable=True))",'    pass')
        sources={'versions/one.py':source};plan=alembic_plan(sources)
        conn=FakeConnection(**kwargs);executor=Executor(lambda:conn,database='fixture',user='migrator')
        executor._ledger=Mock(return_value=bool(kwargs.get('heads') or kwargs.get('hashes')))
        return executor,conn,plan,sources
    def test_atomic_heads_hashes_authority_and_commit(self):
        executor,conn,plan,sources=self.fixture();calls=[]
        result=executor.apply(plan,sources=sources,authorize=lambda:calls.append(True))
        self.assertTrue(result['transaction_committed']);self.assertEqual(result['heads'],['one'])
        self.assertEqual(conn.hashes,dict(plan.source_hashes));self.assertGreaterEqual(len(calls),4)
        statements=[s for s,a in conn.statements]
        self.assertLess(next(i for i,s in enumerate(statements) if 'pg_try_advisory' in s),statements.index('DELETE FROM "public"."alembic_version"'))
        self.assertIn('COMMIT',statements);self.assertTrue(conn.closed)
    def test_stale_database_heads_roll_back_without_mutation(self):
        executor,conn,plan,sources=self.fixture(heads=('unexpected',))
        with self.assertRaises(MigrationError) as error:executor.apply(plan,sources=sources,authorize=lambda:None)
        self.assertEqual(error.exception.code,'source_or_ledger_changed')
        self.assertFalse(any(s.startswith(('INSERT','DELETE','CREATE')) for s,a in conn.statements))
        self.assertEqual(conn.statements[-1][0],'ROLLBACK')
    def test_busy_lock_rolls_back(self):
        executor,conn,plan,sources=self.fixture(busy=True)
        with self.assertRaises(MigrationError) as error:executor.apply(plan,sources=sources,authorize=lambda:None)
        self.assertEqual(error.exception.code,'migration_busy');self.assertNotIn('COMMIT',[s for s,a in conn.statements])
    def test_lost_commit_reply_is_uncertain_not_success(self):
        executor,conn,plan,sources=self.fixture(commit_error=True)
        with self.assertRaises(MigrationError) as error:executor.apply(plan,sources=sources,authorize=lambda:None)
        self.assertEqual(error.exception.code,'commit_uncertain');self.assertNotIn('sensitive',str(error.exception))
    def test_ddl_failure_prevents_head_and_hash_writes(self):
        from unittest.mock import Mock
        executor,conn,_,_=self.fixture();sources={'versions/one.py':SOURCE};plan=alembic_plan(sources)
        executor._operation=Mock(side_effect=MigrationError('schema_verification_failed'))
        with self.assertRaises(MigrationError):executor.apply(plan,sources=sources,authorize=lambda:None)
        self.assertFalse(any(s.startswith(('INSERT','DELETE')) for s,a in conn.statements))
        self.assertEqual(conn.statements[-1][0],'ROLLBACK')


class LedgerBoundaryTests(unittest.TestCase):
    def test_inbound_cascade_rejected_before_ledger_rows(self):
        from migration_postgres import Executor
        from unittest.mock import Mock
        executor=Executor(lambda:None,database='fixture',user='migrator')
        executor._relation=Mock(return_value=(42,'r',False,False,False,2,False,False))
        executor._columns=Mock(return_value=[('version_num',1043,68,True,'','',False,1)])
        def query(c,sql,args=(),limit=1001):
            if 'confrelid' in sql:return [(1,)]
            if 'pg_constraint' in sql:return [('p',True)]
            return [(True,True,True,True,1,1,True,True,'btree','version_num','pg_catalog','text_ops')]
        executor._query=query
        with self.assertRaises(MigrationError) as error:executor._ledger(Mock(),'alembic_version',('version_num',))
        self.assertEqual(error.exception.code,'unsupported_ledger')
