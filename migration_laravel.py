"""Laravel syntax-only planning via the pinned trusted nikic PHP parser.

LaravelPlan is not an executable Alembic plan. Its operation tuples describe the
shared structural shape only. The mandatory immutable column_semantics field
retains Laravel source type, autoincrement and unsigned behavior; an engine
adapter must consume and verify that metadata, including engine-specific temporal
and unsigned semantics. It must never infer autoincrement from a primary key.

Each migration record has the shared first seven fields plus a semantics tuple
at index 7. Plan column_semantics groups these by migration name; entries are
(statement ordinal, schema, table, column, source type, autoincrement, unsigned).
All are covered by the plan digest. No Laravel SQL executor is enabled here.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from migration_plan import MigrationPlan, PlanError


@dataclass(frozen=True)
class LaravelPlan(MigrationPlan):
    applied_batches: tuple
    next_batch: int
    # (migration, ((statement ordinal, schema, table, column, source type,
    #              autoincrement, unsigned), ...)); executors MUST consume this
    # alongside operations, never infer Laravel identity from an integer PK.
    column_semantics: tuple


def _fail(path, node, message):
    raise PlanError(f'{path}:{(node or {}).get("attributes",{}).get("startLine",1)}: {message}')


def _kind(node, kind):return isinstance(node,dict) and node.get('nodeType')==kind


def php_ast(path, source):
    if not isinstance(source,str) or len(source.encode())>1048576:_fail(path,None,'PHP source exceeds bound')
    root=Path(__file__).resolve().parent
    php=shutil.which('php')
    if not php:_fail(path,None,'Trusted PHP runtime is unavailable')
    try:
        result=subprocess.run([php,'-d','memory_limit=64M',str(root/'migration-parser/parse.php'),str(root/'.runtime/migration-parser-vendor')],input=source.encode(),capture_output=True,timeout=10)
        if len(result.stdout)>8388608:_fail(path,None,'PHP AST exceeds bound')
        document=json.loads(result.stdout)
        if result.returncode or 'error' in document:
            raise PlanError(f'{path}:{document.get("line",1)}: {document.get("error","PHP parsing failed")}')
        if not isinstance(document.get('nodes'),list):raise ValueError()
        return document['nodes']
    except (OSError,subprocess.TimeoutExpired,ValueError,RecursionError) as error:
        if isinstance(error,PlanError):raise
        _fail(path,None,'Trusted PHP parser failed; no source was executed')


def _identifier(path,node):
    if not _kind(node,'Scalar_String') or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,62}',node['value']):_fail(path,node,'Expected literal database identifier')
    return node['value']


def _symbol(node):
    return node.get('name') if isinstance(node,dict) and node.get('nodeType') in ('Identifier','Name','Name_FullyQualified') else None


def _class_symbol(node, role, aliases):
    full={'Migration':'Illuminate\\Database\\Migrations\\Migration',
          'Blueprint':'Illuminate\\Database\\Schema\\Blueprint',
          'Schema':'Illuminate\\Support\\Facades\\Schema'}[role]
    if _kind(node,'Name_FullyQualified'):return node.get('name')==full
    return _kind(node,'Name') and node.get('name')==aliases[role]


def _args(path,node):
    result=[]
    for arg in node.get('args',[]):
        if not _kind(arg,'Arg') or arg.get('name') or arg.get('byRef') or arg.get('unpack'):_fail(path,arg,'Named, reference or unpacked arguments are unsupported')
        result.append(arg['value'])
    return result


def _length(path,node):
    if not _kind(node,'Scalar_Int') or type(node['value']) is not int or not 1<=node['value']<=1048576:_fail(path,node,'Expected a bounded literal length')
    return node['value']


def _true(path,args):
    if not args:return True
    symbol=_symbol(args[0].get('name')) if len(args)==1 and _kind(args[0],'Expr_ConstFetch') else None
    if not isinstance(symbol,str) or symbol.lower() not in ('true','false'):_fail(path,args[0] if args else None,'Expected a boolean literal')
    return symbol.lower()=='true'


def _blueprint(path, node, variable, table, creating, semantics, ordinal):
    chain=[]
    while _kind(node,'Expr_MethodCall'):
        name=_symbol(node.get('name'))
        if not name:_fail(path,node,'Dynamic blueprint methods are unsupported')
        chain.append((name,_args(path,node),node));node=node['var']
        if len(chain)>8:_fail(path,node,'Blueprint chain exceeds bound')
    if not _kind(node,'Expr_Variable') or node.get('name')!=variable or not chain:_fail(path,node,'Only explicit blueprint operations are supported')
    chain.reverse();name,args,origin=chain.pop(0)
    if name=='index':
        if chain or not 1<=len(args)<=2:_fail(path,origin,'Invalid index declaration')
        if _kind(args[0],'Scalar_String'):columns=(_identifier(path,args[0]),)
        elif _kind(args[0],'Expr_Array'):
            columns=[]
            for item in args[0]['items']:
                if not _kind(item,'ArrayItem') or item.get('key') is not None or item.get('byRef') or item.get('unpack'):_fail(path,item,'Expected literal index columns')
                columns.append(_identifier(path,item['value']))
            columns=tuple(columns)
        else:_fail(path,args[0],'Expected literal index columns')
        if not columns or len(columns)>32 or len(set(columns))!=len(columns):_fail(path,origin,'Invalid index columns')
        index=_identifier(path,args[1]) if len(args)==2 else table+'_'+'_'.join(columns)+'_index'
        if len(index)>63:_fail(path,origin,'Provide an explicit bounded index name')
        return [],[('create_index','public',table,index,columns)]
    types={'integer':('integer',),'bigInteger':('bigint',),'smallInteger':('smallint',),'boolean':('boolean',),'text':('text',),'date':('date',),'dateTime':('timestamp',),'timestamp':('timestamp',)}
    if name=='timestamps':
        if args or chain:_fail(path,origin,'Only default timestamps are supported')
        semantics.extend((ordinal,'public',table,col,'timestamp',False,False) for col in ('created_at','updated_at'))
        return [('created_at',('timestamp',),True,False),('updated_at',('timestamp',),True,False)],[]
    primary=name in ('id','increments','bigIncrements')
    autoincrement=primary;unsigned=primary
    if name=='id' and not args:column='id'
    elif args:column=_identifier(path,args[0])
    else:_fail(path,origin,'Column name is required')
    if name=='string':
        if not 1<=len(args)<=2:_fail(path,origin,'Invalid string declaration')
        datatype=('varchar',_length(path,args[1]) if len(args)==2 else 255)
    elif primary:
        if len(args)>1:_fail(path,origin,'Unexpected identity column arguments')
        datatype=('integer',) if name=='increments' else ('bigint',)
    elif name in types:
        if len(args)!=1:_fail(path,origin,'Unsupported column arguments')
        datatype=types[name]
    else:_fail(path,origin,'Unsupported or destructive blueprint operation: '+name)
    nullable=False;seen=set()
    for modifier,arguments,where in chain:
        if modifier in seen:_fail(path,where,'Duplicate column modifier')
        seen.add(modifier)
        if modifier=='nullable':nullable=_true(path,arguments)
        elif modifier=='primary' and not arguments:primary=True
        else:_fail(path,where,'Unsupported column modifier: '+modifier)
    if primary and nullable:_fail(path,origin,'Primary key cannot be nullable')
    if not creating and (not nullable or primary):_fail(path,origin,'Added columns must be nullable without primary keys')
    semantics.append((ordinal,'public',table,column,name,autoincrement,unsigned))
    return [(column,datatype,nullable,primary)],[]


def _operations(path, statements, aliases, semantics):
    result=[]
    for ordinal,stmt in enumerate(statements):
        call=stmt.get('expr')
        if not _kind(stmt,'Stmt_Expression') or not _kind(call,'Expr_StaticCall') or not _class_symbol(call.get('class'),'Schema',aliases):_fail(path,stmt,'Only explicit Schema::create/table calls are supported')
        mode=_symbol(call.get('name'));args=_args(path,call)
        if mode not in ('create','table') or len(args)!=2:_fail(path,stmt,'Unsupported or destructive schema operation')
        table=_identifier(path,args[0]);closure=args[1]
        if (not _kind(closure,'Expr_Closure') or closure.get('uses') or closure.get('byRef') or closure.get('attrGroups') or len(closure.get('params',[]))!=1):_fail(path,closure,'Expected a simple Blueprint closure')
        parameter=closure['params'][0]
        if (not _kind(parameter.get('var'),'Expr_Variable') or not _class_symbol(parameter.get('type'),'Blueprint',aliases) or parameter.get('byRef') or parameter.get('variadic') or parameter.get('default') is not None or parameter.get('attrGroups') or parameter.get('flags') or parameter.get('hooks')):_fail(path,parameter,'Invalid Blueprint parameter')
        if closure.get('returnType') is not None and _symbol(closure['returnType'])!='void':_fail(path,closure,'Unsupported closure return type')
        columns=[];indexes=[]
        for operation in closure['stmts']:
            if not _kind(operation,'Stmt_Expression'):_fail(path,operation,'Dynamic Blueprint statements are unsupported')
            cols,idx=_blueprint(path,operation['expr'],parameter['var']['name'],table,mode=='create',semantics,ordinal)
            columns.extend(cols);indexes.extend(idx)
        if len(columns)>256 or len({column[0] for column in columns})!=len(columns):_fail(path,stmt,'Duplicate or excessive columns')
        if mode=='create':
            if sum(column[3] for column in columns)>1:_fail(path,stmt,'Multiple primary declarations are unsupported; separate fluent declarations are not a composite key')
            if not columns:_fail(path,stmt,'New tables require columns')
            if any(col not in {column[0] for column in columns} for index in indexes for col in index[4]):_fail(path,stmt,'Index references an absent new-table column')
            result.append(('create_table','public',table,tuple(columns)))
        else:result.extend(('add_column','public',table,column) for column in columns)
        result.extend(indexes)
        if len(result)>256:_fail(path,stmt,'Migration operation count exceeds bound')
    return tuple(result)


def parse_laravel(path,source,*,include_operations=True):
    if not isinstance(path,str) or not 1<=len(path)<=512 or path.startswith('/') or any(part in ('','.','..') for part in path.split('/')) or any(ord(c)<32 or ord(c)==127 for c in path):raise PlanError('A bounded relative migration path is required')
    name=Path(path).stem
    if not path.endswith('.php') or not re.fullmatch(r'\d{4}_\d{2}_\d{2}_\d{6}_[a-zA-Z0-9_]{1,180}',name):_fail(path,None,'Expected a Laravel timestamp migration filename')
    imports={'Illuminate\\Database\\Migrations\\Migration':'Migration','Illuminate\\Database\\Schema\\Blueprint':'Blueprint','Illuminate\\Support\\Facades\\Schema':'Schema'}
    aliases={};migration=None
    for node in php_ast(path,source):
        if _kind(node,'Stmt_Use') and node['type']==1:
            for item in node['uses']:
                full=_symbol(item['name'])
                if full not in imports or item.get('type') not in (0,1):_fail(path,item,'Unsupported migration import')
                role=imports[full];alias=_symbol(item.get('alias')) or role
                if role in aliases or alias.lower() in [value.lower() for value in aliases.values()]:_fail(path,item,'Duplicate or conflicting import')
                aliases[role]=alias
            continue
        if _kind(node,'Stmt_Declare') and not node.get('stmts'):
            if len(node['declares'])==1 and _symbol(node['declares'][0]['key'])=='strict_types' and node['declares'][0]['value'].get('value')==1:continue
        candidate=node
        if _kind(node,'Stmt_Return') and _kind(node.get('expr'),'Expr_New'):
            if node['expr'].get('args'):_fail(path,node,'Migration constructor arguments are unsupported')
            candidate=node['expr'].get('class')
        if not _kind(candidate,'Stmt_Class') or migration is not None:_fail(path,node,'Unsupported executable module code')
        migration=candidate
    if set(aliases)!=set(imports.values()) or migration is None:_fail(path,None,'Migration, Blueprint and Schema imports and one migration class are required')
    if migration.get('attrGroups') or migration.get('flags') or migration.get('implements') or not _class_symbol(migration.get('extends'),'Migration',aliases):_fail(path,migration,'Unsupported migration class declaration')
    methods={}
    for method in migration['stmts']:
        name_method=_symbol(method.get('name'))
        if (not _kind(method,'Stmt_ClassMethod') or name_method not in ('up','down') or name_method in methods or method.get('flags')!=1 or method.get('params') or method.get('byRef') or method.get('attrGroups') or method.get('stmts') is None):_fail(path,method,'Only public up/down methods are supported; connection overrides and helpers are prohibited')
        if method.get('returnType') is not None and _symbol(method['returnType'])!='void':_fail(path,method,'Unsupported migration return type')
        methods[name_method]=method
    if 'up' not in methods:_fail(path,migration,'Migration up method is required')
    semantics=[]
    ops=_operations(path,methods['up']['stmts'],aliases,semantics) if include_operations else ()
    return (name,(),(),(),path,hashlib.sha256(source.encode()).hexdigest(),ops,tuple(semantics))


def laravel_plan(sources,ledger=(),applied_hashes=None):
    if not isinstance(sources,dict) or not 1<=len(sources)<=1000 or any(not isinstance(k,str) or not isinstance(v,str) for k,v in sources.items()):raise PlanError('Expected a bounded migration source mapping')
    if sum(len(source.encode()) for source in sources.values())>16777216:raise PlanError('Migration source set exceeds bound')
    records={}
    for path,source in sorted(sources.items()):
        record=parse_laravel(path,source,include_operations=False)
        if record[0] in records:_fail(path,None,'Duplicate Laravel migration name')
        records[record[0]]=record
    if not isinstance(ledger,(tuple,list)) or len(ledger)>1000:raise PlanError('Expected actual bounded Laravel migration ledger rows')
    applied={}
    for row in ledger:
        if not isinstance(row,dict) or set(row)!={'migration','batch'} or not isinstance(row['migration'],str) or row['migration'] not in records or row['migration'] in applied or type(row['batch']) is not int or not 1<=row['batch']<2147483647:raise PlanError('Laravel ledger contains an unknown migration, duplicate, or invalid batch')
        applied[row['migration']]=row['batch']
    if applied_hashes is None:applied_hashes={}
    if not isinstance(applied_hashes,dict) or set(applied_hashes)!=set(applied):raise PlanError('Every applied migration requires its pinned source hash')
    if any(records[name][5]!=digest for name,digest in applied_hashes.items()):raise PlanError('Applied Laravel migration source hash changed')
    pending=tuple(parse_laravel(records[name][4],sources[records[name][4]]) for name in sorted(records) if name not in applied)
    batches=tuple(sorted(applied.items()));next_batch=max(applied.values(),default=0)+1
    hashes=tuple((name,records[name][5]) for name in sorted(records));current=tuple(sorted(applied));target=tuple(sorted(records))
    semantics=tuple((record[0],record[7]) for record in pending)
    digest=hashlib.sha256(json.dumps(('laravel',pending,current,target,hashes,batches,next_batch,semantics),separators=(',',':')).encode()).hexdigest()
    return LaravelPlan('laravel',pending,current,target,digest,hashes,batches,next_batch,semantics)
