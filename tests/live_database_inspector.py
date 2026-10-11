"""Actual local PostgreSQL/helper acceptance; no production data or services."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psycopg
from psycopg import sql
from cryptography.fernet import Fernet
from database_helper import InspectorHelper
from database_postgres import Inspector, DatabaseError

def command(args,**kwargs):
    return subprocess.check_output(args,stderr=subprocess.DEVNULL,**kwargs).decode().strip()


def ddl_race_regression(port,password,reader_password,key,environment):
    """Use actual PostgreSQL locks; the wrapper observes SQL but never replaces it."""
    admin_args=dict(host='127.0.0.1',port=port,user='postgres',password=password,dbname='fixture',autocommit=True)
    reader_args=dict(admin_args,user='inspector',password=reader_password)
    checks=[]
    for scenario in ('rls-before-lock','rls-after-lock','rename-after-lock'):
        with psycopg.connect(**admin_args) as admin:
            admin.execute('CREATE TABLE race_target(id bigint PRIMARY KEY, value text)')
            admin.execute("INSERT INTO race_target VALUES(1,'safe fixture row')")
            admin.execute('GRANT SELECT ON race_target TO inspector')
            done=threading.Event();started=threading.Event();errors=[];pid=[];threads=[]
            state={'relations':0,'row_read':False,'waiting':False}
            alteration='ALTER TABLE public.race_target '+('RENAME TO race_renamed' if scenario=='rename-after-lock' else 'ENABLE ROW LEVEL SECURITY')
            def alter():
                try:
                    with psycopg.connect(**admin_args) as ddl:
                        ddl.execute("SET statement_timeout='7000ms'")
                        pid.append(ddl.info.backend_pid);started.set()
                        ddl.execute(alteration)
                except Exception as error:errors.append(type(error).__name__)
                finally:done.set()
            def hook():
                if scenario=='rls-before-lock':
                    admin.execute(alteration)
                    return
                thread=threading.Thread(target=alter,daemon=True);threads.append(thread);thread.start()
                assert started.wait(2),'DDL connection did not start'
                deadline=time.monotonic()+2
                while time.monotonic()<deadline:
                    waiting=admin.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND mode='AccessExclusiveLock' AND NOT granted)",(pid[0],)).fetchone()[0]
                    if waiting:state['waiting']=True;break
                    if done.is_set():break
                    time.sleep(.01)
                assert state['waiting'] and not done.is_set(),'DDL was not fenced by the inspection lock'
            class Cursor:
                def __init__(self,c):self.c=c;self.relation=False
                def __enter__(self):self.c.__enter__();return self
                def __exit__(self,*args):return self.c.__exit__(*args)
                def execute(self,query,args=()):
                    text=query if isinstance(query,str) else query.as_string(self.c.connection)
                    self.relation=text.startswith('SELECT r.oid,r.relkind')
                    if text.startswith('SELECT ') and ' FROM ONLY ' in text:
                        assert state['relations']>=2,'row read lacks a fresh locked relation lookup'
                        if scenario!='rls-before-lock':assert state['waiting'] and not done.is_set(),'DDL completed before the protected read'
                        state['row_read']=True
                    return self.c.execute(query,args)
                def fetchmany(self,*args):
                    rows=self.c.fetchmany(*args)
                    if self.relation:
                        state['relations']+=1
                        if state['relations']==(1 if scenario=='rls-before-lock' else 2):hook()
                    return rows
            class Connection:
                def __init__(self):self.conn=psycopg.connect(**reader_args)
                @property
                def autocommit(self):return self.conn.autocommit
                @autocommit.setter
                def autocommit(self,value):self.conn.autocommit=value
                def cursor(self):return Cursor(self.conn.cursor())
                def rollback(self):return self.conn.rollback()
                def close(self):return self.conn.close()
            inspector=Inspector(Connection,database='fixture',user='inspector',schemas=['public'],row_columns={'public':{'race_target':['id','value']}},cursor_key=key,cursor_context={'environment':environment,'scenario':scenario})
            try:
                if scenario=='rls-before-lock':
                    try:inspector.inspect({'action':'rows','schema':'public','table':'race_target'});raise AssertionError('changed RLS metadata was accepted')
                    except DatabaseError as error:assert error.code=='unsupported_relation',error.code
                    assert not state['row_read']
                else:
                    result=inspector.inspect({'action':'rows','schema':'public','table':'race_target'})
                    assert result['data']['rows'][0][1]['value']=='safe fixture row'
                    assert state['row_read']
            finally:
                for thread in threads:thread.join(8)
            assert not errors,errors
            if scenario!='rls-before-lock':assert done.is_set(),'DDL did not finish after inspection released its lock'
            name='race_renamed' if scenario=='rename-after-lock' else 'race_target'
            observed=admin.execute('SELECT relrowsecurity FROM pg_class WHERE oid=%s::regclass',('public.'+name,)).fetchone()
            assert observed is not None and observed[0]==(scenario!='rename-after-lock')
            admin.execute(sql.SQL('DROP TABLE public.{}').format(sql.Identifier(name)))
            checks.append(scenario)
    return checks

def main():
    created=[];before=command(['docker','ps','-q','--no-trunc']).splitlines()
    helper_image=command(['docker','image','inspect','deploy-monitor-database-inspector:review','--format','{{.Id}}'])
    key=Fernet.generate_key().decode();evidence=[]
    try:
        for environment in ('development','production'):
            password=uuid.uuid4().hex;reader_password=uuid.uuid4().hex
            env=dict(os.environ,POSTGRES_PASSWORD=password)
            cid=command(['docker','run','-d','--label','com.docker.compose.project=deploy-monitor','--label','com.docker.compose.service=db-inspector-fixture-'+environment,'--name','deploy-monitor-db-fixture-'+uuid.uuid4().hex,'--memory','256m','--cpus','0.5','--tmpfs','/var/lib/postgresql/data:rw,size=268435456','--env','POSTGRES_PASSWORD','--env','POSTGRES_DB=fixture','-p','127.0.0.1::5432','postgres:16-alpine'],env=env)
            created.append(cid)
            port=int(command(['docker','inspect',cid,'--format','{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}']))
            admin=None
            for attempt in range(80):
                try:admin=psycopg.connect(host='127.0.0.1',port=port,user='postgres',password=password,dbname='fixture',connect_timeout=1,autocommit=True);break
                except psycopg.Error:time.sleep(.25)
            assert admin is not None,'fixture readiness failed'
            with admin:
                admin.execute(sql.SQL('CREATE ROLE inspector LOGIN PASSWORD {}').format(sql.Literal(reader_password)))
                admin.execute('REVOKE ALL ON DATABASE fixture FROM PUBLIC')
                admin.execute('GRANT CONNECT ON DATABASE fixture TO inspector')
                admin.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
                admin.execute('GRANT USAGE ON SCHEMA public TO inspector')
                admin.execute('CREATE TABLE sentinel(id bigint PRIMARY KEY, name text, empty_value text)')
                admin.execute('INSERT INTO sentinel VALUES(1,%s,NULL),(2,%s,%s),(3,%s,%s)',(environment,environment,'',environment,'x'*5000))
                admin.execute('CREATE VIEW unsupported AS SELECT * FROM sentinel')
                admin.execute('GRANT SELECT ON sentinel,unsupported TO inspector')
            with tempfile.TemporaryDirectory(prefix='db-inspector-state-') as state:
                helper=InspectorHelper(docker=shutil.which('docker'),socket=os.environ.get('DOCKER_HOST','unix:///var/run/docker.sock'),image=helper_image,state_dir=state)
                base={'database':'fixture','user':'inspector','password':reader_password,'port':5432,'schemas':['public'],'row_columns':{'public':{'sentinel':['id','name','empty_value']}},'cursor_key':key,'cursor_context':{'project':'deploy-monitor','environment':environment,'revision':1}}
                def verify(identity):
                    row=json.loads(command(['docker','inspect',identity]))[0]
                    assert row['Id']==cid and row['State']['Running']
                def inspect(request,**changes):return helper.inspect(cid,dict(base,request=request,**changes),revalidate=verify)
                schemas=inspect({'action':'schemas'})
                assert schemas.get('result',{}).get('data')==['public'],schemas
                tables=inspect({'action':'tables','schema':'public'});assert {r['name'] for r in tables['result']['data']}=={'sentinel','unsupported'},tables
                structure=inspect({'action':'table','schema':'public','table':'sentinel'});assert structure['result']['data']['keys'][0]['kind']=='p',structure
                request={'action':'rows','schema':'public','table':'sentinel','limit':2}
                first=inspect(request);assert first['result']['data']['rows'][0][1]['value']==environment,first
                assert first['result']['data']['rows'][0][2]['value'] is None
                assert first['result']['data']['rows'][1][2]['value']==''
                cursor=first['result']['next_cursor'];assert cursor
                second=inspect(dict(request,cursor=cursor));assert second['result']['data']['rows'][0][2]['truncated'] is True,second
                assert second['result']['next_cursor'] is None
                assert inspect(dict(request,cursor=cursor),cursor_context={'environment':'wrong'})=={'error':'cursor_stale'}
                assert inspect({'action':'rows','schema':'public','table':'unsupported'})=={'error':'unsupported_relation'}
                assert inspect({'action':'schemas'},user='postgres',password=password)=={'error':'privileged_credential'}
                assert inspect({'action':'schemas'},database='wrong')=={'error':'connection_failed'}
                with psycopg.connect(host='127.0.0.1',port=port,user='inspector',password=reader_password,dbname='fixture',autocommit=True) as conn:
                    try:conn.execute('DELETE FROM sentinel');raise AssertionError('write unexpectedly allowed')
                    except psycopg.errors.InsufficientPrivilege:pass
                    assert conn.execute('SELECT count(*) FROM sentinel').fetchone()[0]==3
                assert inspect({'action':'rows','schema':'public','table':'sentinel'})['result']['data']['rows']
                with psycopg.connect(host='127.0.0.1',port=port,user='postgres',password=password,dbname='fixture',autocommit=True) as setup:
                    setup.execute('CREATE ROLE untrusted_schema_owner NOLOGIN')
                    setup.execute('ALTER SCHEMA public OWNER TO untrusted_schema_owner')
                    try:
                        assert inspect(request)=={'error':'unsupported_schema_owner'},'non-superuser schema owner was accepted'
                    finally:
                        setup.execute('ALTER SCHEMA public OWNER TO pg_database_owner')
                    setup.execute('GRANT postgres TO untrusted_schema_owner')
                    try:
                        assert inspect(request)=={'error':'unsupported_schema_owner'},'non-superuser owner-role member was accepted'
                    finally:
                        setup.execute('REVOKE postgres FROM untrusted_schema_owner')
                        setup.execute('DROP ROLE untrusted_schema_owner')
                    assert inspect(request)['result']['data']['rows'],'trusted owner did not recover after privilege cleanup'
                    setup.execute('GRANT UPDATE(name) ON sentinel TO inspector')
                    assert inspect({'action':'schemas'})=={'error':'privileged_credential'}
                    setup.execute('REVOKE UPDATE(name) ON sentinel FROM inspector')
                    setup.execute('CREATE SCHEMA "a.b"; CREATE SCHEMA a; CREATE TABLE "a.b".c(id int PRIMARY KEY); CREATE TABLE a."b.c"(id int PRIMARY KEY)')
                    setup.execute('GRANT USAGE ON SCHEMA "a.b",a TO inspector; GRANT SELECT ON "a.b".c,a."b.c" TO inspector')
                    policies={'a.b':{'c':['id']}}
                    denied=inspect({'action':'rows','schema':'a','table':'b.c'},schemas=['a.b','a'],row_columns=policies)
                    assert denied=={'error':'row_policy_required'},denied
                    assert inspect({'action':'rows','schema':'a.b','table':'c'},schemas=['a.b','a'],row_columns=policies)['result']['data']['rows']==[]
                    setup.execute('CREATE TABLE child(id int PRIMARY KEY,parent_id bigint REFERENCES sentinel(id)); GRANT SELECT ON child TO inspector')
                    detail=inspect({'action':'table','schema':'public','table':'child'})['result']['data']
                    assert next(k for k in detail['keys'] if k['kind']=='f')['reference']=={'schema':'public','table':'sentinel','columns':['id']},detail
                    setup.execute('BEGIN; LOCK TABLE sentinel IN ACCESS EXCLUSIVE MODE')
                    try:assert inspect(request)=={'error':'timeout'}
                    finally:setup.execute('ROLLBACK')
                race_checks=ddl_race_regression(port,password,reader_password,key,environment)
                assert not list(Path(state).iterdir()),'helper recovery remained'
                evidence.append({'environment':environment,'schemas':True,'structure':True,'row_pages':2,'sentinel_preserved':True,'cross_scope_cursor_denied':True,'helper_cleanup':True,'ddl_races':race_checks,'untrusted_schema_owner_denied':True,'owner_membership_denied':True})
        print(json.dumps({'checks':evidence,'helper_image':helper_image}))
    finally:
        for cid in created:subprocess.run(['docker','rm','-f','-v',cid],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        after=command(['docker','ps','-q','--no-trunc']).splitlines()
        assert set(before)<=set(after),'existing peer changed'
        assert not command(['docker','ps','-aq','--filter','label=net.shiftcodes.database-inspection']),'orphan helper remains'

if __name__=='__main__':main()
