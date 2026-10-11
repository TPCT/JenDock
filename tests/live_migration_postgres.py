"""Actual disposable PostgreSQL typed migration acceptance; no production DB."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psycopg
from migration_plan import alembic_plan, PlanError
from migration_postgres import Executor, MigrationError


def command(argv,**kwargs):return subprocess.check_output(argv,stderr=subprocess.DEVNULL,timeout=30,**kwargs).decode().strip()
def peers():
    ids=command(['docker','ps','-aq','--no-trunc']).splitlines()
    return {r['Id']:(r['Image'],r['State']['Running']) for r in json.loads(command(['docker','inspect',*ids]))} if ids else {}
def source(revision,parent,body):
    return 'from alembic import op\nimport sqlalchemy as sa\nrevision = '+repr(revision)+'\ndown_revision = '+repr(parent)+'\ndef upgrade():\n'+''.join('    '+line+'\n' for line in body)
def reject(fn,code=None):
    try:fn()
    except MigrationError as error:
        if code:assert error.code==code,(error.code,code)
        return error.code
    raise AssertionError('Expected migration rejection')


def main():
    before=peers();token=uuid.uuid4().hex;cid=None
    image=command(['docker','image','inspect','postgres:16-alpine','--format','{{.Id}}'])
    password=uuid.uuid4().hex
    try:
        cid=command(['docker','create','--label','com.docker.compose.project=deploy-monitor','--label','com.docker.compose.service=migration-fixture','--label','net.shiftcodes.migration-fixture='+token,'--memory','256m','--cpus','0.5','--tmpfs','/var/lib/postgresql/data:rw,size=268435456','--env','POSTGRES_PASSWORD','--env','POSTGRES_DB=fixture','-p','127.0.0.1::5432',image],env=dict(os.environ,POSTGRES_PASSWORD=password))
        command(['docker','start',cid])
        port=int(command(['docker','inspect',cid,'--format','{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}']))
        def connect():return psycopg.connect(host='127.0.0.1',port=port,user='postgres',password=password,dbname='fixture',connect_timeout=2,autocommit=True)
        admin=None
        for _ in range(80):
            try:admin=connect();break
            except psycopg.Error:time.sleep(.25)
        assert admin is not None,'Fixture readiness failed'
        with admin:
            admin.execute('CREATE TABLE public.business_sentinel(id integer PRIMARY KEY,value text NOT NULL)')
            admin.execute("INSERT INTO public.business_sentinel VALUES(7,'preserved')")
            executor=Executor(connect,database='fixture',user='postgres')
            sources={'versions/one.py':source('one',None,["op.create_table('items',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('name',sa.String(80),nullable=True))"])}
            plan=alembic_plan(sources);calls=[]
            result=executor.apply(plan,sources=sources,authorize=lambda:calls.append(True))
            assert result['transaction_committed'] and result['heads']==['one']
            assert admin.execute("INSERT INTO public.items(name) VALUES('first') RETURNING id").fetchone()[0]==1
            assert admin.execute('SELECT version_num FROM public.alembic_version').fetchall()==[('one',)]
            hashes=dict(admin.execute('SELECT revision,source_sha256 FROM public.deploy_monitor_migration_hashes').fetchall())
            assert hashes==dict(plan.source_hashes)
            sources['versions/two.py']=source('two','one',["op.add_column('items',sa.Column('optional',sa.Text(),nullable=True))","op.create_index('items_name_idx','items',['name'])"])
            next_plan=alembic_plan(sources,('one',),hashes)
            result=executor.apply(next_plan,sources=sources,authorize=lambda:None)
            assert result['heads']==['two'] and admin.execute('SELECT optional FROM public.items').fetchone()==(None,)
            assert admin.execute("SELECT indisvalid FROM pg_index WHERE indexrelid='public.items_name_idx'::regclass").fetchone()==(True,)
            hashes=dict(admin.execute('SELECT revision,source_sha256 FROM public.deploy_monitor_migration_hashes').fetchall())
            replay=alembic_plan(sources,('two',),hashes)
            assert executor.apply(replay,sources=sources,authorize=lambda:None)['applied']==[]
            assert admin.execute('SELECT count(*) FROM public.items').fetchone()==(1,)
            reject(lambda:executor.apply(next_plan,sources=sources,authorize=lambda:None),'source_or_ledger_changed')
            reject(lambda:Executor(connect,database='wrong',user='postgres').apply(replay,sources=sources,authorize=lambda:None),'identity_changed')
            changed=dict(sources);changed['versions/one.py']+='\n'
            reject(lambda:executor.apply(replay,sources=changed,authorize=lambda:None),'source_or_ledger_changed')
            try:alembic_plan(dict(sources,**{'versions/destructive.py':source('bad','two',["op.drop_table('items')"])}),('two',),hashes)
            except PlanError:pass
            else:raise AssertionError('Destructive pending migration parsed')
            failure_sources=dict(sources,**{'versions/fail.py':source('fail','two',["op.create_table('must_rollback',sa.Column('id',sa.Integer(),primary_key=True))","op.add_column('missing_table',sa.Column('new_field',sa.Text(),nullable=True))"])})
            failing=alembic_plan(failure_sources,('two',),hashes)
            reject(lambda:executor.apply(failing,sources=failure_sources,authorize=lambda:None),'unsupported_relation')
            assert admin.execute("SELECT to_regclass('public.must_rollback')").fetchone()==(None,)
            assert admin.execute('SELECT version_num FROM public.alembic_version').fetchall()==[('two',)]
            assert dict(admin.execute('SELECT revision,source_sha256 FROM public.deploy_monitor_migration_hashes').fetchall())==hashes
            admin.execute('CREATE TABLE public.ledger_child(id integer PRIMARY KEY,version_num varchar(64) REFERENCES public.alembic_version(version_num) ON DELETE CASCADE)')
            admin.execute("INSERT INTO public.ledger_child VALUES(9,'two')")
            reject(lambda:executor.apply(replay,sources=sources,authorize=lambda:None),'unsupported_ledger')
            assert admin.execute('SELECT * FROM public.ledger_child').fetchall()==[(9,'two')]
            assert admin.execute('SELECT version_num FROM public.alembic_version').fetchall()==[('two',)]
            assert admin.execute('SELECT * FROM public.business_sentinel').fetchall()==[(7,'preserved')]
            print(json.dumps({'postgres':admin.execute('SHOW server_version').fetchone()[0],'image':image,'create_serial_add_nullable_index':True,'ledger_hashes_verified':True,'reconciled_replay_noop':True,'stale_heads_changed_hash_wrong_database_denied':True,'destructive_pending_denied':True,'midmigration_rollback':True,'business_sentinel_preserved':True,'inbound_ledger_cascade_denied':True,'executor_sha256':hashlib.sha256(Path('migration_postgres.py').read_bytes()).hexdigest()}),flush=True)
    finally:
        if cid:
            row=json.loads(command(['docker','inspect',cid]))[0]
            assert row['Id']==cid and row['Config']['Labels'].get('net.shiftcodes.migration-fixture')==token
            command(['docker','rm','-f','-v',cid])
        after=peers()
        assert before==after,'Preexisting peer identity/image/running state changed'
        assert not command(['docker','ps','-aq','--filter','label=net.shiftcodes.migration-fixture='+token])
        print(json.dumps({'exact_cleanup':True,'peer_identity_image_running_preserved':True}),flush=True)


if __name__=='__main__':main()
