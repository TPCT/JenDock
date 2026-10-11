"""Fixed PostgreSQL catalog and bounded row inspection. No arbitrary SQL API."""
import hashlib
import json
import time
from cryptography.fernet import Fernet, InvalidToken

class DatabaseError(ValueError):
    def __init__(self, code):
        self.code=code
        super().__init__('Database inspection could not complete: '+code+'.')

SCALARS={16,20,21,23,25,700,701,1042,1043,1082,1083,1114,1184,1186,1266,2950}

class Inspector:
    def __init__(self,connect,*,database,user,schemas,row_columns=None,cursor_key,cursor_context):
        self.connect=connect;self.database=database;self.user=user
        if not isinstance(schemas,list) or not 1<=len(schemas)<=100 or any(not isinstance(s,str) or not s or len(s)>128 or s.startswith('pg_') or s=='information_schema' for s in schemas):raise DatabaseError('invalid_policy')
        self.schemas=schemas;self.columns=row_columns or {};self.cipher=Fernet(cursor_key)
        self.context=hashlib.sha256(json.dumps(cursor_context,sort_keys=True,allow_nan=False).encode()).hexdigest()

    def _query(self,c,sql,args=(),limit=1001):
        c.execute(sql,args);rows=c.fetchmany(limit)
        if len(rows)==limit:raise DatabaseError('result_limit')
        return rows

    def _cursor(self,token,shape):
        if token is None:return None
        try:
            if not isinstance(token,str) or len(token)>8192:raise ValueError()
            value=json.loads(self.cipher.decrypt(token.encode(),ttl=900))
            if value['context']!=self.context or value['shape']!=shape:raise ValueError()
            return value['after']
        except (ValueError,KeyError,TypeError,InvalidToken):raise DatabaseError('cursor_stale') from None

    def _next(self,after,shape):
        return self.cipher.encrypt(json.dumps({'context':self.context,'shape':shape,'after':after}).encode()).decode()

    def inspect(self,request):
        from psycopg import sql
        if not isinstance(request,dict) or request.get('action') not in ('schemas','tables','table','rows'):raise DatabaseError('invalid_request')
        action=request['action'];allowed={'action','limit','cursor'}|({'schema'} if action!='schemas' else set())|({'table'} if action in ('table','rows') else set())
        if set(request)-allowed:raise DatabaseError('invalid_request')
        limit=request.get('limit',25)
        if type(limit) is not int or not 1<=limit<=100:raise DatabaseError('invalid_request')
        schema=request.get('schema');table=request.get('table')
        if action!='schemas' and schema not in self.schemas:raise DatabaseError('permission_denied')
        if action in ('table','rows') and (not isinstance(table,str) or not table or len(table)>128):raise DatabaseError('invalid_request')
        conn=None
        try:
            conn=self.connect();conn.autocommit=True
            with conn.cursor() as c:
                c.execute('BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY')
                c.execute("SET LOCAL statement_timeout='3000ms'")
                c.execute("SET LOCAL lock_timeout='500ms'")
                c.execute("SET LOCAL search_path=pg_catalog")
                identity=self._query(c,"SELECT current_database(),current_user,(SELECT oid::text FROM pg_database WHERE datname=current_database()),current_setting('server_version_num')")[0]
                if identity[0]!=self.database or identity[1]!=self.user:raise DatabaseError('identity_changed')
                powerful=self._query(c,"SELECT 1 FROM pg_roles WHERE pg_has_role(oid,'MEMBER') AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls OR rolname IN ('pg_write_all_data','pg_execute_server_program','pg_write_server_files','pg_read_server_files')) LIMIT 1")
                if powerful or self._query(c,"SELECT 1 WHERE has_database_privilege(current_database(),'CREATE') OR has_database_privilege(current_database(),'TEMP')"):raise DatabaseError('privileged_credential')
                if self._query(c,"SELECT 1 FROM pg_namespace WHERE has_schema_privilege(oid,'CREATE') LIMIT 1"):raise DatabaseError('privileged_credential')
                if self._query(c,"SELECT 1 FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace WHERE r.relkind IN ('r','p','v','f') AND NOT(n.nspname='pg_catalog' AND r.relname='pg_settings') AND (has_table_privilege(r.oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES') OR has_any_column_privilege(r.oid,'INSERT,UPDATE,REFERENCES')) LIMIT 1"):raise DatabaseError('privileged_credential')
                if self._query(c,"SELECT 1 FROM pg_class WHERE CASE WHEN relkind='S' THEN has_sequence_privilege(oid,'USAGE,UPDATE') ELSE false END LIMIT 1"):raise DatabaseError('privileged_credential')
                shape=[action,schema,table,limit];after=self._cursor(request.get('cursor'),shape)
                next_cursor=None
                if action=='schemas':
                    if after is not None and not isinstance(after,str):raise DatabaseError('cursor_stale')
                    rows=self._query(c,"SELECT nspname FROM pg_namespace WHERE nspname=ANY(%s) AND has_schema_privilege(oid,'USAGE') AND (%s::text IS NULL OR nspname>%s) ORDER BY nspname LIMIT %s",(self.schemas,after,after,limit+1))
                    if len(rows)>limit:next_cursor=self._next(rows[limit-1][0],shape)
                    data=[r[0] for r in rows[:limit]]
                elif action=='tables':
                    if after is not None and not isinstance(after,str):raise DatabaseError('cursor_stale')
                    rows=self._query(c,"SELECT r.relname,r.relkind FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname=%s AND has_schema_privilege(n.oid,'USAGE') AND r.relkind IN ('r','p','v','m','f') AND has_any_column_privilege(r.oid,'SELECT') AND (%s::text IS NULL OR r.relname>%s) ORDER BY r.relname LIMIT %s",(schema,after,after,limit+1))
                    if len(rows)>limit:next_cursor=self._next(rows[limit-1][0],shape)
                    data=[{'name':r[0],'kind':r[1]} for r in rows[:limit]]
                else:
                    relation=self._query(c,"SELECT r.oid,r.relkind,r.relrowsecurity,r.relhassubclass,r.relam,(SELECT EXISTS(SELECT 1 FROM pg_inherits WHERE inhrelid=r.oid)),r.relhasrules FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname=%s AND r.relname=%s AND has_schema_privilege(n.oid,'USAGE') AND has_any_column_privilege(r.oid,'SELECT')",(schema,table))
                    if len(relation)!=1:raise DatabaseError('table_unavailable')
                    rel=relation[0];oid=rel[0]
                    if action=='rows':
                        # SQL relation locks do not lock a schema's name. Require
                        # schema administration to remain in the trusted cluster
                        # administrator boundary; otherwise name replacement could
                        # route a later statement to an executable view.
                        trusted_schema=self._query(c,"SELECT 1 FROM pg_namespace n JOIN pg_roles owner ON owner.oid=CASE WHEN n.nspowner=(SELECT oid FROM pg_roles WHERE rolname='pg_database_owner') THEN (SELECT datdba FROM pg_database WHERE datname=current_database()) ELSE n.nspowner END WHERE n.nspname=%s AND owner.rolsuper AND NOT EXISTS(SELECT 1 FROM pg_roles member WHERE NOT member.rolsuper AND pg_has_role(member.oid,owner.oid,'MEMBER'))",(schema,))
                        if not trusted_schema:raise DatabaseError('unsupported_schema_owner')
                        if rel[1]!='r' or rel[2] or rel[3] or rel[5] or rel[6] or rel[4]!=2:raise DatabaseError('unsupported_relation')
                        # Retain the relation lock through the read. Catalog evidence must
                        # come from a fresh snapshot after waiting for concurrent DDL.
                        c.execute(sql.SQL('LOCK TABLE ONLY {}.{} IN ACCESS SHARE MODE').format(sql.Identifier(schema),sql.Identifier(table)))
                        relation=self._query(c,"SELECT r.oid,r.relkind,r.relrowsecurity,r.relhassubclass,r.relam,(SELECT EXISTS(SELECT 1 FROM pg_inherits WHERE inhrelid=r.oid)),r.relhasrules FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname=%s AND r.relname=%s AND has_schema_privilege(n.oid,'USAGE') AND has_any_column_privilege(r.oid,'SELECT')",(schema,table))
                        if len(relation)!=1 or relation[0][0]!=oid:raise DatabaseError('identity_changed')
                        rel=relation[0]
                    columns=self._query(c,"SELECT attname,atttypid,attnotnull,attgenerated,attnum,format_type(atttypid,atttypmod) FROM pg_attribute WHERE attrelid=%s AND attnum>0 AND NOT attisdropped AND has_column_privilege(attrelid,attnum,'SELECT') ORDER BY attnum",(oid,))
                    indexes=self._query(c,"SELECT i.indisprimary,i.indisunique,i.indisvalid,i.indimmediate,i.indkey::text,ci.relname,am.amname,(i.indisready AND i.indnkeyatts=1 AND i.indexprs IS NULL AND i.indpred IS NULL AND NOT EXISTS(SELECT 1 FROM pg_opclass oc JOIN pg_namespace ons ON ons.oid=oc.opcnamespace WHERE oc.oid=ANY(i.indclass) AND (ons.nspname<>'pg_catalog' OR oc.opcname NOT IN ('int2_ops','int4_ops','int8_ops')))) FROM pg_index i JOIN pg_class ci ON ci.oid=i.indexrelid JOIN pg_am am ON am.oid=ci.relam WHERE i.indrelid=%s ORDER BY ci.relname",(oid,))
                    fingerprint=hashlib.sha256(json.dumps([rel,columns,indexes],default=str).encode()).hexdigest()
                    if action=='table':
                        if after is not None:raise DatabaseError('invalid_request')
                        keys=self._query(c,"SELECT conname,contype,conkey,confkey,confrelid FROM pg_constraint WHERE conrelid=%s AND contype IN ('p','u','f') ORDER BY conname",(oid,))
                        names={r[4]:r[0] for r in columns}
                        key_data=[]
                        for item in keys:
                            current={'name':item[0],'kind':item[1],'columns':[names.get(number) for number in item[2] or []]}
                            if item[1]=='f':
                                reference=self._query(c,"SELECT n.nspname,r.relname FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace WHERE r.oid=%s AND n.nspname=ANY(%s) AND has_schema_privilege(n.oid,'USAGE')",(item[4],self.schemas))
                                referenced=self._query(c,"SELECT attnum,attname FROM pg_attribute WHERE attrelid=%s AND attnum=ANY(%s) AND has_column_privilege(attrelid,attnum,'SELECT')",(item[4],item[3])) if reference else []
                                mapping=dict(referenced)
                                current['reference']={'schema':reference[0][0],'table':reference[0][1],'columns':[mapping[n] for n in item[3]]} if reference and all(n in mapping for n in item[3]) else {'restricted':True}
                            key_data.append(current)
                        data={'columns':[{'name':r[0],'type_oid':r[1],'type':r[5],'nullable':not r[2]} for r in columns], 'keys':key_data, 'indexes':[{'name':r[5],'primary':r[0],'unique':r[1],'valid':r[2],'columns':[names.get(int(n)) for n in r[4].split()]} for r in indexes],'schema_fingerprint':fingerprint}
                    else:
                        if rel[1]!='r' or rel[2] or rel[3] or rel[5] or rel[6] or rel[4]!=2:raise DatabaseError('unsupported_relation')
                        selected=self.columns.get(schema,{}).get(table)
                        if not isinstance(selected,list) or not selected or len(selected)>20 or len(set(selected))!=len(selected):raise DatabaseError('row_policy_required')
                        lookup={r[0]:r for r in columns}
                        if any(name not in lookup or lookup[name][1] not in SCALARS or lookup[name][3] for name in selected):raise DatabaseError('unsupported_columns')
                        pk=[r for r in indexes if r[0] and r[2] and r[3] and r[6]=='btree' and r[7] and len(r[4].split())==1]
                        if len(pk)!=1:raise DatabaseError('unsupported_pagination')
                        key=next((r for r in columns if r[4]==int(pk[0][4]) and r[1] in (20,21,23)),None)
                        if not key:raise DatabaseError('unsupported_pagination')
                        last=None
                        if after is not None:
                            if not isinstance(after,dict) or after.get('fingerprint')!=fingerprint or type(after.get('key')) is not int:raise DatabaseError('cursor_stale')
                            last=after['key']
                        limit=min(limit,max(1,200000//(len(selected)*257*6)-1))
                        projection=sql.SQL(',').join(sql.SQL('left({}::text,257)').format(sql.Identifier(n)) for n in selected)
                        query=sql.SQL('SELECT {},{} FROM ONLY {}.{} WHERE (%s::bigint IS NULL OR {}>%s) ORDER BY {} LIMIT %s').format(sql.Identifier(key[0]),projection,sql.Identifier(schema),sql.Identifier(table),sql.Identifier(key[0]),sql.Identifier(key[0]))
                        rows=self._query(c,query,(last,last,limit+1))
                        if len(rows)>limit:next_cursor=self._next({'key':rows[limit-1][0],'fingerprint':fingerprint},shape)
                        data={'columns':selected,'rows':[[{'value':v[:256] if v is not None else None,'truncated':v is not None and len(v)>256} for v in row[1:]] for row in rows[:limit]],'schema_fingerprint':fingerprint}
                result={'engine':'postgresql','identity':{'database':identity[0],'user':identity[1],'database_oid':identity[2],'server_version':identity[3],'cluster_identity_verified':False},'data':data,'next_cursor':next_cursor,'page_size':limit,'observed_at':time.time()}
                if len(json.dumps(result).encode())>240000:raise DatabaseError('result_limit')
                return result
        except DatabaseError:raise
        except Exception as error:
            code=getattr(error,'sqlstate',None)
            safe='connection_failed' if conn is None else 'timeout' if code in ('57014','55P03') else 'permission_denied' if code=='42501' else 'connection_failed' if code and (code.startswith('08') or code in ('28P01','28000','3D000')) else 'query_failed'
            raise DatabaseError(safe) from None
        finally:
            if conn is not None:
                try:conn.rollback()
                except Exception:pass
                try:conn.close()
                except Exception:pass
