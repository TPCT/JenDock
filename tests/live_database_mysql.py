"""Explicit local MySQL/MariaDB adapter acceptance (not public API acceptance).

Run with --run and immutable local --mysql-image/--mariadb-image IDs, after
coordinating exclusive fixture time. Requires test-only PyMySQL 1.1.2 and
cryptography. Creates disposable Compose deploy-monitor labeled database
containers only; never connects to existing databases or changes their grants.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from database_mysql import Inspector, DatabaseError


def command(argv, **kwargs):
    return subprocess.check_output(argv, stderr=subprocess.DEVNULL, timeout=kwargs.pop('timeout', 30), **kwargs).decode().strip()


def snapshot():
    ids = command(['docker', 'ps', '-aq', '--no-trunc']).splitlines()
    if not ids:return {}
    return {r['Id']: {'image': r['Image'], 'running': r['State']['Running'], 'restarting': r['State']['Restarting'], 'started': r['State']['StartedAt'], 'restarts': r['RestartCount']} for r in json.loads(command(['docker', 'inspect', *ids]))}


def connect(port, password, user='root'):
    import pymysql
    return pymysql.connect(host='127.0.0.1', port=port, user=user, password=password,
                           database='fixture', charset='utf8mb4', autocommit=True,
                           connect_timeout=2, read_timeout=8, write_timeout=3,
                           local_infile=False, client_flag=0)


def execute(conn, sql, args=()):
    with conn.cursor() as c:
        c.execute(sql, args)
        return c.fetchall() if c.description else []


def denied(inspector, request, code):
    try:inspector.inspect(request)
    except DatabaseError as error:
        assert error.code == code, (error.code, code)
    else:raise AssertionError('Expected rejection: ' + code)


def ddl_races(port, root_password, reader_password, key, environment):
    evidence = []
    for scenario in ('view-before-lock', 'rename-after-lock'):
        admin = connect(port, root_password)
        execute(admin, 'CREATE TABLE race_target(id bigint PRIMARY KEY, value varchar(100)) ENGINE=InnoDB')
        execute(admin, "INSERT INTO race_target VALUES(1,'safe')")
        state = {'relations': 0, 'read': False, 'waiting': False}
        done = threading.Event(); started = threading.Event(); errors = []; threads = []; pid = []
        def alter():
            ddl = None
            try:
                ddl = connect(port, root_password)
                execute(ddl, 'SET SESSION lock_wait_timeout=6')
                pid.append(execute(ddl, 'SELECT CONNECTION_ID()')[0][0]);started.set()
                execute(ddl, 'RENAME TABLE race_target TO race_old')
            except Exception as error:errors.append(type(error).__name__)
            finally:
                if ddl:ddl.close()
                done.set()
        def hook():
            if scenario == 'view-before-lock':
                execute(admin, 'RENAME TABLE race_target TO race_old')
                execute(admin, 'CREATE VIEW race_target AS SELECT id,IF(SLEEP(4)=0,value,value) AS value FROM race_old')
                return
            thread = threading.Thread(target=alter, daemon=True);threads.append(thread);thread.start()
            assert started.wait(2), 'DDL connection failed to start'
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                processes = execute(admin, 'SHOW PROCESSLIST')
                if any(r[0] == pid[0] and 'metadata lock' in str(r[6]).lower() for r in processes):
                    state['waiting'] = True;break
                if done.is_set():break
                time.sleep(.02)
            assert state['waiting'] and not done.is_set(), 'DDL not fenced by read lock'
        class Cursor:
            def __init__(self, cursor):self.c=cursor;self.relation=False
            def execute(self, sql, args=()):
                self.relation = sql.startswith('SELECT TABLE_TYPE,ENGINE')
                if sql.startswith('SELECT `id`,'):
                    assert scenario != 'view-before-lock', 'Swapped view reached row query'
                    assert state['waiting'] and not done.is_set(), 'DDL escaped lock before read'
                    state['read'] = True
                return self.c.execute(sql,args)
            def fetchmany(self, limit):
                result=self.c.fetchmany(limit)
                if self.relation:
                    state['relations'] += 1
                    if state['relations'] == (1 if scenario == 'view-before-lock' else 2):hook()
                return result
            def close(self):self.c.close()
        class Connection:
            def __init__(self):self.c=connect(port, reader_password, 'inspector')
            def cursor(self):return Cursor(self.c.cursor())
            def rollback(self):self.c.rollback()
            def close(self):self.c.close()
        inspector = Inspector(Connection, database='fixture', user='inspector', schemas=['fixture'], row_columns={'fixture': {'race_target': ['id','value']}}, cursor_key=key, cursor_context={'environment':environment,'scenario':scenario})
        request={'action':'rows','schema':'fixture','table':'race_target'}
        try:
            if scenario == 'view-before-lock':denied(inspector,request,'unsupported_relation')
            else:
                result=inspector.inspect(request)
                assert result['data']['rows'][0][1]['value']=='safe' and state['read']
        finally:
            for thread in threads:thread.join(8)
            try:
                if scenario=='view-before-lock':execute(admin,'DROP VIEW IF EXISTS race_target')
                execute(admin,'DROP TABLE IF EXISTS race_old,race_target')
            finally:admin.close()
        assert not errors, errors
        if scenario=='rename-after-lock':assert done.is_set()
        evidence.append(scenario)
    return evidence


def inspect_environment(port, root_password, environment, engine):
    reader_password=uuid.uuid4().hex;key=Fernet.generate_key()
    admin=connect(port,root_password)
    try:
        execute(admin,"CREATE USER 'inspector'@'%%' IDENTIFIED BY %s",(reader_password,))
        execute(admin,"CREATE USER 'metadata'@'%%' IDENTIFIED BY %s",(reader_password,))
        execute(admin,'GRANT SELECT, LOCK TABLES ON fixture.* TO inspector')
        execute(admin,'GRANT SELECT ON fixture.* TO metadata')
        execute(admin,'CREATE TABLE parent(id bigint unsigned PRIMARY KEY) ENGINE=InnoDB')
        execute(admin,'CREATE TABLE sentinel(id bigint unsigned PRIMARY KEY, value varchar(2048), nullable_value varchar(100), parent_id bigint unsigned, CONSTRAINT parent_fk FOREIGN KEY(parent_id) REFERENCES parent(id)) ENGINE=InnoDB')
        execute(admin,'INSERT INTO sentinel VALUES(%s,%s,NULL,NULL),(%s,%s,%s,NULL),(%s,%s,%s,NULL)',(2**63,environment,2**63+1,'x'*1024,'',2**63+2,'last','done'))
        execute(admin,'CREATE VIEW unsupported AS SELECT * FROM sentinel')
        def inspector(user='inspector',context=None):
            return Inspector(lambda:connect(port,reader_password,user), database='fixture',user=user,schemas=['fixture'],row_columns={'fixture':{'sentinel':['id','value','nullable_value']}},cursor_key=key,cursor_context=context or {'environment':environment})
        obj=inspector()
        schemas=obj.inspect({'action':'schemas'});assert schemas['data']==['fixture']
        assert schemas['engine']==engine,schemas['engine']
        tables=obj.inspect({'action':'tables','schema':'fixture','limit':2});assert len(tables['data'])==2 and tables['next_cursor']
        rest=obj.inspect({'action':'tables','schema':'fixture','limit':2,'cursor':tables['next_cursor']})
        assert {r['name'] for r in tables['data']+rest['data']}=={'parent','sentinel','unsupported'}
        structure=obj.inspect({'action':'table','schema':'fixture','table':'sentinel'})
        assert {k['kind'] for k in structure['data']['keys']}=={'p','f'}, {'engine':engine,'keys':structure['data']['keys']}
        foreign=next(k for k in structure['data']['keys'] if k['kind']=='f');assert foreign['reference']['columns']==['id']
        request={'action':'rows','schema':'fixture','table':'sentinel','limit':2}
        first=obj.inspect(request);rows=first['data']['rows']
        assert rows[0][0]['value']==str(2**63) and rows[0][1]['value']==environment and rows[0][2]['value'] is None
        assert rows[1][1]['truncated'] and len(rows[1][1]['value'])==256 and rows[1][2]['value']==''
        second=obj.inspect(dict(request,cursor=first['next_cursor']));assert len(second['data']['rows'])==1 and second['next_cursor'] is None
        denied(inspector(context={'environment':'other'}),dict(request,cursor=first['next_cursor']),'cursor_stale')
        assert inspector('metadata').inspect({'action':'schemas'})['data']==['fixture']
        denied(inspector('metadata'),request,'row_lock_privilege_required')
        denied(obj,{'action':'rows','schema':'fixture','table':'unsupported'},'unsupported_relation')
        execute(admin,'GRANT UPDATE ON fixture.sentinel TO inspector')
        denied(obj,{'action':'schemas'},'unsupported_privileges')
        execute(admin,'REVOKE UPDATE ON fixture.sentinel FROM inspector')
        races=ddl_races(port,root_password,reader_password,key,environment)
        return {'engine':engine,'environment':environment,'version':schemas['identity']['server_version'],'account':schemas['identity']['account'],'metadata':True,'pages':[2,1],'unsigned_pk':True,'null_empty_truncated':True,'missing_lock_denied':True,'write_grant_denied':True,'ddl_races':races,'cluster_identity_verified':False}
    finally:admin.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run',action='store_true');parser.add_argument('--engine',choices=('mysql','mariadb','both'),default='both');parser.add_argument('--mysql-image',required=True);parser.add_argument('--mariadb-image',required=True)
    args=parser.parse_args()
    if not args.run:raise SystemExit('Explicit --run required')
    images={'mysql':args.mysql_image,'mariadb':args.mariadb_image}
    if args.engine != 'both':images={args.engine:images[args.engine]}
    for value in images.values():
        assert len(value)==71 and value.startswith('sha256:') and all(c in '0123456789abcdef' for c in value[7:]), 'Immutable local image IDs required'
    before=snapshot();created=[];evidence=[];token=uuid.uuid4().hex
    try:
        for engine,image in images.items():
            for environment in ('development','production'):
                password=uuid.uuid4().hex
                prefix='MYSQL' if engine=='mysql' else 'MARIADB'
                env=dict(os.environ,**{prefix+'_ROOT_PASSWORD':password,prefix+'_ROOT_HOST':'%',prefix+'_DATABASE':'fixture'})
                name='deploy-monitor-mysql-fixture-'+token[:8]+'-'+engine+'-'+environment
                cid=command(['docker','create','--name',name,'--label','com.docker.compose.project=deploy-monitor','--label','com.docker.compose.service=mysql-inspector-fixture','--label','net.shiftcodes.fixture='+token,'--memory','768m','--cpus','1','--tmpfs','/var/lib/mysql:rw,size=536870912','--env',prefix+'_ROOT_PASSWORD','--env',prefix+'_ROOT_HOST','--env',prefix+'_DATABASE','-p','127.0.0.1::3306',image],env=env)
                created.append(cid);command(['docker','start',cid])
                port=int(command(['docker','inspect',cid,'--format','{{(index (index .NetworkSettings.Ports "3306/tcp") 0).HostPort}}']))
                ready=False
                for _ in range(180):
                    try:
                        conn=connect(port,password);conn.close();ready=True;break
                    except Exception:time.sleep(.5)
                assert ready,engine+' readiness failed'
                result=inspect_environment(port,password,environment,engine);result['image']=image
                evidence.append(result);print(json.dumps(result),flush=True)
                # Release this exact engine fixture before the next case to bound memory.
                assert json.loads(command(['docker','inspect',cid]))[0]['Config']['Labels']['net.shiftcodes.fixture']==token
                command(['docker','rm','-f','-v',cid]);created.remove(cid)
    finally:
        cleanup_errors=[]
        for cid in created:
            try:
                row=json.loads(command(['docker','inspect',cid]))[0]
                assert row['Id']==cid and row['Config']['Labels']['net.shiftcodes.fixture']==token
                command(['docker','rm','-f','-v',cid])
            except Exception as error:cleanup_errors.append(type(error).__name__)
        after=snapshot()
        transitions={cid:{'before':row,'after':after.get(cid)} for cid,row in before.items() if after.get(cid)!=row}
        print(json.dumps({'cleanup_errors':cleanup_errors,'peer_transitions':transitions}),flush=True)
        assert not cleanup_errors,'Fixture cleanup failed'
        assert not command(['docker','ps','-aq','--filter','label=net.shiftcodes.fixture='+token]),'Fixture leftovers'
        assert set(before)==set(after),'Peer container set changed'
        assert all(after[cid]['image']==row['image'] and (not row['running'] or after[cid]['running']) for cid,row in before.items()),'Peer identity or running state changed'
    print(json.dumps({'accepted':evidence,'adapter_sha256':hashlib.sha256(Path('database_mysql.py').read_bytes()).hexdigest(),'cleanup':True,'scope':'direct adapter against actual engines; no HTTP/helper integration'}),flush=True)


if __name__=='__main__':main()
