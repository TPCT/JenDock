import dataclasses
import hashlib
from pathlib import Path
import tempfile
import unittest
from migration_laravel import parse_laravel, laravel_plan
from migration_plan import PlanError

FIRST='2026_10_11_100000_create_customers'
SECOND='2026_10_11_110000_add_notes'


def source(up,down="Schema::dropIfExists('customers');"):
    return '''<?php
declare(strict_types=1);
use Illuminate\\Database\\Migrations\\Migration;
use Illuminate\\Database\\Schema\\Blueprint;
use Illuminate\\Support\\Facades\\Schema;
return new class extends Migration {
    public function up(): void {
        '''+up+'''
    }
    public function down(): void {
        '''+down+'''
    }
};
'''


class LaravelPlanTests(unittest.TestCase):
    def test_real_php_parser_useful_create_and_additive_index_plan(self):
        text=source("Schema::create('customers', function (Blueprint $table) {$table->id(); $table->string('name', 120); $table->text('note')->nullable(); $table->timestamps(); $table->index(['name'], 'ix_customers_name');});")
        plan=laravel_plan({FIRST+'.php':text})
        operation=plan.migrations[0][6]
        self.assertEqual(operation[0][0],'create_table')
        self.assertEqual(operation[0][3][0],('id',('bigint',),False,True))
        self.assertEqual(operation[0][3][1],('name',('varchar',120),False,False))
        self.assertEqual(operation[-1],('create_index','public','customers','ix_customers_name',('name',)))
        self.assertEqual(plan.next_batch,1)
        self.assertEqual(plan.source_hashes,((FIRST,hashlib.sha256(text.encode()).hexdigest()),))
        with self.assertRaises(dataclasses.FrozenInstanceError):plan.next_batch=99

    def test_actual_ledger_batches_pending_hashes_and_old_destructive_up(self):
        old=source("Schema::dropIfExists('old_table');")
        new=source("Schema::table('customers', function (Blueprint $table) {$table->text('note')->nullable(); $table->index('note');});")
        sources={FIRST+'.php':old,SECOND+'.php':new}
        hashes={FIRST:hashlib.sha256(old.encode()).hexdigest()}
        plan=laravel_plan(sources,[{'migration':FIRST,'batch':7}],hashes)
        self.assertEqual(plan.next_batch,8);self.assertEqual(plan.applied_batches,((FIRST,7),))
        self.assertEqual([item[0] for item in plan.migrations],[SECOND])
        self.assertEqual(plan.migrations[0][6][0],('add_column','public','customers',('note',('text',),True,False)))
        self.assertEqual(plan.migrations[0][6][1],('create_index','public','customers','customers_note_index',('note',)))
        with self.assertRaisesRegex(PlanError,'destructive'):laravel_plan(sources)
        with self.assertRaisesRegex(PlanError,'hash changed'):laravel_plan(dict(sources,**{FIRST+'.php':old+'\n'}),[{'migration':FIRST,'batch':7}],hashes)
        with self.assertRaisesRegex(PlanError,'pinned'):laravel_plan(sources,[{'migration':FIRST,'batch':7}])
        hashes[SECOND]=hashlib.sha256(new.encode()).hexdigest()
        complete=laravel_plan(sources,[{'migration':FIRST,'batch':7},{'migration':SECOND,'batch':8}],hashes)
        self.assertEqual(complete.migrations,())

    def test_destructive_raw_dynamic_connection_and_helpers_rejected(self):
        operations=["Schema::drop('customers');", "DB::statement('DELETE FROM customers');", "Schema::connection('production')->create('x', function(Blueprint $table){$table->id();});", "helper();", "if (true) {Schema::drop('x');}", "Schema::table('x', function(Blueprint $table){$table->dropColumn('name');});", "Schema::table('x', function(Blueprint $table){$table->string('new');});", "Schema::create('x', function(Blueprint $table){$table->id();$table->string('x')->default('secret');});", "Schema::create('x', function(Blueprint $table) use ($secret) {$table->id();});", "Schema::create('x', function(Blueprint $table){$table->{$method}('x');});"]
        for operation in operations:
            with self.subTest(operation=operation),self.assertRaisesRegex(PlanError,r'2026_10_11_100000_create_customers.php:\d+:'):
                parse_laravel(FIRST+'.php',source(operation))
        with self.assertRaises(PlanError):parse_laravel(FIRST+'.php',source('').replace('public function up()',"protected $connection = 'production'; public function up()"))
        with self.assertRaises(PlanError):parse_laravel(FIRST+'.php',source('').replace('public function up()',"public function helper(){} public function up()"))

    def test_source_is_parsed_not_executed_including_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel=Path(directory)/'executed'
            evil="file_put_contents('"+str(sentinel)+"', 'executed');"
            with self.assertRaises(PlanError):parse_laravel(FIRST+'.php',source(evil))
            accepted=parse_laravel(FIRST+'.php',source('',evil))
            self.assertEqual(accepted[6],());self.assertFalse(sentinel.exists())
            with self.assertRaises(PlanError):parse_laravel(FIRST+'.php',source('')+evil)
            self.assertFalse(sentinel.exists())

    def test_named_classes_aliases_and_invalid_syntax(self):
        text=source("Schema::create('x', function(Blueprint $table){$table->integer('id')->primary();});")
        named=text.replace('return new class extends Migration','class CreateCustomers extends Migration').replace('};','}',1)
        self.assertEqual(parse_laravel(FIRST+'.php',named)[6][0][0],'create_table')
        aliased=text.replace('Facades\\Schema;','Facades\\Schema as S;').replace('Schema::','S::')
        self.assertEqual(parse_laravel(FIRST+'.php',aliased)[6],parse_laravel(FIRST+'.php',text)[6])
        with self.assertRaisesRegex(PlanError,'syntax'):parse_laravel(FIRST+'.php','<?php return new class {public function up( {')
        with self.assertRaises(PlanError):parse_laravel('../'+FIRST+'.php',text)
        with self.assertRaises(PlanError):parse_laravel('not_a_migration.php',text)

    def test_identity_and_source_types_preserved_in_immutable_plan(self):
        def plan(body):
            return laravel_plan({FIRST+'.php':source("Schema::create('x',function(Blueprint $table){"+body+"});")})
        identity=plan("$table->id();")
        plain=plan("$table->bigInteger('id')->primary();")
        self.assertEqual(identity.migrations[0][6],plain.migrations[0][6])
        self.assertEqual(identity.column_semantics[0][1][0][-3:],('id',True,True))
        self.assertEqual(plain.column_semantics[0][1][0][-3:],('bigInteger',False,False))
        self.assertNotEqual(identity.column_semantics,plain.column_semantics)
        dates=plan("$table->dateTime('a');$table->timestamp('b');")
        self.assertEqual([v[4] for v in dates.column_semantics[0][1]],['dateTime','timestamp'])
        with self.assertRaises(dataclasses.FrozenInstanceError):plain.column_semantics=()

    def test_fully_qualified_names_do_not_use_short_import_aliases(self):
        text=source("Schema::create('x',function(Blueprint $table){$table->id();});")
        for old,new in [('extends Migration',r'extends \Migration'),('function(Blueprint',r'function(\Blueprint'),('Schema::',r'\Schema::')]:
            with self.subTest(name=new),self.assertRaises(PlanError):
                parse_laravel(FIRST+'.php',text.replace(old,new))
        qualified=text.replace('extends Migration',r'extends \Illuminate\Database\Migrations\Migration').replace('function(Blueprint',r'function(\Illuminate\Database\Schema\Blueprint').replace('Schema::',r'\Illuminate\Support\Facades\Schema::')
        self.assertEqual(parse_laravel(FIRST+'.php',qualified)[6],parse_laravel(FIRST+'.php',text)[6])

    def test_multiple_primary_and_nonliteral_boolean_fail_with_source_location(self):
        bodies=["$table->integer('a')->primary();$table->integer('b')->primary();",
                r"$table->text('a')->nullable(namespace\flag);",
                "$table->text('a')->nullable($flag);",
                "$table->text('a')->nullable(Flags::ENABLED);",
                "$table->text('a')->nullable(true, false);"]
        for body in bodies:
            with self.subTest(body=body),self.assertRaisesRegex(PlanError,r'2026_10_11_100000_create_customers.php:\d+:'):
                parse_laravel(FIRST+'.php',source("Schema::create('x',function(Blueprint $table){"+body+"});"))

    def test_ledger_and_column_bounds(self):
        text=source("Schema::create('x', function(Blueprint $table){$table->id();});")
        for ledger in ([{'migration':FIRST,'batch':True}],[{'migration':'unknown','batch':1}],[{'migration':FIRST,'batch':1},{'migration':FIRST,'batch':2}]):
            with self.assertRaises(PlanError):laravel_plan({FIRST+'.php':text},ledger,{FIRST:hashlib.sha256(text.encode()).hexdigest()})
        for body in ("$table->string('x', 0);", "$table->id();$table->integer('id');", "$table->id()->nullable();", "$table->id();$table->index(['missing']);"):
            with self.assertRaises(PlanError):parse_laravel(FIRST+'.php',source("Schema::create('x',function(Blueprint $table){"+body+"});"))

if __name__=='__main__':unittest.main()
