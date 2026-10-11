"""Transactional PostgreSQL executor for the immutable declarative Alembic subset.

No migration module is imported and no candidate SQL reaches this executor. The
caller supplies a fresh bounded connector and a current-authority callback.
Schema administrators, the migration role and cluster administrators are trusted;
this is not isolation from a malicious administrator or privileged startup code.
Existing Alembic history requires an already trusted source-hash ledger; this
module never silently baselines an existing database. Integration/runtime proof
is separate from the pure compiler tests.
"""
import hashlib
import json
import re
import time
from migration_plan import MigrationPlan, PlanError, alembic_plan


class MigrationError(ValueError):
    def __init__(self, code):
        self.code=code
        super().__init__('Migration could not complete: '+code+'.')


RESERVED={'alembic_version','deploy_monitor_migration_hashes','migrations','deploy_monitor_laravel_hashes'}
TYPES={'integer','bigint','smallint','boolean','text','date','timestamp'}
OIDS={'integer':23,'bigint':20,'smallint':21,'boolean':16,'text':25,'date':1082,'timestamp':1114,'varchar':1043}


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}',value):
        raise MigrationError('invalid_operation')
    return '"'+value+'"'


def column(value, *, additive=False):
    if type(value) is not tuple or len(value)!=4:raise MigrationError('invalid_operation')
    name,kind,nullable,primary=value
    identifier(name)
    if type(nullable) is not bool or type(primary) is not bool or (primary and nullable):raise MigrationError('invalid_operation')
    if additive and (not nullable or primary):raise MigrationError('invalid_operation')
    if type(kind) is not tuple or not kind:raise MigrationError('invalid_operation')
    if kind[0]=='varchar':
        if len(kind)!=2 or type(kind[1]) is not int or not 1<=kind[1]<=1048576:raise MigrationError('invalid_operation')
        sqltype='varchar('+str(kind[1])+')'
    elif kind[0]=='timestamp' and len(kind)==2:
        if type(kind[1]) is not int or not 0<=kind[1]<=6:raise MigrationError('invalid_operation')
        sqltype='timestamp('+str(kind[1])+')'
    elif len(kind)==1 and kind[0] in TYPES:sqltype=kind[0]
    else:raise MigrationError('invalid_operation')
    return name,kind,nullable,primary,sqltype


def compile_operation(operation,*,autoincrement_columns=None):
    if type(operation) is not tuple or len(operation)<4:raise MigrationError('invalid_operation')
    action,schema,table=operation[:3]
    if schema!='public' or table in RESERVED:raise MigrationError('unsupported_schema')
    target='"public".'+identifier(table)
    if action=='create_table' and len(operation)==4:
        columns=operation[3]
        if type(columns) is not tuple or not 1<=len(columns)<=256:raise MigrationError('invalid_operation')
        parsed=[column(c) for c in columns]
        if len({c[0] for c in parsed})!=len(parsed):raise MigrationError('invalid_operation')
        primary=[c[0] for c in parsed if c[3]]
        eligible={c[0] for c in parsed if c[3] and len(primary)==1 and c[1][0] in ('integer','bigint','smallint')}
        if autoincrement_columns is not None and (type(autoincrement_columns) is not tuple or any(not isinstance(n,str) for n in autoincrement_columns) or len(set(autoincrement_columns))!=len(autoincrement_columns) or not set(autoincrement_columns)<=eligible):raise MigrationError('invalid_operation')
        auto=eligible if autoincrement_columns is None else set(autoincrement_columns)
        parts=[]
        for name,kind,nullable,pk,sqltype in parsed:
            # SQLAlchemy PostgreSQL's implicit autoincrement applies only to a
            # single integer primary key, not every member of a composite key.
            if name in auto:
                sqltype={'integer':'SERIAL','bigint':'BIGSERIAL','smallint':'SMALLSERIAL'}[kind[0]]
            parts.append(identifier(name)+' '+sqltype+(' NOT NULL' if not nullable else ''))
        if primary:parts.append('PRIMARY KEY ('+','.join(map(identifier,primary))+')')
        return 'CREATE TABLE '+target+' ('+','.join(parts)+')'
    if autoincrement_columns not in (None,()):raise MigrationError('invalid_operation')
    if action=='add_column' and len(operation)==4:
        name,kind,nullable,pk,sqltype=column(operation[3],additive=True)
        return 'ALTER TABLE '+target+' ADD COLUMN '+identifier(name)+' '+sqltype
    if action=='create_index' and len(operation)==5:
        index,columns=operation[3:]
        if index in RESERVED:raise MigrationError('invalid_operation')
        if type(columns) is not tuple or not 1<=len(columns)<=32 or any(not isinstance(n,str) for n in columns) or len(set(columns))!=len(columns):raise MigrationError('invalid_operation')
        return 'CREATE INDEX '+identifier(index)+' ON '+target+' USING btree ('+','.join(map(identifier,columns))+')'
    raise MigrationError('invalid_operation')


