"""Parse declarative Alembic upgrades without importing or executing project code."""
import ast
import contextvars
import time
from dataclasses import dataclass
import hashlib
import json
import re


class PlanError(ValueError):
    pass


PLANNING_DEADLINE=contextvars.ContextVar('migration_planning_deadline',default=None)
def planning_remaining():
    deadline=PLANNING_DEADLINE.get()
    if deadline is None:return 10.0
    remaining=deadline-time.monotonic()
    if remaining<=0:raise PlanError('Migration planning deadline exceeded')
    return remaining


@dataclass(frozen=True)
class MigrationPlan:
    framework: str
    migrations: tuple
    current_heads: tuple
    target_heads: tuple
    digest: str
    source_hashes: tuple


def _error(path, node, reason):
    raise PlanError(f'{path}:{getattr(node, "lineno", 1)}: {reason}')


def _literal(path, node):
    # Literal means syntax, not ast.literal_eval of arbitrary collections/code.
    if isinstance(node, ast.Constant) and type(node.value) in (str, int, bool, type(None)):
        return node.value
    _error(path, node, 'Expected a literal value')


def _name(path, node):
    value = _literal(path, node)
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', value):
        _error(path, node, 'Unsupported database identifier')
    return value


def _kwargs(path, node, allowed):
    result = {}
    for item in node.keywords:
        if item.arg not in allowed or item.arg in result:
            _error(path, item.value, 'Unsupported or duplicate argument')
        result[item.arg] = item.value
    return result


def _bool(path, node):
    value = _literal(path, node)
    if type(value) is not bool:
        _error(path, node, 'Expected a literal boolean')
    return value


def _type(path, node, sqlalchemy):
    args = []
    if isinstance(node, ast.Call):
        if node.keywords: _error(path, node, 'Type keyword arguments are unsupported')
        args = node.args
        node = node.func
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name) or node.value.id != sqlalchemy:
        _error(path, node, 'Use an explicit SQLAlchemy builtin type')
    kinds = {'Integer':'integer','BigInteger':'bigint','SmallInteger':'smallint','Boolean':'boolean','Text':'text','Date':'date','DateTime':'timestamp','String':'varchar'}
    if node.attr not in kinds: _error(path, node, 'Unsupported column type')
    if node.attr == 'String':
        if len(args) != 1: _error(path, node, 'String requires an explicit bounded length')
        size = _literal(path, args[0])
        if type(size) is not int or not 1 <= size <= 1048576: _error(path, node, 'Invalid string length')
        return (kinds[node.attr], size)
    if args: _error(path, node, 'Unexpected type arguments')
    return (kinds[node.attr],)


def _column(path, node, sqlalchemy, additive=False):
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Name) or node.func.value.id != sqlalchemy or node.func.attr != 'Column':
        _error(path, node, 'Expected SQLAlchemy Column')
    if len(node.args) != 2: _error(path, node, 'Column requires name and supported type only')
    kwargs = _kwargs(path, node, {'nullable','primary_key'})
    primary = _bool(path, kwargs['primary_key']) if 'primary_key' in kwargs else False
    nullable = _bool(path, kwargs['nullable']) if 'nullable' in kwargs else not primary
    if primary and nullable: _error(path, node, 'A primary key cannot be nullable')
    if additive and (not nullable or primary): _error(path, node, 'Adding a column requires nullable=True and no primary key')
    return (_name(path, node.args[0]), _type(path, node.args[1], sqlalchemy), nullable, primary)


def _operation(path, node, aliases):
    op, sa = aliases['op'], aliases['sa']
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Name) or node.func.value.id != op:
        _error(path, node, 'Only explicit Alembic operation calls are supported')
    kind = node.func.attr
    allowed = {'create_table':{'schema'},'add_column':{'schema'},'create_index':{'schema','unique'}}
    if kind not in allowed: _error(path, node, 'Unsupported or destructive upgrade operation: '+kind)
    kwargs = _kwargs(path, node, allowed[kind])
    schema = _name(path, kwargs['schema']) if 'schema' in kwargs else 'public'
    if kind == 'create_table':
        if len(node.args) < 2: _error(path, node, 'A table requires at least one column')
        columns = tuple(_column(path, column, sa) for column in node.args[1:])
        if len(columns) > 256 or len({column[0] for column in columns}) != len(columns): _error(path, node, 'Duplicate or excessive columns')
        return (kind, schema, _name(path, node.args[0]), columns)
    if kind == 'add_column':
        if len(node.args) != 2: _error(path, node, 'add_column requires table and column')
        return (kind, schema, _name(path, node.args[0]), _column(path, node.args[1], sa, True))
    if len(node.args) != 3 or not isinstance(node.args[2], (ast.List, ast.Tuple)):
        _error(path, node, 'create_index requires literal index, table and column list')
    if 'unique' in kwargs and _bool(path, kwargs['unique']): _error(path, node, 'Unique indexes require a separate verified executor capability')
    columns = tuple(_name(path, item) for item in node.args[2].elts)
    if not columns or len(columns)>32 or len(set(columns))!=len(columns): _error(path, node, 'Invalid index columns')
    return (kind, schema, _name(path, node.args[1]), _name(path, node.args[0]), columns)


