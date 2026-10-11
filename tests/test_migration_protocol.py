import copy
import dataclasses
import unittest
from unittest.mock import MagicMock,patch
from migration_plan import alembic_plan,PlanError
from migration_postgres import MigrationError
import migration_protocol as protocol
from migration_helper_entry import decode

SOURCE="""from alembic import op
import sqlalchemy as sa
revision='a'
down_revision=None
def upgrade():
    op.create_table('items',sa.Column('id',sa.Integer(),primary_key=True))
def downgrade():
    op.drop_table('items')
"""

class MigrationProtocolTests(unittest.TestCase):
    def setUp(self):
        self.request={'protocol_version':1,'operation':'inspect_plan','engine':'postgresql','framework':'alembic','grammar':'alembic-postgresql-v1','database':'fixture','user':'migrator','password':'private-fixture-secret','port':5432,'sources':{'a.py':SOURCE}}
        self.baseline={'heads':[],'ledger':[],'hashes':{}}
        self.identity={'database':'fixture','user':'migrator','database_oid':'42','cluster_identity_verified':False}
        self.plan=alembic_plan(self.request['sources'])
        self.proof=protocol.make_proof(self.request,self.plan,self.baseline,self.identity,{'items':None})
    def test_empty_sources_require_empty_ledger_and_have_no_effects(self):
        from migration_laravel import laravel_plan
        for framework,grammar in [('alembic','alembic-postgresql-v1'),('laravel','laravel12-stock-postgresql-v1')]:
            request=dict(self.request,framework=framework,grammar=grammar,sources={})
            protocol.validate_request(request)
            plan=protocol.rebuild(request,self.baseline)
            self.assertEqual(plan.migrations,())
            proof=protocol.make_proof(request,plan,self.baseline,self.identity,{})
            for operation in ('apply','reconcile'):
                connection=MagicMock();executor=MagicMock()
                with patch.object(protocol,'Executor',return_value=executor),patch.object(protocol,'LaravelExecutor',return_value=executor),patch.object(protocol,'start'),patch.object(protocol,'identity',return_value=self.identity),patch.object(protocol,'read_ledger',return_value=(self.baseline,[])),patch.object(protocol,'schema_state',return_value={}):
                    result=protocol.dispatch(dict(request,operation=operation,proof=proof),connect=lambda:connection,authorize=lambda:None)
                self.assertEqual(result['status'],'committed');self.assertTrue(result['no_op'])
                executor.apply.assert_not_called();executor._operation.assert_not_called()
                connection.rollback.assert_called_once()
        with self.assertRaises(PlanError):alembic_plan({},current_heads=['a'],applied_hashes={'a':'a'*64})
        with self.assertRaises(PlanError):laravel_plan({},[{'migration':'2026_10_11_000001_initial','batch':1}],{'2026_10_11_000001_initial':'a'*64})

    def test_total_deadline_expires_before_connection_and_resets_context(self):
        from migration_plan import PLANNING_DEADLINE
        connection=MagicMock()
        with patch.object(protocol.time,'monotonic',side_effect=[0,91]):
            with self.assertRaises(PlanError):protocol.dispatch(self.request,connect=connection,authorize=lambda:None)
        connection.assert_not_called()
        self.assertIsNone(PLANNING_DEADLINE.get())

    def test_closed_wire_and_source_payload_no_destination_or_sql(self):
        for field in ('host','command','sql','authorize','actor'):
            with self.assertRaises(MigrationError):protocol.validate_request(dict(self.request,**{field:'forbidden'}))
        for change in ({'engine':'mysql'},{'operation':'downgrade'},{'port':True},{'protocol_version':True},{'grammar':'custom-application'}):
            with self.assertRaises(MigrationError):protocol.validate_request(dict(self.request,**change))
        with self.assertRaises(ValueError):decode('{"operation":"apply","operation":"reconcile"}')
        with self.assertRaises(ValueError):decode('{"value":NaN}')
    def test_proof_rebuilt_from_sources_not_caller_operations(self):
        request=dict(self.request,operation='apply',proof=self.proof)
        self.assertEqual(protocol.check_proof(request)[0],self.plan)
        corrupt=copy.deepcopy(self.proof);corrupt['plan']['migrations'][0][6][0][2]='other_table'
        corrupt['digest']=protocol.digest({k:v for k,v in corrupt.items() if k!='digest'})
        with self.assertRaises(MigrationError):protocol.check_proof(dict(request,proof=corrupt))
        with self.assertRaises(MigrationError):protocol.check_proof(dict(request,sources={'a.py':SOURCE+'\n# changed'}))
        self.assertNotIn('private-fixture-secret',protocol.canonical(self.proof))
    def test_authorization_denial_happens_before_connection(self):
        connect=MagicMock();authorize=MagicMock(side_effect=MigrationError('authority_denied'))
        with self.assertRaises(MigrationError):protocol.dispatch(self.request,connect=connect,authorize=authorize)
        connect.assert_not_called()
    def reconcile(self,baseline,state,rows=()):
        connection=MagicMock();executor=MagicMock()
        with patch.object(protocol,'Executor',return_value=executor),patch.object(protocol,'start'),patch.object(protocol,'identity',return_value=self.identity),patch.object(protocol,'read_ledger',return_value=(baseline,rows)),patch.object(protocol,'schema_state',return_value=state),patch.object(protocol,'verify_postconditions') as verify:
            result=protocol.dispatch(dict(self.request,operation='reconcile',proof=self.proof),connect=lambda:connection,authorize=lambda:None)
            executor.apply.assert_not_called();executor._operation.assert_not_called()
            connection.rollback.assert_called_once();connection.close.assert_called_once()
            return result,verify.call_count
    def test_reconcile_never_applies_and_requires_exact_target_hash_commit_proof(self):
        row=('a',self.plan.source_hashes[0][1],'a.py',self.plan.digest)
        result,count=self.reconcile(self.proof['target'],{'items':{'observed':True}},[row])
        self.assertEqual(result['status'],'committed');self.assertEqual(count,1)
        with self.assertRaises(MigrationError):self.reconcile(self.proof['target'],{'items':{'observed':True}},[row[:-1]+('other-operation',)])
    def test_unchanged_baseline_only_is_safe_not_applied(self):
        result,count=self.reconcile(self.baseline,{'items':None})
        self.assertEqual(result['status'],'not_applied');self.assertEqual(count,0)
        result,count=self.reconcile(self.baseline,{'items':{'foreign':True}})
        self.assertEqual(result['status'],'reconciliation_required')
    def test_wrong_database_identity_cannot_reconcile(self):
        self.identity=dict(self.identity,database_oid='43')
        with self.assertRaises(MigrationError):self.reconcile(self.proof['target'],{},[])
    def test_apply_rechecks_baseline_inside_executor_transaction_before_effects(self):
        connection=MagicMock();created=[];effects=[]
        class FakeExecutor:
            def __init__(self,connect,**kw):self.connect=connect;created.append(self)
            def apply(self,plan,*,sources,authorize):
                authorize();self.connect();authorize();effects.append('DDL');return {'transaction_committed':True}
        with patch.object(protocol,'Executor',FakeExecutor),patch.object(protocol,'identity',return_value=self.identity),patch.object(protocol,'read_ledger',return_value=(self.baseline,[])),patch.object(protocol,'schema_state',return_value={'items':{'changed':True}}):
            with self.assertRaises(MigrationError):protocol.dispatch(dict(self.request,operation='apply',proof=self.proof),connect=lambda:connection,authorize=lambda:None)
        self.assertEqual(effects,[])

if __name__=='__main__':unittest.main()
