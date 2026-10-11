"""Closed trusted migration protocol. Source and credentials never become commands."""
import dataclasses
import hashlib
import json
import re
import time
from migration_plan import alembic_plan
from migration_laravel import laravel_plan
from migration_postgres import Executor, MigrationError, validate_plan
from migration_laravel_postgres import Executor as LaravelExecutor, GRAMMAR, translated_operations

VERSION=1
GRAMMARS={'alembic':'alembic-postgresql-v1','laravel':GRAMMAR}
MAX_REQUEST=24*1024*1024
MAX_RESPONSE=4*1024*1024


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def json_value(value):return json.loads(canonical(value))


def validate_request(request):
    common={'protocol_version','operation','engine','framework','grammar','database','user','password','port','sources'}
    if not isinstance(request,dict):raise MigrationError('invalid_request')
    operation=request.get('operation');framework=request.get('framework')
    if operation not in ('inspect_plan','apply','reconcile') or framework not in GRAMMARS:raise MigrationError('invalid_request')
    if set(request)!=common|({'proof'} if operation!='inspect_plan' else set()):raise MigrationError('invalid_request')
    if type(request['protocol_version']) is not int or request['protocol_version']!=VERSION or request['engine']!='postgresql' or request['grammar']!=GRAMMARS[framework]:raise MigrationError('invalid_request')
    if type(request['port']) is not int or not 1<=request['port']<=65535:raise MigrationError('invalid_request')
    if any(not isinstance(request[k],str) or not 1<=len(request[k])<=4096 or '\0' in request[k] for k in ('database','user','password')):raise MigrationError('invalid_request')
    sources=request['sources']
    if not isinstance(sources,dict) or not 0<=len(sources)<=1000 or any(not isinstance(k,str) or not isinstance(v,str) for k,v in sources.items()):raise MigrationError('invalid_source')
    if sum(len(v.encode()) for v in sources.values())>16*1024*1024 or len(canonical(request).encode())>MAX_REQUEST:raise MigrationError('input_limit')
    return request


def rebuild(request,baseline):
    if not isinstance(baseline,dict) or set(baseline)!={'heads','ledger','hashes'}:raise MigrationError('invalid_proof')
    if request['framework']=='alembic':
        if baseline['ledger']!=[]:raise MigrationError('invalid_proof')
        return alembic_plan(request['sources'],baseline['heads'],baseline['hashes'])
    if baseline['heads']!=[]:raise MigrationError('invalid_proof')
    return laravel_plan(request['sources'],baseline['ledger'],baseline['hashes'])


def target(plan):
    if plan.framework=='alembic':return {'heads':list(plan.target_heads),'ledger':[],'hashes':dict(plan.source_hashes)}
    ledger=dict(plan.applied_batches);ledger.update({r[0]:plan.next_batch for r in plan.migrations})
    return {'heads':[],'ledger':[{'migration':n,'batch':b} for n,b in sorted(ledger.items())],'hashes':dict(plan.source_hashes)}


def tables(plan):return sorted({op[2] for record in plan.migrations for op in record[6]})


def read_ledger(c,executor,framework):
    if framework=='alembic':
        exists=executor._ledger(c,'alembic_version',('version_num',))
        head=sorted(r[0] for r in executor._query(c,'SELECT version_num FROM public.alembic_version')) if exists else []
        ledger=[];name='deploy_monitor_migration_hashes'
    else:
        exists=executor._laravel_ledger(c)
        rows=executor._query(c,'SELECT migration,batch FROM public.migrations') if exists else []
        if len(dict(rows))!=len(rows):raise MigrationError('ledger_changed')
        ledger=[{'migration':n,'batch':b} for n,b in sorted(rows)];head=[];name='deploy_monitor_laravel_hashes'
    exists=executor._ledger(c,name,('revision','source_sha256','source_path','plan_digest'))
    rows=executor._query(c,'SELECT revision,source_sha256,source_path,plan_digest FROM public.'+name) if exists else []
    if len({r[0] for r in rows})!=len(rows):raise MigrationError('ledger_changed')
    return {'heads':head,'ledger':ledger,'hashes':{r[0]:r[1] for r in rows}},rows