def validate_plan(plan,sources,current_heads,applied_hashes):
    if type(plan) is not MigrationPlan or plan.framework!='alembic':raise MigrationError('invalid_plan')
    try:rebuilt=alembic_plan(sources,current_heads=current_heads,applied_hashes=applied_hashes)
    except (PlanError,TypeError,ValueError):raise MigrationError('source_or_ledger_changed') from None
    if rebuilt!=plan:raise MigrationError('source_or_ledger_changed')
    if sum(len(m[6]) for m in plan.migrations)>4096:raise MigrationError('operation_limit')
    for migration in plan.migrations:
        for operation in migration[6]:compile_operation(operation)
    return rebuilt


class Executor:
    def __init__(self,connect,*,database,user):
        if any(not isinstance(v,str) or not v or len(v)>128 or '\0' in v for v in (database,user)):
            raise MigrationError('invalid_identity')
        self.connect=connect;self.database=database;self.user=user

    def _query(self,c,sql,args=(),limit=1001):
        c.execute(sql,args);rows=c.fetchmany(limit)
        if len(rows)>=limit:raise MigrationError('catalog_limit')
        return rows

    def _relation(self,c,table):
        rows=self._query(c,"SELECT r.oid,r.relkind,r.relrowsecurity,r.relhasrules,r.relhassubclass,r.relam,EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid=r.oid AND NOT t.tgisinternal),EXISTS(SELECT 1 FROM pg_catalog.pg_inherits i WHERE i.inhrelid=r.oid) FROM pg_catalog.pg_class r JOIN pg_catalog.pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='public' AND r.relname=%s",(table,))
        if len(rows)>1:raise MigrationError('schema_changed')
        return rows[0] if rows else None

    def _ordinary(self,row):
        if not row or row[1]!='r' or row[2] or row[3] or row[4] or row[5]!=2 or row[6] or row[7]:raise MigrationError('unsupported_relation')

    def _columns(self,c,oid):
        return self._query(c,"SELECT a.attname,a.atttypid,a.atttypmod,a.attnotnull,a.attgenerated,a.attidentity,a.atthasdef,a.attnum FROM pg_catalog.pg_attribute a WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum",(oid,))

    def _ledger(self,c,table,expected):
        row=self._relation(c,table)
        if not row:return False
        # Acquire before selecting user-visible ledger data, then refresh catalog.
        c.execute('LOCK TABLE ONLY "public".'+identifier(table)+' IN SHARE ROW EXCLUSIVE MODE')
        current=self._relation(c,table);self._ordinary(current)
        if current[0]!=row[0]:raise MigrationError('ledger_changed')
        columns=self._columns(c,current[0])
        if [r[0] for r in columns]!=list(expected) or any(r[1] not in (25,1043) or not r[3] or r[4] or r[5] or r[6] for r in columns):raise MigrationError('unsupported_ledger')
        # Referencing tables can attach internal cascade triggers to this ledger.
        # The retained table lock also fences concurrent FK installation.
        if self._query(c,"SELECT 1 FROM pg_catalog.pg_constraint WHERE confrelid=%s AND contype='f' LIMIT 1",(current[0],)):
            raise MigrationError('unsupported_ledger')
        constraints=self._query(c,"SELECT contype,convalidated FROM pg_catalog.pg_constraint WHERE conrelid=%s",(current[0],))
        if constraints!=[('p',True)]:raise MigrationError('unsupported_ledger')
        indexes=self._query(c,"SELECT i.indisprimary,i.indisunique,i.indisvalid,i.indisready,i.indnkeyatts,i.indnatts,i.indexprs IS NULL,i.indpred IS NULL,am.amname,a.attname,ons.nspname,oc.opcname FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class x ON x.oid=i.indexrelid JOIN pg_catalog.pg_am am ON am.oid=x.relam JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=i.indkey[0] JOIN pg_catalog.pg_opclass oc ON oc.oid=i.indclass[0] JOIN pg_catalog.pg_namespace ons ON ons.oid=oc.opcnamespace WHERE i.indrelid=%s",(current[0],))
        if len(indexes)!=1 or tuple(indexes[0][:10])!=(True,True,True,True,1,1,True,True,'btree',expected[0]) or indexes[0][10]!='pg_catalog' or indexes[0][11] not in ('text_ops','varchar_ops'):
            raise MigrationError('unsupported_ledger')
        return True

    def _check_column(self,c,oid,expected,auto=False):
        name,kind,nullable,pk,_=column(expected)
        rows=[r for r in self._columns(c,oid) if r[0]==name]
        if len(rows)!=1:raise MigrationError('schema_verification_failed')
        actual=rows[0]
        modifier=kind[1]+4 if kind[0]=='varchar' else kind[1] if kind[0]=='timestamp' and len(kind)==2 else -1
        if actual[1]!=OIDS[kind[0]] or actual[2]!=modifier or actual[3]!=(not nullable) or actual[4] or actual[5] or bool(actual[6])!=auto:
            raise MigrationError('schema_verification_failed')
        if auto:
            owned=self._query(c,"SELECT s.oid FROM pg_catalog.pg_depend d JOIN pg_catalog.pg_class s ON s.oid=d.objid WHERE d.classid='pg_catalog.pg_class'::regclass AND d.refclassid='pg_catalog.pg_class'::regclass AND d.refobjid=%s AND d.refobjsubid=%s AND d.deptype='a' AND s.relkind='S'",(oid,actual[7]))
            if len(owned)!=1:raise MigrationError('schema_verification_failed')
            exact_default=self._query(c,"SELECT 1 FROM pg_catalog.pg_attrdef a WHERE a.adrelid=%s AND a.adnum=%s AND pg_catalog.pg_get_expr(a.adbin,a.adrelid)=pg_catalog.format('nextval(%%L::regclass)',%s::oid::regclass::text) AND EXISTS(SELECT 1 FROM pg_catalog.pg_depend d WHERE d.classid='pg_catalog.pg_attrdef'::regclass AND d.objid=a.oid AND d.refclassid='pg_catalog.pg_class'::regclass AND d.refobjid=%s AND d.deptype='n')",(oid,actual[7],owned[0][0],owned[0][0]))
            if exact_default!=[(1,)]:raise MigrationError('schema_verification_failed')

    def _operation(self,c,operation,*,autoincrement_columns=None):
        sql=compile_operation(operation,autoincrement_columns=autoincrement_columns);action,_,table=operation[:3]
        prior=self._relation(c,table)
        if action=='create_table':
            if prior:raise MigrationError('schema_changed')
        else:
            self._ordinary(prior)
            c.execute('LOCK TABLE ONLY "public".'+identifier(table)+' IN ACCESS EXCLUSIVE MODE')
            current=self._relation(c,table);self._ordinary(current)
            if current[0]!=prior[0]:raise MigrationError('schema_changed')
            if action=='create_index':
                columns={r[0]:r for r in self._columns(c,current[0])}
                if any(n not in columns or columns[n][1] not in OIDS.values() or columns[n][4] or columns[n][5] for n in operation[4]):raise MigrationError('unsupported_index_columns')
        c.execute(sql)
        observed=self._relation(c,table);self._ordinary(observed)
        if action=='create_table':
            expected=operation[3];primary=[v[0] for v in expected if v[3]]
            if [r[0] for r in self._columns(c,observed[0])]!=[v[0] for v in expected]:raise MigrationError('schema_verification_failed')
            for value in expected:self._check_column(c,observed[0],value,auto=(value[3] and len(primary)==1 and value[1][0] in ('integer','bigint','smallint')) if autoincrement_columns is None else value[0] in autoincrement_columns)
            keys=self._query(c,"SELECT array_agg(a.attname ORDER BY u.position) FROM pg_catalog.pg_constraint k CROSS JOIN LATERAL unnest(k.conkey) WITH ORDINALITY u(number,position) JOIN pg_catalog.pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=u.number WHERE k.conrelid=%s AND k.contype='p' GROUP BY k.oid",(observed[0],))
            if keys!=([(primary,)] if primary else []):raise MigrationError('schema_verification_failed')
        elif action=='add_column':self._check_column(c,observed[0],operation[3])
        else:
            indexes=self._query(c,"SELECT i.indisvalid,i.indisready,i.indisunique,i.indisprimary,i.indexprs IS NULL,i.indpred IS NULL,am.amname,(SELECT array_agg(a.attname ORDER BY u.position) FROM unnest(i.indkey) WITH ORDINALITY u(number,position) JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=u.number) FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class x ON x.oid=i.indexrelid JOIN pg_catalog.pg_namespace n ON n.oid=x.relnamespace JOIN pg_catalog.pg_am am ON am.oid=x.relam WHERE i.indrelid=%s AND n.nspname='public' AND x.relname=%s",(observed[0],operation[3]))
            if len(indexes)!=1 or tuple(indexes[0][:7])!=(True,True,False,False,True,True,'btree') or list(indexes[0][7])!=list(operation[4]):raise MigrationError('schema_verification_failed')
        return {'operation':action,'schema':'public','table':table,'verified':True}

    def apply(self,plan,*,sources,authorize):
        if not callable(authorize):raise MigrationError('authority_required')
        # Validate complete source/operation shape before even obtaining credentials.
        if type(plan) is not MigrationPlan:raise MigrationError('invalid_plan')
        try:
            source_hashes=dict(plan.source_hashes)
            pending={m[0] for m in plan.migrations}
        except (TypeError,ValueError,IndexError):raise MigrationError('invalid_plan') from None
        validate_plan(plan,sources,plan.current_heads,{r:h for r,h in source_hashes.items() if r not in pending})
        authorize();conn=None;committing=False;deadline=time.monotonic()+60
        try:
            conn=self.connect();conn.autocommit=True
            with conn.cursor() as c:
                c.execute('BEGIN ISOLATION LEVEL READ COMMITTED')
                c.execute("SET LOCAL search_path=pg_catalog")
                c.execute("SET LOCAL statement_timeout='10000ms'")
                c.execute("SET LOCAL lock_timeout='1000ms'")
                c.execute("SET LOCAL idle_in_transaction_session_timeout='15000ms'")
                identity=self._query(c,'SELECT current_database(),current_user')[0]
                if tuple(identity)!=(self.database,self.user):raise MigrationError('identity_changed')
                if not self._query(c,'SELECT pg_catalog.pg_try_advisory_xact_lock(%s)',(638805667313650798,))[0][0]:raise MigrationError('migration_busy')
                if self._query(c,"SELECT 1 FROM pg_catalog.pg_event_trigger WHERE evtenabled<>'D' LIMIT 1"):raise MigrationError('unsupported_event_triggers')
                authorize()
                heads_exist=self._ledger(c,'alembic_version',('version_num',))
                hashes_exist=self._ledger(c,'deploy_monitor_migration_hashes',('revision','source_sha256','source_path','plan_digest'))
                heads=tuple(sorted(r[0] for r in self._query(c,'SELECT version_num FROM "public"."alembic_version"'))) if heads_exist else ()
                hash_rows=self._query(c,'SELECT revision,source_sha256 FROM "public"."deploy_monitor_migration_hashes"') if hashes_exist else []
                if len({r[0] for r in hash_rows})!=len(hash_rows):raise MigrationError('ledger_changed')
                validate_plan(plan,sources,heads,dict(hash_rows))
                if not heads_exist:c.execute('CREATE TABLE "public"."alembic_version" (version_num varchar(64) PRIMARY KEY)')
                if not hashes_exist:c.execute('CREATE TABLE "public"."deploy_monitor_migration_hashes" (revision varchar(64) PRIMARY KEY,source_sha256 varchar(64) NOT NULL,source_path text NOT NULL,plan_digest varchar(64) NOT NULL)')
                def guard():
                    remaining=int((deadline-time.monotonic())*1000)
                    if remaining<=0:raise MigrationError('timeout')
                    c.execute("SELECT pg_catalog.set_config('statement_timeout',%s,true)",(str(min(10000,remaining)),))
                    authorize()
                evidence=[]
                for migration in plan.migrations:
                    guard()
                    for operation in migration[6]:
                        guard();evidence.append(self._operation(c,operation))
                    c.execute('INSERT INTO "public"."deploy_monitor_migration_hashes" (revision,source_sha256,source_path,plan_digest) VALUES (%s,%s,%s,%s)',(migration[0],migration[5],migration[4],plan.digest))
                c.execute('DELETE FROM "public"."alembic_version"')
                for head in plan.target_heads:c.execute('INSERT INTO "public"."alembic_version" (version_num) VALUES (%s)',(head,))
                actual=tuple(sorted(r[0] for r in self._query(c,'SELECT version_num FROM "public"."alembic_version"')))
                actual_hashes=self._query(c,'SELECT revision,source_sha256 FROM "public"."deploy_monitor_migration_hashes"')
                if actual!=plan.target_heads or len(actual_hashes)!=len(plan.source_hashes) or dict(actual_hashes)!=dict(plan.source_hashes):raise MigrationError('ledger_verification_failed')
                guard();committing=True
                c.execute('COMMIT')
                committing=False
                return {'framework':'alembic','database':self.database,'plan_digest':plan.digest,'heads':list(actual),'applied':[m[0] for m in plan.migrations],'operations':evidence,'transaction_committed':True,'observed_at':time.time()}
        except MigrationError:raise
        except Exception as error:
            if committing:raise MigrationError('commit_uncertain') from None
            code=getattr(error,'sqlstate',None)
            raise MigrationError('timeout' if code in ('57014','55P03') else 'execution_failed') from None
        finally:
            if conn is not None:
                try:
                    with conn.cursor() as cleanup:cleanup.execute('ROLLBACK')
                except Exception:pass
                try:conn.close()
                except Exception:pass