def _annotation(path, node, typing_names):
    if node is None or (isinstance(node,ast.Constant) and node.value is None):return
    if isinstance(node,ast.Name) and node.id=='str':return
    if isinstance(node,ast.BinOp) and isinstance(node.op,ast.BitOr):
        _annotation(path,node.left,typing_names);_annotation(path,node.right,typing_names);return
    if isinstance(node,ast.Subscript) and isinstance(node.value,ast.Name) and node.value.id in typing_names:
        elements=node.slice.elts if isinstance(node.slice,ast.Tuple) else (node.slice,)
        if not elements or len(elements)>8:_error(path,node,'Unsupported annotation arity')
        for element in elements:_annotation(path,element,typing_names)
        return
    _error(path,node,'Executable or unsupported type annotation')


def parse_alembic(path, source, *, include_operations=True):
    if (not isinstance(path,str) or not 1<=len(path)<=512 or path.startswith('/')
            or any(part in ('','..','.') for part in path.split('/'))
            or any(ord(c)<32 or ord(c)==127 for c in path) or not path.endswith('.py')):
        raise PlanError('Migration source requires a bounded relative Python path')
    if not isinstance(source, str) or len(source.encode()) > 1048576: raise PlanError(f'{path}:1: Source exceeds bound')
    try: tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError) as error: raise PlanError(f'{path}:{getattr(error,"lineno",1)}: Invalid Python source') from None
    aliases = {}; metadata = {}; upgrade = None; functions=set();typing_names=set()
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value,str): continue
        if isinstance(node, ast.Import) and len(node.names)==1 and node.names[0].name=='sqlalchemy':
            if 'sa' in aliases: _error(path,node,'Duplicate SQLAlchemy import')
            aliases['sa']=node.names[0].asname or 'sqlalchemy';continue
        if isinstance(node,ast.ImportFrom) and node.level==0 and node.module=='typing':
            for item in node.names:
                if item.name not in {'Sequence','Union','Optional'} or item.asname or item.name in typing_names:
                    _error(path,node,'Unsupported typing import')
                typing_names.add(item.name)
            continue
        if isinstance(node, ast.ImportFrom) and node.level==0 and node.module=='alembic' and len(node.names)==1 and node.names[0].name=='op':
            if 'op' in aliases: _error(path,node,'Duplicate Alembic import')
            aliases['op']=node.names[0].asname or 'op';continue
        if isinstance(node,(ast.Assign,ast.AnnAssign)):
            if isinstance(node,ast.AnnAssign):_annotation(path,node.annotation,typing_names)
            targets=node.targets if isinstance(node,ast.Assign) else [node.target]
            if len(targets)!=1 or not isinstance(targets[0],ast.Name) or targets[0].id not in {'revision','down_revision','branch_labels','depends_on'}:
                _error(path,node,'Only revision metadata assignments are supported')
            key=targets[0].id
            if key in metadata: _error(path,node,'Duplicate revision metadata')
            value=node.value
            metadata[key]=tuple(_literal(path,item) for item in value.elts) if isinstance(value,(ast.Tuple,ast.List)) else _literal(path,value)
            continue
        if isinstance(node,ast.FunctionDef) and node.name in {'upgrade','downgrade'}:
            _annotation(path,node.returns,typing_names)
            if getattr(node,'type_params',None):_error(path,node,'Generic migration functions are unsupported')
            if node.name in functions or node.decorator_list or node.args.args or node.args.posonlyargs or node.args.kwonlyargs or node.args.vararg or node.args.kwarg:
                _error(path,node,'Migration functions must be undecorated and accept no arguments')
            functions.add(node.name)
            if node.name=='upgrade':upgrade=node
            continue
        _error(path,node,'Unsupported module code or import')
    if set(aliases.values()).intersection(typing_names):_error(path,tree,'Import names collide')
    if set(aliases)!={'op','sa'} or aliases['op']==aliases['sa'] or any(value in {'revision','down_revision','branch_labels','depends_on','upgrade','downgrade'} for value in aliases.values()):
        _error(path,tree,'Distinct SQLAlchemy and Alembic imports are required')
    if upgrade is None or not {'revision','down_revision'}<=set(metadata): _error(path,tree,'Missing upgrade or revision metadata')
    def revisions(value):
        result=() if value is None else value if isinstance(value,tuple) else (value,)
        if len(result)>100 or len(set(result))!=len(result) or any(not isinstance(item,str) or not re.fullmatch('[A-Za-z0-9_]{1,64}',item) for item in result):
            _error(path,tree,'Invalid revision references')
        return result
    if not isinstance(metadata['revision'],str):_error(path,tree,'Revision must be a literal string')
    revision=revisions(metadata['revision'])
    if len(revision)!=1: _error(path,tree,'One revision ID is required')
    parents=revisions(metadata['down_revision']);dependencies=revisions(metadata.get('depends_on'))
    labels=revisions(metadata.get('branch_labels'))
    operations=[]
    for node in upgrade.body if include_operations else ():
        if isinstance(node,ast.Pass) or (isinstance(node,ast.Expr) and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str)):continue
        if not isinstance(node,ast.Expr): _error(path,node,'Conditional, dynamic, or indirect upgrades are unsupported')
        operations.append(_operation(path,node.value,aliases))
        if len(operations)>256:_error(path,node,'Migration operation count exceeds limit')
    return (revision[0],parents,dependencies,labels,path,hashlib.sha256(source.encode()).hexdigest(),tuple(operations))


