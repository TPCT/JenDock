"""Laravel12 stock PostgreSQL grammar over parsed immutable operations only.

No PHP application bootstrapping. Stock defaults: string length255, timeprecision0,
no table/index prefix, no custom Blueprint macros. unsigned has no PostgreSQL
constraint in Laravel's grammar. Grammar identity is mandatory and contributes to
execution_digest; future job targets must pin it alongside plan/source hashes.
An existing Laravel ledger without trusted historical hashes is not baselined.
"""
import hashlib
import json
import time
from migration_laravel import LaravelPlan, laravel_plan
from migration_plan import PlanError
from migration_postgres import Executor as BaseExecutor, MigrationError, compile_operation

GRAMMAR='laravel12-stock-postgresql-v1'
HASH_LEDGER='deploy_monitor_laravel_hashes'


def translated_operations(record):
    if type(record) is not tuple or len(record)!=8:raise MigrationError('invalid_plan')
    semantics=iter(record[7]);result=[]
    for operation in record[6]:
        kind,schema,table=operation[:3]
        if kind=='create_index':
            compile_operation(operation,autoincrement_columns=())
            result.append((operation,()));continue
        columns=operation[3] if kind=='create_table' else (operation[3],)
        translated=[];auto=[]
        for column in columns:
            meta=next(semantics,None)
            if type(meta) is not tuple or len(meta)!=7 or meta[1:4]!=(schema,table,column[0]):raise MigrationError('invalid_semantics')
            ordinal,_,_,name,source_type,increment,unsigned=meta
            if type(ordinal) is not int or ordinal<0 or type(increment) is not bool or type(unsigned) is not bool:raise MigrationError('invalid_semantics')
            expected={'id':('bigint',),'increments':('integer',),'bigIncrements':('bigint',),'integer':('integer',),'bigInteger':('bigint',),'smallInteger':('smallint',),'boolean':('boolean',),'text':('text',),'date':('date',),'dateTime':('timestamp',),'timestamp':('timestamp',)}
            actual_type=column[1]
            if source_type=='string':
                if len(actual_type)!=2 or actual_type[0]!='varchar':raise MigrationError('invalid_semantics')
            elif expected.get(source_type)!=actual_type:raise MigrationError('invalid_semantics')
            identity=source_type in ('id','increments','bigIncrements')
            if increment!=identity or unsigned!=identity or (identity and not column[3]):raise MigrationError('invalid_semantics')
            if increment:auto.append(name)
            datatype=('timestamp',0) if source_type in ('dateTime','timestamp') else actual_type
            translated.append((name,datatype,column[2],column[3]))
        mapped=(kind,schema,table,tuple(translated)) if kind=='create_table' else (kind,schema,table,translated[0])
        compile_operation(mapped,autoincrement_columns=tuple(auto))
        result.append((mapped,tuple(auto)))
    if next(semantics,None) is not None:raise MigrationError('invalid_semantics')
    return tuple(result)


def validate_plan(plan,sources,ledger,hashes):
    if type(plan) is not LaravelPlan or plan.framework!='laravel':raise MigrationError('invalid_plan')
    try:rebuilt=laravel_plan(sources,ledger,hashes)
    except (PlanError,ValueError,TypeError):raise MigrationError('source_or_ledger_changed') from None
    if rebuilt!=plan:raise MigrationError('source_or_ledger_changed')
    if sum(len(r[6]) for r in plan.migrations)>4096:raise MigrationError('operation_limit')
    for record in plan.migrations:translated_operations(record)
    return rebuilt