def identity(c,executor):
    row=executor._query(c,'SELECT current_database(),current_user,(SELECT oid::text FROM pg_catalog.pg_database WHERE datname=current_database())')[0]
    if tuple(row[:2])!=(executor.database,executor.user):raise MigrationError('identity_changed')
    return {'database':row[0],'user':row[1],'database_oid':row[2],'cluster_identity_verified':False}


def schema_state(c,executor,names):
    result={}
    for name in names:
        from migration_postgres import identifier
        before=executor._relation(c,name)
        if not before:result[name]=None;continue
        executor._ordinary(before)
        c.execute('LOCK TABLE ONLY public.'+identifier(name)+' IN ACCESS EXCLUSIVE MODE')
        row=executor._relation(c,name);executor._ordinary(row)
        if row[0]!=before[0]:raise MigrationError('schema_changed')
        indexes=executor._query(c,"SELECT x.relname,pg_catalog.pg_get_indexdef(i.indexrelid) FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class x ON x.oid=i.indexrelid WHERE i.indrelid=%s ORDER BY x.relname",(row[0],))
        result[name]=json_value({'relation':row,'columns':executor._columns(c,row[0]),'indexes_digest':digest(indexes)})
    return result


def start(conn,c,executor):
    conn.autocommit=True
    c.execute('BEGIN ISOLATION LEVEL READ COMMITTED')
    c.execute('SET LOCAL search_path=pg_catalog')
    c.execute("SET LOCAL statement_timeout='5000ms'")
    c.execute("SET LOCAL lock_timeout='1000ms'")
    c.execute("SET LOCAL idle_in_transaction_session_timeout='15000ms'")
    if not executor._query(c,'SELECT pg_catalog.pg_try_advisory_xact_lock(%s)',(638805667313650798,))[0][0]:raise MigrationError('migration_busy')


def execution_digest(plan,grammar):
    return hashlib.sha256(json.dumps((plan.digest,grammar),separators=(',',':')).encode()).hexdigest() if plan.framework=='laravel' else plan.digest


def make_proof(request,plan,baseline,observed,schema):
    value={'version':1,'framework':request['framework'],'grammar':request['grammar'],
        'identity':observed,'baseline':baseline,'target':target(plan),'plan':json_value(dataclasses.asdict(plan)),
        'baseline_schema':schema,'execution_digest':execution_digest(plan,request['grammar'])}
    value['digest']=digest(value)
    if len(canonical(value).encode())>MAX_RESPONSE-1024:raise MigrationError('proof_limit')
    return value


def check_proof(request):
    proof=request['proof']
    fields={'version','framework','grammar','identity','baseline','target','plan','baseline_schema','execution_digest','digest'}
    if not isinstance(proof,dict) or set(proof)!=fields or proof['version']!=1 or type(proof['version']) is not int:raise MigrationError('invalid_proof')
    if proof['digest']!=digest({k:v for k,v in proof.items() if k!='digest'}):raise MigrationError('invalid_proof')
    plan=rebuild(request,proof['baseline'])
    if proof!=make_proof(request,plan,proof['baseline'],proof['identity'],proof['baseline_schema']):raise MigrationError('invalid_proof')
    if set(proof['baseline_schema'])!=set(tables(plan)):raise MigrationError('invalid_proof')
    return plan,proof


def verify_postconditions(c,executor,plan):
    # Observation only: never invoke Executor._operation, which issues DDL.
    for record in plan.migrations:
        operations=translated_operations(record) if plan.framework=='laravel' else [(op,None) for op in record[6]]
        for operation,auto in operations:
            kind,_,name=operation[:3];row=executor._relation(c,name);executor._ordinary(row)
            if kind in ('create_table','add_column'):
                columns=operation[3] if kind=='create_table' else [operation[3]]
                primary=[v[0] for v in columns if v[3]]
                for col in columns:
                    increment=col[3] and len(primary)==1 and col[1][0] in ('integer','bigint','smallint') if auto is None else col[0] in auto
                    executor._check_column(c,row[0],col,auto=increment)
                if kind=='create_table':
                    keys=executor._query(c,"SELECT array_agg(a.attname ORDER BY u.position) FROM pg_catalog.pg_constraint k CROSS JOIN LATERAL unnest(k.conkey) WITH ORDINALITY u(number,position) JOIN pg_catalog.pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=u.number WHERE k.conrelid=%s AND k.contype='p' GROUP BY k.oid",(row[0],))
                    if keys!=([(primary,)] if primary else []):raise MigrationError('schema_verification_failed')
            elif kind=='create_index':
                values=executor._query(c,"SELECT i.indisvalid,i.indisready,i.indisunique,i.indisprimary,i.indexprs IS NULL,i.indpred IS NULL,am.amname,(SELECT array_agg(a.attname ORDER BY u.position) FROM unnest(i.indkey) WITH ORDINALITY u(number,position) JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=u.number) FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class x ON x.oid=i.indexrelid JOIN pg_catalog.pg_namespace n ON n.oid=x.relnamespace JOIN pg_catalog.pg_am am ON am.oid=x.relam WHERE i.indrelid=%s AND n.nspname='public' AND x.relname=%s",(row[0],operation[3]))
                if len(values)!=1 or tuple(values[0][:7])!=(True,True,False,False,True,True,'btree') or list(values[0][7])!=list(operation[4]):raise MigrationError('schema_verification_failed')