def alembic_plan(sources, current_heads=(), applied_hashes=None):
    """Return immutable migrations in dependency order; execution is a separate adapter.

    Migration tuple: revision, parents, dependencies, labels, path, source_sha256,
    operations. Operations contain only tuples/scalars; no executable source.
    """
    if not isinstance(sources,dict) or not 0<=len(sources)<=1000:raise PlanError('A bounded source mapping is required')
    if any(not isinstance(path,str) or not isinstance(source,str) for path,source in sources.items()):raise PlanError('Source paths and documents must be strings')
    if sum(len(source.encode()) for source in sources.values())>16777216:raise PlanError('Migration source set exceeds bound')
    if not isinstance(current_heads,(tuple,list)):raise PlanError('Database ledger heads must be a sequence')
    if applied_hashes is not None and not isinstance(applied_hashes,dict):raise PlanError('Applied hashes must be a mapping')
    records={};labels={}
    for path,source in sorted(sources.items()):
        planning_remaining()
        record=parse_alembic(path,source,include_operations=False)
        if record[0] in records:raise PlanError(f'{path}:1: Duplicate revision')
        records[record[0]]=record
        for label in record[3]:
            if label in labels:raise PlanError(f'{path}:1: Duplicate branch label')
            labels[label]=record[0]
    visiting=set();visited=set();ordered=[]
    def visit(revision):
        if revision not in records:raise PlanError('Unknown revision dependency: '+revision)
        if revision in visiting:raise PlanError('Revision graph contains a cycle: '+revision)
        if revision in visited:return
        if len(visiting)>=256:raise PlanError('Revision graph depth exceeds parser limit')
        visiting.add(revision)
        for parent in records[revision][1]+records[revision][2]:visit(parent)
        visiting.remove(revision);visited.add(revision);ordered.append(revision)
    try:
        for revision in sorted(records):visit(revision)
    except RecursionError:raise PlanError('Revision graph depth exceeds parser limit') from None
    heads=tuple(current_heads)
    if any(not isinstance(head,str) for head in heads) or len(set(heads))!=len(heads) or any(head not in records for head in heads):raise PlanError('Database ledger contains duplicate or unknown heads')
    applied=set()
    def mark(revision):
        if revision in applied:return
        applied.add(revision)
        for parent in records[revision][1]+records[revision][2]:mark(parent)
    for head in heads:mark(head)
    for selected in heads:
        ancestors=set()
        def ancestry(revision):
            for parent in records[revision][1]+records[revision][2]:
                if parent not in ancestors:
                    ancestors.add(parent);ancestry(parent)
        ancestry(selected)
        if ancestors.intersection(heads):raise PlanError('Database ledger heads overlap ancestry')
    if applied and (not isinstance(applied_hashes,dict) or set(applied_hashes)!=applied):
        raise PlanError('Every applied migration requires its pinned source hash')
    for revision,digest in (applied_hashes or {}).items():
        if revision not in applied or records[revision][5]!=digest:raise PlanError('Applied migration source hash changed or ledger disagrees: '+revision)
    parents={parent for record in records.values() for parent in record[1]+record[2]}
    target=tuple(sorted(set(records)-parents))
    pending=tuple(parse_alembic(records[revision][4],sources[records[revision][4]]) for revision in ordered if revision not in applied)
    hashes=tuple((revision,records[revision][5]) for revision in sorted(records))
    canonical=json.dumps(('alembic',pending,tuple(sorted(heads)),target,hashes),separators=(',',':'))
    return MigrationPlan('alembic',pending,tuple(sorted(heads)),target,hashlib.sha256(canonical.encode()).hexdigest(),hashes)
