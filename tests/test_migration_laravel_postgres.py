import dataclasses
import unittest
from migration_laravel import laravel_plan
from migration_postgres import compile_operation, MigrationError
from migration_laravel_postgres import validate_plan, translated_operations

PATH='2026_10_11_120000_create_items.php'
def source(body):
    return '<?php use Illuminate\\Database\\Migrations\\Migration; use Illuminate\\Database\\Schema\\Blueprint; use Illuminate\\Support\\Facades\\Schema; return new class extends Migration { public function up():void {'+body+'}};'

class LaravelExecutorTests(unittest.TestCase):
    def plan(self,body):
        sources={PATH:source(body)};return laravel_plan(sources),sources
    def test_explicit_identity_vs_plain_pk(self):
        for declaration,serial in [("$t->id();",True),("$t->bigInteger('id')->primary();",False)]:
            plan,sources=self.plan("Schema::create('items',function(Blueprint $t){"+declaration+"});")
            validate_plan(plan,sources,[],{})
            op,auto=translated_operations(plan.migrations[0])[0]
            self.assertEqual('BIGSERIAL' in compile_operation(op,autoincrement_columns=auto),serial)
    def test_temporal_stock_precision_and_unsigned_pg_semantics(self):
        plan,_=self.plan("Schema::create('items',function(Blueprint $t){$t->id();$t->dateTime('at');$t->timestamp('other');$t->timestamps();});")
        op,auto=translated_operations(plan.migrations[0])[0]
        self.assertEqual([c[1] for c in op[3][1:]], [('timestamp',0)]*4)
        sql=compile_operation(op,autoincrement_columns=auto)
        self.assertNotIn('CHECK',sql);self.assertIn('timestamp(0)',sql)
    def test_semantics_tamper_fails_before_connection(self):
        plan,sources=self.plan("Schema::create('items',function(Blueprint $t){$t->id();});")
        with self.assertRaises(MigrationError):validate_plan(dataclasses.replace(plan,column_semantics=()),sources,[],{})
    def test_explicit_auto_override_preserves_legacy_and_rejects_unknown(self):
        operation=('create_table','public','items',(('id',('integer',),False,True),))
        self.assertIn('SERIAL',compile_operation(operation))
        self.assertNotIn('SERIAL',compile_operation(operation,autoincrement_columns=()))
        self.assertIn('SERIAL',compile_operation(operation,autoincrement_columns=('id',)))
        with self.assertRaises(MigrationError):compile_operation(operation,autoincrement_columns=('missing',))


class LedgerDefaultTests(unittest.TestCase):
    def test_owned_sequence_does_not_authorize_custom_default(self):
        from migration_postgres import Executor
        from unittest.mock import Mock
        executor=Executor(lambda:None,database='fixture',user='postgres')
        executor._columns=Mock(return_value=[('id',23,-1,True,'','',True,1)])
        executor._query=Mock(side_effect=[[(99,)],[]])
        with self.assertRaises(MigrationError):executor._check_column(Mock(),42,('id',('integer',),False,True),auto=True)
