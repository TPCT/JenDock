"""Actual parsed Laravel/PHP to PostgreSQL typed migration acceptance; no production DB."""
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
from migration_laravel import laravel_plan
from migration_plan import PlanError
from migration_laravel_postgres import Executor, GRAMMAR
from migration_postgres import MigrationError


def command(argv,**kwargs):return subprocess.check_output(argv,stderr=subprocess.DEVNULL,timeout=30,**kwargs).decode().strip()
def peers():
    ids=command(['docker','ps','-aq','--no-trunc']).splitlines()
    return {r['Id']:(r['Image'],r['State']['Running']) for r in json.loads(command(['docker','inspect',*ids]))} if ids else {}
def source(body):
    return '<?php use Illuminate\\Database\\Migrations\\Migration; use Illuminate\\Database\\Schema\\Blueprint; use Illuminate\\Support\\Facades\\Schema; return new class extends Migration { public function up():void {'+body+'}};'
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
            executor=Executor(connect,database='fixture',user='postgres',grammar=GRAMMAR)
            first='2026_10_11_100000_create_items';second='2026_10_11_110000_add_notes'
            sources={first+'.php':source("Schema::create('items',function(Blueprint $t){$t->id();$t->string('name',80)->nullable();$t->dateTime('event_at')->nullable();$t->timestamp('other_at')->nullable();$t->timestamps();});Schema::create('manual',function(Blueprint $t){$t->bigInteger('id')->primary();});")}
            plan=laravel_plan(sources)
            result=executor.apply(plan,sources=sources,authorize=lambda:None)
            assert result['transaction_committed'] and result['batch']==1
            assert admin.execute("INSERT INTO public.items(name,event_at,other_at) VALUES('first','2026-10-11 12:00:00.6','2026-10-11 12:00:00.6') RETURNING id").fetchone()==(1,)
            assert admin.execute("SELECT to_char(event_at,'SS.US'),to_char(other_at,'SS.US') FROM public.items").fetchone()==('01.000000','01.000000')
            assert admin.execute("SELECT column_default FROM information_schema.columns WHERE table_schema='public' AND table_name='manual' AND column_name='id'").fetchone()==(None,)
            # PostgreSQL's Laravel grammar ignores unsigned; preserve this behavior.
            admin.execute('INSERT INTO public.items(id) VALUES(-4)')
            admin.execute('INSERT INTO public.manual(id) VALUES(-8)')
            ledger=lambda:[{'migration':n,'batch':b} for n,b in admin.execute('SELECT migration,batch FROM public.migrations').fetchall()]
            hashes=lambda:dict(admin.execute('SELECT revision,source_sha256 FROM public.deploy_monitor_laravel_hashes').fetchall())
            assert ledger()==[{'migration':first,'batch':1}] and hashes()==dict(plan.source_hashes)
            sources[second+'.php']=source("Schema::table('items',function(Blueprint $t){$t->text('note')->nullable();$t->index('name','items_name_index');});")
            next_plan=laravel_plan(sources,ledger(),hashes())
            result=executor.apply(next_plan,sources=sources,authorize=lambda:None)
            assert result['batch']==2 and all(r[0] is None for r in admin.execute('SELECT note FROM public.items').fetchall())
            assert admin.execute("SELECT indisvalid FROM pg_index WHERE indexrelid='public.items_name_index'::regclass").fetchone()==(True,)
            replay=laravel_plan(sources,ledger(),hashes())
            assert executor.apply(replay,sources=sources,authorize=lambda:None)['applied']==[]
            reject(lambda:executor.apply(next_plan,sources=sources,authorize=lambda:None),'source_or_ledger_changed')
            reject(lambda:Executor(connect,database='wrong',user='postgres',grammar=GRAMMAR).apply(replay,sources=sources,authorize=lambda:None),'identity_changed')
            changed=dict(sources);changed[first+'.php']+='\n'
            reject(lambda:executor.apply(replay,sources=changed,authorize=lambda:None),'source_or_ledger_changed')
            try:laravel_plan(dict(sources,**{'2026_10_11_120000_bad.php':source("Schema::drop('items');")}),ledger(),hashes())
            except PlanError:pass
            else:raise AssertionError('Destructive pending migration parsed')
            failure_sources=dict(sources,**{'2026_10_11_130000_fail.php':source("Schema::create('must_rollback',function(Blueprint $t){$t->id();});Schema::table('missing',function(Blueprint $t){$t->text('note')->nullable();});")})
            failing=laravel_plan(failure_sources,ledger(),hashes())
            reject(lambda:executor.apply(failing,sources=failure_sources,authorize=lambda:None),'unsupported_relation')
            assert admin.execute("SELECT to_regclass('public.must_rollback')").fetchone()==(None,)
            assert len(ledger())==2 and hashes()==dict(replay.source_hashes)
            admin.execute('CREATE TABLE public.ledger_child(id integer PRIMARY KEY,migration_id integer REFERENCES public.migrations(id) ON DELETE CASCADE)')
            admin.execute('INSERT INTO public.ledger_child SELECT 9,id FROM public.migrations ORDER BY id LIMIT 1')
            reject(lambda:executor.apply(replay,sources=sources,authorize=lambda:None),'unsupported_ledger')
            assert admin.execute('SELECT count(*) FROM public.ledger_child').fetchone()==(1,)
            admin.execute('DROP TABLE public.ledger_child')
            admin.execute("CREATE FUNCTION public.evil_default() RETURNS integer LANGUAGE plpgsql AS $$BEGIN DELETE FROM public.business_sentinel; RETURN 999; END$$")
            admin.execute('ALTER TABLE public.migrations ALTER COLUMN id SET DEFAULT public.evil_default()')
            third=dict(sources,**{'2026_10_11_140000_more.php':source("Schema::table('items',function(Blueprint $t){$t->text('must_not_exist')->nullable();});")})
            third_plan=laravel_plan(third,ledger(),hashes())
            reject(lambda:executor.apply(third_plan,sources=third,authorize=lambda:None),'schema_verification_failed')
            assert not admin.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='items' AND column_name='must_not_exist'").fetchall()
            assert admin.execute('SELECT * FROM public.business_sentinel').fetchall()==[(7,'preserved')]
            print(json.dumps({'postgres':admin.execute('SHOW server_version').fetchone()[0],'image':image,'real_php_parser':True,'stock_grammar':GRAMMAR,'identity_and_plain_pk_distinct':True,'timestamp_precision_zero':True,'unsigned_pg_compatibility':True,'create_add_nullable_index':True,'ledger_batches_hashes_verified':True,'reconciled_replay_noop':True,'stale_heads_changed_hash_wrong_database_denied':True,'destructive_pending_denied':True,'midmigration_rollback':True,'business_sentinel_preserved':True,'inbound_ledger_cascade_denied':True,'malicious_ledger_default_denied':True,'executor_sha256':hashlib.sha256(Path('migration_laravel_postgres.py').read_bytes()).hexdigest()}),flush=True)
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