class Executor(BaseExecutor):
    def __init__(self,connect,*,database,user,grammar):
        if grammar!=GRAMMAR:raise MigrationError('unsupported_grammar')
        super().__init__(connect,database=database,user=user)
        self.grammar=grammar

    def _laravel_ledger(self,c):
        before=self._relation(c,'migrations')
        if not before:return False
        c.execute('LOCK TABLE ONLY "public"."migrations" IN SHARE ROW EXCLUSIVE MODE')
        row=self._relation(c,'migrations');self._ordinary(row)
        if row[0]!=before[0]:raise MigrationError('ledger_changed')
        expected=(('id',('integer',),False,True),('migration',('varchar',255),False,False),('batch',('integer',),False,False))
        if [a[0] for a in self._columns(c,row[0])]!=[v[0] for v in expected]:raise MigrationError('unsupported_ledger')
        for value in expected:self._check_column(c,row[0],value,auto=value[0]=='id')
        if self._query(c,"SELECT 1 FROM pg_catalog.pg_constraint WHERE confrelid=%s AND contype='f' LIMIT 1",(row[0],)):raise MigrationError('unsupported_ledger')
        if self._query(c,'SELECT contype,convalidated FROM pg_catalog.pg_constraint WHERE conrelid=%s',(row[0],))!=[('p',True)]:raise MigrationError('unsupported_ledger')
        indexes=self._query(c,"SELECT i.indisprimary,i.indisunique,i.indisvalid,i.indisready,i.indnkeyatts,i.indnatts,i.indexprs IS NULL,i.indpred IS NULL,am.amname,a.attname,ons.nspname,oc.opcname FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class x ON x.oid=i.indexrelid JOIN pg_catalog.pg_am am ON am.oid=x.relam JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=i.indkey[0] JOIN pg_catalog.pg_opclass oc ON oc.oid=i.indclass[0] JOIN pg_catalog.pg_namespace ons ON ons.oid=oc.opcnamespace WHERE i.indrelid=%s",(row[0],))
        if indexes!=[(True,True,True,True,1,1,True,True,'btree','id','pg_catalog','int4_ops')]:raise MigrationError('unsupported_ledger')
        return True

    def apply(self,plan,*,sources,authorize):
        if type(plan) is not LaravelPlan:raise MigrationError('invalid_plan')
        if not callable(authorize):raise MigrationError('authority_required')
        applied=dict(plan.applied_batches)
        validate_plan(plan,sources,[{'migration':n,'batch':b} for n,b in plan.applied_batches],{n:h for n,h in plan.source_hashes if n in applied})
        execution_digest=hashlib.sha256(json.dumps((plan.digest,self.grammar),separators=(',',':')).encode()).hexdigest()
        authorize();conn=None;committing=False;deadline=time.monotonic()+60
        try:
            conn=self.connect();conn.autocommit=True
            with conn.cursor() as c:
                c.execute('BEGIN ISOLATION LEVEL READ COMMITTED')
                c.execute('SET LOCAL search_path=pg_catalog')
                c.execute("SET LOCAL statement_timeout='10000ms'")
                c.execute("SET LOCAL lock_timeout='1000ms'")
                c.execute("SET LOCAL idle_in_transaction_session_timeout='15000ms'")
                if tuple(self._query(c,'SELECT current_database(),current_user')[0])!=(self.database,self.user):raise MigrationError('identity_changed')
                if not self._query(c,'SELECT pg_catalog.pg_try_advisory_xact_lock(%s)',(638805667313650798,))[0][0]:raise MigrationError('migration_busy')
                if self._query(c,"SELECT 1 FROM pg_catalog.pg_event_trigger WHERE evtenabled<>'D' LIMIT 1"):raise MigrationError('unsupported_event_triggers')
                authorize()
                ledger_exists=self._laravel_ledger(c)
                hashes_exist=self._ledger(c,HASH_LEDGER,('revision','source_sha256','source_path','plan_digest'))
                rows=self._query(c,'SELECT migration,batch FROM public.migrations') if ledger_exists else []
                hashes=self._query(c,'SELECT revision,source_sha256 FROM public.deploy_monitor_laravel_hashes') if hashes_exist else []
                if len(dict(hashes))!=len(hashes):raise MigrationError('ledger_changed')
                validate_plan(plan,sources,[{'migration':n,'batch':b} for n,b in rows],dict(hashes))
                if not ledger_exists:c.execute('CREATE TABLE public.migrations(id SERIAL PRIMARY KEY,migration varchar(255) NOT NULL,batch integer NOT NULL)')
                if not hashes_exist:c.execute('CREATE TABLE public.deploy_monitor_laravel_hashes(revision varchar(255) PRIMARY KEY,source_sha256 varchar(64) NOT NULL,source_path text NOT NULL,plan_digest varchar(64) NOT NULL)')
                def guard():
                    remaining=int((deadline-time.monotonic())*1000)
                    if remaining<=0:raise MigrationError('timeout')
                    c.execute("SELECT pg_catalog.set_config('statement_timeout',%s,true)",(str(min(10000,remaining)),));authorize()
                evidence=[]
                for record in plan.migrations:
                    guard()
                    for operation,auto in translated_operations(record):
                        guard();evidence.append(self._operation(c,operation,autoincrement_columns=auto))
                    c.execute('INSERT INTO public.migrations(migration,batch) VALUES(%s,%s)',(record[0],plan.next_batch))
                    c.execute('INSERT INTO public.deploy_monitor_laravel_hashes(revision,source_sha256,source_path,plan_digest) VALUES(%s,%s,%s,%s)',(record[0],record[5],record[4],execution_digest))
                actual=self._query(c,'SELECT migration,batch FROM public.migrations')
                expected=dict(plan.applied_batches);expected.update({r[0]:plan.next_batch for r in plan.migrations})
                actual_hashes=self._query(c,'SELECT revision,source_sha256 FROM public.deploy_monitor_laravel_hashes')
                if len(actual)!=len(expected) or dict(actual)!=expected or len(actual_hashes)!=len(plan.source_hashes) or dict(actual_hashes)!=dict(plan.source_hashes):raise MigrationError('ledger_verification_failed')
                guard();committing=True;c.execute('COMMIT');committing=False
                return {'framework':'laravel','grammar':self.grammar,'plan_digest':plan.digest,'execution_digest':execution_digest,'applied':[r[0] for r in plan.migrations],'batch':plan.next_batch if plan.migrations else None,'operations':evidence,'transaction_committed':True,'observed_at':time.time()}
        except MigrationError:raise
        except Exception as error:
            if committing:raise MigrationError('commit_uncertain') from None
            raise MigrationError('timeout' if getattr(error,'sqlstate',None) in ('57014','55P03') else 'execution_failed') from None
        finally:
            if conn is not None:
                try:
                    with conn.cursor() as cleanup:cleanup.execute('ROLLBACK')
                except Exception:pass
                try:conn.close()
                except Exception:pass
