import dataclasses
import hashlib
import unittest
from migration_plan import PlanError, alembic_plan, parse_alembic


def source(revision='first', parent=None, upgrade='pass', downgrade="op.drop_table('customers')", depends=None):
    return f'''"""Real declarative Alembic source fixture."""
from alembic import op
import sqlalchemy as sa
revision = {revision!r}
down_revision = {parent!r}
branch_labels = None
depends_on = {depends!r}
def upgrade() -> None:
    {upgrade}
def downgrade() -> None:
    {downgrade}
'''


class AlembicPlanTests(unittest.TestCase):
    def test_useful_schema_upgrade_and_destructive_downgrade_without_execution(self):
        text=source(upgrade="op.create_table('customers', sa.Column('id', sa.BigInteger(), primary_key=True), sa.Column('name', sa.String(120), nullable=False), sa.Column('active', sa.Boolean(), nullable=True))\n    op.add_column('orders', sa.Column('note', sa.Text(), nullable=True))\n    op.create_index('ix_customers_name', 'customers', ['name'], unique=False)")
        plan=alembic_plan({'versions/001.py':text})
        self.assertEqual(plan.target_heads,('first',))
        self.assertEqual([op[0] for op in plan.migrations[0][6]],['create_table','add_column','create_index'])
        self.assertEqual(plan.migrations[0][6][0][3][0],('id',('bigint',),False,True))
        self.assertEqual(dict(plan.source_hashes)['first'],hashlib.sha256(text.encode()).hexdigest())
        with self.assertRaises(dataclasses.FrozenInstanceError):plan.digest='x'
        with self.assertRaises(TypeError):plan.migrations[0][6][0][3][0][0]='x'

    def test_actual_branch_graph_and_ledger_pending_reconciliation(self):
        a=source('a');b=source('b','a');c=source('c','a');merge=source('merge',('b','c'))
        sources={'a.py':a,'b.py':b,'c.py':c,'merge.py':merge}
        hashes={rev:hashlib.sha256(text.encode()).hexdigest() for rev,text in [('a',a),('b',b),('c',c)]}
        plan=alembic_plan(sources,('b','c'),hashes)
        self.assertEqual([entry[0] for entry in plan.migrations],['merge'])
        self.assertEqual(plan.target_heads,('merge',))
        hashes['merge']=hashlib.sha256(merge.encode()).hexdigest()
        complete=alembic_plan(sources,('merge',),hashes)
        self.assertEqual(complete.migrations,())
        with self.assertRaisesRegex(PlanError,'overlap'):alembic_plan(sources,('a','b'),hashes)
        with self.assertRaisesRegex(PlanError,'pinned'):alembic_plan(sources,('b',),{})
        with self.assertRaisesRegex(PlanError,'hash changed'):alembic_plan(sources,('merge',),dict(hashes,a='0'*64))

    def test_graph_dependencies_unknown_cycles_duplicate_and_labels(self):
        plan=alembic_plan({'a.py':source('a'),'b.py':source('b',depends='a')})
        self.assertEqual([entry[0] for entry in plan.migrations],['a','b'])
        self.assertEqual(plan.target_heads,('b',))
        branched=alembic_plan({'a.py':source('a'),'b.py':source('b',depends='a'),'c.py':source('c','a')})
        self.assertEqual(branched.target_heads,('b','c'))
        for sources in ({'a.py':source('a','missing')},{'a.py':source('a','b'),'b.py':source('b','a')},{'a.py':source('a'),'b.py':source('a')}):
            with self.assertRaises(PlanError):alembic_plan(sources)
        with self.assertRaises(PlanError):alembic_plan({'a.py':source()},('unknown',),{})

    def test_unsafe_forward_code_is_rejected_with_source_location(self):
        for operation in ("op.drop_table('customers')", "op.execute('DELETE FROM customers')", "op.add_column('customers', sa.Column('x', sa.Text(), nullable=False))", "op.create_index('idx', 'customers', ['id'], unique=True)", "op.create_table('x', sa.Column('id', sa.Integer(), server_default=sa.text('evil()')))", "helper()", "if True:\n        op.create_table('x', sa.Column('id', sa.Integer()))", "creator = op.create_table", "op.create_table('x', *columns)"):
            with self.subTest(operation=operation),self.assertRaisesRegex(PlanError,r'versions/bad.py:\d+:'):
                alembic_plan({'versions/bad.py':source(upgrade=operation)})
        for code in ('import application_helpers\n','from application_helpers import build\n','creator = op.create_table\n',"print('must never execute')\n"):
            with self.assertRaises(PlanError):parse_alembic('unsafe.py',source()+code)

    def test_import_aliases_are_explicit_and_cannot_be_overwritten(self):
        text=source(upgrade="op.create_table('test', sa.Column('id', sa.Integer()))")
        aliased=text.replace('import op','import op as operations').replace('sqlalchemy as sa','sqlalchemy as sqltypes').replace('op.','operations.').replace('sa.','sqltypes.')
        self.assertEqual(parse_alembic('a.py',aliased)[6][0][0],'create_table')
        for altered in (text.replace('sqlalchemy as sa','sqlalchemy as op'),text+'\nop = evil\n',text.replace('def upgrade()', '@helper()\ndef upgrade()')):
            with self.assertRaises(PlanError):parse_alembic('a.py',altered)

    def test_plan_digest_tracks_full_source_not_only_operations(self):
        first=source();one=alembic_plan({'a.py':first});two=alembic_plan({'a.py':first+'\n# changed source\n'})
        self.assertNotEqual(one.digest,two.digest)
        self.assertEqual(one.migrations[0][6],two.migrations[0][6])

    def test_modern_alembic_typing_header_and_bounded_inputs(self):
        text=source().replace('from alembic import op','from typing import Sequence, Union\nfrom alembic import op').replace("revision = 'first'", "revision: str = 'first'")
        self.assertEqual(parse_alembic('versions/001.py',text)[0],'first')
        for path in ('../escape.py','/absolute.py','bad\nname.py','a//b.py'):
            with self.assertRaises(PlanError):parse_alembic(path,source())
        with self.assertRaises(PlanError):alembic_plan({'a.py':source()},'first')
        with self.assertRaises(PlanError):alembic_plan({'a.py':source()},(),[])
        with self.assertRaises(PlanError):parse_alembic('a.py',source().replace("revision = 'first'","revision = ('first',)"))

    def test_annotations_cannot_hide_module_execution(self):
        for text in (source().replace("revision = 'first'","revision: dangerous() = 'first'"),source().replace('def downgrade() -> None:', 'def downgrade() -> dangerous():'),source().replace('def upgrade() -> None:', 'def upgrade() -> dangerous():')):
            with self.assertRaisesRegex(PlanError,'annotation'):parse_alembic('bad.py',text)
        typed=source().replace('from alembic import op','from typing import Union, Sequence\nfrom alembic import op').replace('down_revision = None','down_revision: Union[str, Sequence[str], None] = None')
        self.assertEqual(parse_alembic('typed.py',typed)[0],'first')

    def test_historical_applied_upgrade_is_not_reclassified_or_executed(self):
        old=source('old',upgrade="op.drop_table('retired_table')")
        new=source('new','old',upgrade="op.add_column('customers', sa.Column('note', sa.Text(), nullable=True))")
        hashes={'old':hashlib.sha256(old.encode()).hexdigest()}
        plan=alembic_plan({'old.py':old,'new.py':new},('old',),hashes)
        self.assertEqual([migration[0] for migration in plan.migrations],['new'])
        with self.assertRaisesRegex(PlanError,'destructive'):alembic_plan({'old.py':old,'new.py':new})
        with self.assertRaisesRegex(PlanError,'hash changed'):alembic_plan({'old.py':old+'\n','new.py':new},('old',),hashes)

if __name__=='__main__':unittest.main()