def _dispatch(request,*,connect,authorize):
    validate_request(request)
    if not callable(authorize):raise MigrationError('authority_required')
    factory=LaravelExecutor if request['framework']=='laravel' else Executor
    options={'database':request['database'],'user':request['user']}
    if request['framework']=='laravel':options['grammar']=request['grammar']
    executor=factory(connect,**options)
    operation=request['operation'];authorize()
    if operation=='apply':
        plan,proof=check_proof(request);active=[];checked=False
        if not plan.migrations:
            return _dispatch(dict(request,operation='reconcile'),connect=connect,authorize=authorize)
        def connection():
            conn=connect();active.append(conn);return conn
        def guard():
            nonlocal checked
            authorize()
            if active and not checked:
                with active[0].cursor() as c:
                    current,_=read_ledger(c,executor,request['framework'])
                    if identity(c,executor)!=proof['identity'] or current!=proof['baseline'] or schema_state(c,executor,tables(plan))!=proof['baseline_schema']:raise MigrationError('proof_stale')
                checked=True
        result=factory(connection,**options).apply(plan,sources=request['sources'],authorize=guard)
        authorize()
        return {'status':'committed','proof_digest':proof['digest'],'evidence':result}
    conn=None
    try:
        conn=connect();conn.autocommit=True
        with conn.cursor() as c:
            start(conn,c,executor);observed=identity(c,executor);baseline,hash_rows=read_ledger(c,executor,request['framework']);authorize()
            if operation=='inspect_plan':
                plan=rebuild(request,baseline)
                # Compile before publishing an executable proof.
                if request['framework']=='laravel':
                    from migration_laravel_postgres import validate_plan as validate_laravel
                    validate_laravel(plan,request['sources'],baseline['ledger'],baseline['hashes'])
                else:validate_plan(plan,request['sources'],baseline['heads'],baseline['hashes'])
                proof=make_proof(request,plan,baseline,observed,schema_state(c,executor,tables(plan)))
                authorize();return {'status':'planned','proof':proof,'observed_at':time.time()}
            plan,proof=check_proof(request)
            if observed!=proof['identity']:raise MigrationError('identity_changed')
            state=schema_state(c,executor,tables(plan))
            if baseline==proof['baseline'] and state==proof['baseline_schema']:
                status='committed' if not plan.migrations else 'not_applied'
            elif baseline==proof['target']:
                by_revision={r[0]:r for r in hash_rows}
                for record in plan.migrations:
                    if by_revision.get(record[0])!=(record[0],record[5],record[4],proof['execution_digest']):raise MigrationError('commit_evidence_changed')
                verify_postconditions(c,executor,plan);status='committed'
            else:status='reconciliation_required'
            authorize();return {'status':status,'proof_digest':proof['digest'],'no_op':not bool(plan.migrations),'observed_at':time.time()}
    finally:
        if conn is not None:
            try:conn.rollback()
            except Exception:pass
            conn.close()


def dispatch(request,*,connect,authorize):
    from migration_plan import PLANNING_DEADLINE,planning_remaining
    if not callable(authorize):raise MigrationError('authority_required')
    token=PLANNING_DEADLINE.set(time.monotonic()+90)
    def guard():
        planning_remaining();authorize();planning_remaining()
    try:return _dispatch(request,connect=connect,authorize=guard)
    finally:PLANNING_DEADLINE.reset(token)
