"""Real pinned helper JSONL → PostgreSQL acceptance; disposable product fixture."""
import json,os,shutil,sys,tempfile,time,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from live_migration_postgres import command,peers,source
from migration_runner import MigrationHelper,RunnerError
import psycopg

def main():
    before=peers();nonce=uuid.uuid4().hex;cid=None;password=uuid.uuid4().hex
    helper_image=command(['docker','image','inspect','deploy-monitor-migration-helper:review','--format','{{.Id}}'])
    image=command(['docker','image','inspect','postgres:16-alpine','--format','{{.Id}}'])
    try:
        cid=command(['docker','create','--label','com.docker.compose.project=deploy-monitor','--label','com.docker.compose.service=migration-fixture','--label','net.shiftcodes.migration-fixture='+nonce,'--memory','256m','--cpus','0.5','--tmpfs','/var/lib/postgresql/data:rw,size=268435456','--env','POSTGRES_PASSWORD','--env','POSTGRES_DB=fixture','-p','127.0.0.1::5432',image],env=dict(os.environ,POSTGRES_PASSWORD=password))
        command(['docker','start',cid]);port=int(command(['docker','inspect',cid,'--format','{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}']))
        admin=None
        for _ in range(80):
            try:admin=psycopg.connect(host='127.0.0.1',port=port,user='postgres',password=password,dbname='fixture',connect_timeout=2,autocommit=True);break
            except psycopg.Error:time.sleep(.25)
        assert admin is not None
        with admin,tempfile.TemporaryDirectory(prefix='migration-helper-') as directory:
            helper=MigrationHelper(docker=shutil.which('docker'),socket='unix:///var/run/docker.sock',image=helper_image,state_dir=directory)
            guards=[]
            def revalidate(expected):
                row=json.loads(command(['docker','inspect',expected]))[0]
                assert expected==cid and row['Id']==cid and row['Image']==image and row['State']['Running']
            def invoke(request):return helper.invoke(cid,request,revalidate=revalidate,authorize=lambda:guards.append(True))
            request={'protocol_version':1,'operation':'inspect_plan','engine':'postgresql','framework':'alembic','grammar':'alembic-postgresql-v1','database':'fixture','user':'postgres','password':password,'port':5432,'sources':{}}
            empty=invoke(request)['proof']
            for operation in ('apply','reconcile'):
                result=invoke(dict(request,operation=operation,proof=empty));assert result['status']=='committed' and result['no_op']
            assert admin.execute("SELECT to_regclass('public.alembic_version')").fetchone()==(None,)
            request['sources']={'versions/one.py':source('one',None,["op.create_table('items',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('name',sa.String(80),nullable=True))"])}
            proof=invoke(request)['proof']
            assert invoke(dict(request,operation='reconcile',proof=proof))['status']=='not_applied'
            assert invoke(dict(request,operation='apply',proof=proof))['status']=='committed'
            assert invoke(dict(request,operation='reconcile',proof=proof))['status']=='committed'
            assert admin.execute("INSERT INTO public.items(name) VALUES('sentinel') RETURNING id").fetchone()==(1,)
            replay=invoke(request)['proof']
            assert invoke(dict(request,operation='apply',proof=replay))['no_op']
            assert admin.execute('SELECT name FROM public.items').fetchall()==[('sentinel',)]
            # Actual PHP AST parser in the same immutable helper, separate ledger.
            request.update(framework='laravel',grammar='laravel12-stock-postgresql-v1',sources={'2026_10_11_000001_create_customers.php':"<?php\nuse Illuminate\\Database\\Migrations\\Migration;\nuse Illuminate\\Database\\Schema\\Blueprint;\nuse Illuminate\\Support\\Facades\\Schema;\nreturn new class extends Migration { public function up(): void { Schema::create('customers', function (Blueprint $table) { $table->id(); $table->string('name', 80); }); } public function down(): void { Schema::dropIfExists('customers'); } };"})
            proof=invoke(request)['proof'];assert invoke(dict(request,operation='apply',proof=proof))['status']=='committed'
            assert invoke(dict(request,operation='reconcile',proof=proof))['status']=='committed'
            assert admin.execute("INSERT INTO public.customers(name) VALUES('customer') RETURNING id").fetchone()==(1,)
            assert not list(Path(directory).iterdir())
            print(json.dumps({'helper_image':helper_image,'actual_jsonl_authorizations':len(guards),'empty_plan_no_ledger_write':True,'alembic_apply_reconcile_replay':True,'laravel_php_ast_apply_reconcile':True,'sentinel_preserved':True}),flush=True)
    finally:
        if cid:
            row=json.loads(command(['docker','inspect',cid]))[0];assert row['Config']['Labels'].get('net.shiftcodes.migration-fixture')==nonce
            command(['docker','rm','-f','-v',cid])
        assert peers()==before
        assert not command(['docker','ps','-aq','--filter','label=net.shiftcodes.migration-fixture='+nonce])
        print(json.dumps({'exact_cleanup':True,'peers_preserved':True}),flush=True)

if __name__=='__main__':main()
