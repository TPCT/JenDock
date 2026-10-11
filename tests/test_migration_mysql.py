"""Compiler-only evidence; no executor, ledger, actual dialect or DDL claims."""
import unittest
from migration_mysql import CompileError, compile_operation, compile_laravel_record, identifier
from migration_laravel import laravel_plan
from migration_plan import alembic_plan
import test_migration_laravel as laravel_fixture


class MySQLCompilerTests(unittest.TestCase):
    def record(self, body):
        source=laravel_fixture.source(body)
        return laravel_plan({'2026_10_11_100000_create_example.php':source}).migrations[0]

    def test_actual_laravel_parser_preserves_auto_unsigned_and_time_semantics(self):
        record=self.record("Schema::create('events',function(Blueprint $table){$table->id();$table->dateTime('opened');$table->timestamp('closed')->nullable();$table->boolean('enabled');$table->string('title',120);$table->timestamps();$table->index('title');});")
        for engine in ('mysql','mariadb'):
            statements=compile_laravel_record(record,database='business',engine=engine)
            self.assertIn('`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT',statements[0])
            self.assertIn('`opened` DATETIME NOT NULL',statements[0])
            self.assertIn('`closed` TIMESTAMP NULL',statements[0])
            self.assertIn('`created_at` TIMESTAMP NULL',statements[0])
            self.assertIn('`enabled` TINYINT(1) NOT NULL',statements[0])
            self.assertTrue(statements[0].endswith('ENGINE=InnoDB'))
            self.assertEqual(statements[1],'CREATE INDEX `events_title_index` ON `business`.`events` (`title`)')

    def test_plain_primary_is_not_implicitly_unsigned_or_auto_in_laravel(self):
        record=self.record("Schema::create('events',function(Blueprint $table){$table->bigInteger('id')->primary();});")
        sql=compile_laravel_record(record,database='business',engine='mysql')[0]
        self.assertIn('`id` BIGINT NOT NULL',sql)
        self.assertNotIn('UNSIGNED',sql);self.assertNotIn('AUTO_INCREMENT',sql)
        increments=self.record("Schema::create('events',function(Blueprint $table){$table->increments('id');});")
        self.assertIn('`id` INT UNSIGNED NOT NULL AUTO_INCREMENT',compile_laravel_record(increments,database='business',engine='mysql')[0])

    def test_nullable_addition_and_closed_destructive_boundary(self):
        record=self.record("Schema::table('events',function(Blueprint $table){$table->text('note')->nullable();});")
        self.assertEqual(compile_laravel_record(record,database='business',engine='mariadb'),('ALTER TABLE `business`.`events` ADD COLUMN `note` TEXT NULL',))
        for operation in (('drop_table','business','events',()),('execute','business','events','DELETE FROM events'),
                          ('add_column','business','events',('note',('text',),False,False))):
            with self.assertRaises(CompileError):compile_operation(operation,database='business',engine='mysql',framework='alembic')

    def test_alembic_actual_parser_namespace_ambiguity_not_silently_remapped(self):
        source="from alembic import op\nimport sqlalchemy as sa\nrevision='a'\ndown_revision=None\ndef upgrade():\n op.create_table('events',sa.Column('id',sa.Integer(),primary_key=True)SCHEMA)\ndef downgrade():\n pass\n"
        for schema in ('',",schema='public'"):
            plan=alembic_plan({'001.py':source.replace('SCHEMA',schema)})
            with self.assertRaisesRegex(CompileError,'ambiguous_alembic_namespace'):
                compile_operation(plan.migrations[0][6][0],database='business',engine='mysql',framework='alembic')
        plan=alembic_plan({'001.py':source.replace('SCHEMA',",schema='business'")})
        sql=compile_operation(plan.migrations[0][6][0],database='business',engine='mysql',framework='alembic')
        self.assertIn('`business`.`events`',sql);self.assertIn('AUTO_INCREMENT',sql)
        with self.assertRaisesRegex(CompileError,'foreign_namespace'):
            compile_operation(plan.migrations[0][6][0],database='other',engine='mysql',framework='alembic')

    def test_exact_semantic_consumption_rejects_trailing_none(self):
        record=self.record("Schema::create('events',function(Blueprint $table){$table->id();});")
        for suffix in ((None,), (None, record[7][0])):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(CompileError,'extra_column_semantics'):
                compile_laravel_record(record[:7]+(record[7]+suffix,),database='business',engine='mysql')

    def test_semantic_ordinals_preserve_create_groups_and_order_allowing_gaps(self):
        record=self.record("Schema::create('events',function(Blueprint $table){$table->id();$table->text('note');});")
        inconsistent=(record[7][0], (1,)+record[7][1][1:])
        with self.assertRaisesRegex(CompileError,'invalid_semantic_order'):
            compile_laravel_record(record[:7]+(inconsistent,),database='business',engine='mysql')
        additions=self.record("Schema::table('events',function(Blueprint $table){$table->text('a')->nullable();});Schema::table('events',function(Blueprint $table){$table->text('b')->nullable();});")
        decreasing=((4,)+additions[7][0][1:], (3,)+additions[7][1][1:])
        with self.assertRaisesRegex(CompileError,'invalid_semantic_order'):
            compile_laravel_record(additions[:7]+(decreasing,),database='business',engine='mysql')
        gaps=((4,)+additions[7][0][1:], (999,)+additions[7][1][1:])
        self.assertEqual(len(compile_laravel_record(additions[:7]+(gaps,),database='business',engine='mysql')),2)
        actual=self.record("Schema::table('events',function(Blueprint $table){});Schema::create('notes',function(Blueprint $table){$table->id();});")
        self.assertEqual(actual[7][0][0],1)
        self.assertEqual(len(compile_laravel_record(actual,database='business',engine='mysql')),1)

    def test_malformed_discriminators_and_case_insensitive_index_duplicates(self):
        for discriminator in ([], {}, None):
            operation=('create_table','business','events',(('id',(discriminator,),False,True),))
            with self.subTest(discriminator=discriminator), self.assertRaises(CompileError):
                compile_operation(operation,database='business',engine='mysql',framework='alembic')
        for columns in (('a','A'), ('a', [])):
            operation=('create_index','business','events','events_a_index',columns)
            with self.subTest(columns=columns), self.assertRaisesRegex(CompileError,'invalid_index'):
                compile_operation(operation,database='business',engine='mysql',framework='alembic')
        record=self.record("Schema::create('events',function(Blueprint $table){$table->id();});")
        semantic=record[7][0][:4]+([],)+record[7][0][5:]
        with self.assertRaisesRegex(CompileError,'invalid_column_semantics'):
            compile_laravel_record(record[:7]+((semantic,),),database='business',engine='mysql')

    def test_invalid_semantics_target_and_identifier_rejected(self):
        record=self.record("Schema::create('events',function(Blueprint $table){$table->id();});")
        for semantics in ((),(record[7][0][:-1]+(False,),),record[7]*2):
            with self.assertRaises(CompileError):compile_laravel_record(record[:7]+(semantics,),database='business',engine='mysql')
        for value in ('x` DROP DATABASE x','x.y','x'*65,'bad\x00',''):
            with self.assertRaises(CompileError):identifier(value)
        for database,engine in (('mysql','mysql'),('business','postgresql')):
            with self.assertRaises(CompileError):compile_laravel_record(record,database=database,engine=engine)
        op=('create_table','business','migrations',(('id',('integer',),False,True),))
        with self.assertRaisesRegex(CompileError,'reserved_relation'):compile_operation(op,database='business',engine='mysql',framework='alembic')


if __name__=='__main__':unittest.main()
