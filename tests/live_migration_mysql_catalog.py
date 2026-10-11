"""Opt-in local synthetic compiler/catalog DDL acceptance, not pipeline acceptance.

Runs immutable local images, internal networking, no published ports/pulls. Only
nonce-owned resources are removed. Source copies are frozen for each execution.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'net.shiftcodes.migration-catalog-fixture'


def command(argv, *, timeout=60):
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Fixture command failed with exit ' + str(result.returncode))
    return result.stdout.decode().strip()


def inside(engine):
    import pymysql
    from migration_mysql import compile_operation
    from migration_mysql_catalog import Catalog, CatalogError
    end = time.monotonic() + 120
    while True:
        try:
            connection = pymysql.connect(host='database', user='migrator', password=os.environ['MYSQL_PASSWORD'],
                database='business', connect_timeout=3, read_timeout=5, write_timeout=5, autocommit=True)
            break
        except Exception:
            if time.monotonic() >= end: raise RuntimeError('Database readiness timed out') from None
            time.sleep(.5)
    try:
        cursor = connection.cursor()
        catalog = Catalog(cursor, database='business', user='migrator', engine=engine)
        identity = catalog.identity(); checks = ['authenticated_identity']
        assert catalog.schema_state(('events',)) == (('events', None, (), (), ()),)
        definitions = (('id',('bigint',),False,True),('enabled',('boolean',),False,False),
                       ('opened',('timestamp',),False,False),('closed',('timestamp',),True,False),
                       ('title',('varchar',120),False,False),('body',('text',),True,False),
                       ('count',('integer',),False,False),('small',('smallint',),False,False),
                       ('big',('bigint',),False,False),('day',('date',),True,False))
        sources = ('id','boolean','dateTime','timestamp','string','text','integer','smallInteger','bigInteger','date')
        semantics = tuple((0,'public','events',c[0],source,source=='id',source=='id') for c,source in zip(definitions,sources))
        create = ('create_table','public','events',definitions)
        operation = create
        try:
            baseline=catalog.schema_state(('events',))
            cursor.execute(compile_operation(create,database='business',engine=engine,framework='laravel',column_semantics=semantics))
            assert catalog.verify_transition(create,baseline,semantics) == catalog.schema_state(('events',));checks.append('create_all_supported_scalar_types_and_transition')
            add = ('add_column','public','events',('note',('text',),True,False))
            add_semantics = ((1,'public','events','note','text',False,False),)
            operation = add
            baseline=catalog.schema_state(('events',))
            cursor.execute(compile_operation(add,database='business',engine=engine,framework='laravel',column_semantics=add_semantics))
            assert catalog.verify_transition(add,baseline,add_semantics) == catalog.schema_state(('events',));checks.append('nullable_add_column_and_transition')
            index = ('create_index','public','events','events_title_index',('title',))
            operation = index
            baseline=catalog.schema_state(('events',))
            cursor.execute(compile_operation(index,database='business',engine=engine,framework='laravel'))
            assert catalog.verify_transition(index,baseline) == catalog.schema_state(('events',));checks.append('nonunique_btree_index_and_transition')
        except CatalogError as error:
            # Synthetic fixture metadata only, no credentials or business records.
            print(json.dumps({'passed':False,'code':error.code,'operation':operation[0],
                              'identity':identity,'schema':catalog.schema_state(('events',))}),flush=True)
            return False
        cursor.execute("INSERT INTO events(enabled,opened,title,`count`,small,big) VALUES(1,'2026-10-11 12:00:00','fixture',1,2,3)")
        cursor.execute('SELECT id,enabled,opened,closed,title,note FROM events')
        row=cursor.fetchone()
        assert row[0:2] == (1,1) and str(row[2])=='2026-10-11 12:00:00' and row[3] is None and row[4:] == ('fixture',None)
        checks.append('actual_insert_auto_increment_temporal_nulls')
        bool_op=('add_column','public','events',('flag',('boolean',),True,False))
        bool_semantics=((1,'public','events','flag','boolean',False,False),)
        cursor.execute(compile_operation(bool_op,database='business',engine=engine,framework='laravel',column_semantics=bool_semantics))
        assert catalog.verify_operation(bool_op,bool_semantics)
        before=catalog.schema_state(('events',))
        assert before == catalog.schema_state(('events',))
        cursor.execute('ALTER TABLE events MODIFY flag TINYINT(2) NULL')
        try: catalog.verify_operation(bool_op,bool_semantics)
        except CatalogError as error: assert error.code=='postcondition_failed'
        else: raise AssertionError('Changed column was accepted')
        assert before != catalog.schema_state(('events',));checks.append('schema_drift_rejected')
        # Account visibility is part of the proof: INFORMATION_SCHEMA may hide
        # triggers from a SELECT-only account, even when table rows are visible.
        administrator = pymysql.connect(host='database',user='root',password=os.environ['MYSQL_ROOT_PASSWORD'],
            database='business',connect_timeout=3,read_timeout=5,write_timeout=5,autocommit=True)
        try:
            with administrator.cursor() as admin:
                admin.execute('CREATE TRIGGER fixture_trigger BEFORE INSERT ON events FOR EACH ROW SET NEW.enabled = NEW.enabled')
                admin.execute("CREATE USER 'limited'@'%%' IDENTIFIED BY %s",(os.environ['MYSQL_PASSWORD'],))
                admin.execute("GRANT SELECT ON business.* TO 'limited'@'%'")
        finally: administrator.close()
        limited=pymysql.connect(host='database',user='limited',password=os.environ['MYSQL_PASSWORD'],
            database='business',connect_timeout=3,read_timeout=5,write_timeout=5,autocommit=True)
        try:
            limited_catalog=Catalog(limited.cursor(),database='business',user='limited',engine=engine)
            limited_catalog.identity()
            try: limited_catalog.schema_state(('events',))
            except CatalogError as error: assert error.code=='metadata_visibility_unverified',error.code
            else: raise AssertionError('Hidden trigger metadata was accepted')
        finally: limited.close()
        try: catalog.schema_state(('events',))
        except CatalogError as error: assert error.code=='unsafe_relation',error.code
        else: raise AssertionError('Visible trigger was accepted')
        checks.extend(('hidden_trigger_visibility_rejected','visible_trigger_rejected'))
        print(json.dumps({'passed':True,'identity':identity,'checks':checks}),flush=True)
        return True
    finally: connection.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run',action='store_true');parser.add_argument('--inside',action='store_true')
    parser.add_argument('--engine',choices=('mysql','mariadb'),required=True)
    parser.add_argument('--database-image');parser.add_argument('--helper-image');parser.add_argument('--evidence',type=Path)
    args=parser.parse_args()
    if args.inside:
        try: return 0 if inside(args.engine) else 1
        except Exception as error:
            import traceback
            print(json.dumps({'passed':False,'error_type':type(error).__name__,
                'code':getattr(error,'code',None),
                'locations':[(Path(f.filename).name,f.lineno,f.name) for f in traceback.extract_tb(error.__traceback__)]}),flush=True)
            return 1
    if not args.run: raise SystemExit('Explicit --run required')
    docker=shutil.which('docker');assert docker
    assert os.environ.get('DOCKER_HOST','unix:///var/run/docker.sock').startswith('unix:///')
    for image in (args.database_image,args.helper_image):
        assert isinstance(image,str) and re.fullmatch(r'sha256:[a-f0-9]{64}',image)
        assert command([docker,'image','inspect',image,'--format','{{.Id}}'])==image
    image=json.loads(command([docker,'image','inspect',args.database_image]))[0]
    assert any(ref.split('@')[0].split('/')[-1]==args.engine for ref in image.get('RepoDigests',[]))
    nonce=uuid.uuid4().hex;prefix='deploy-monitor-catalog-'+nonce[:12]
    network=volume=None; containers=[]; result=None
    label_args=['--label',LABEL+'='+nonce,'--label','com.docker.compose.project=deploy-monitor']
    files=(ROOT/'migration_mysql.py',ROOT/'migration_mysql_catalog.py',Path(__file__).resolve())
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    with tempfile.TemporaryDirectory(prefix='deploy-monitor-catalog-') as folder:
        path=Path(folder);path.chmod(0o755)
        for file in files: shutil.copyfile(file,path/file.name)
        password=uuid.uuid4().hex
        env=path/'private.env';env.write_text('MYSQL_ROOT_HOST=%\nMYSQL_DATABASE=business\nMYSQL_USER=migrator\nMYSQL_PASSWORD='+password+'\nMYSQL_ROOT_PASSWORD='+uuid.uuid4().hex+'\n');env.chmod(0o600)
        try:
            network=command([docker,'network','create','--internal',*label_args,prefix])
            volume=command([docker,'volume','create',*label_args,prefix+'-data'])
            cid=command([docker,'run','-d','--pull','never','--name',prefix+'-db',*label_args,
                '--label','com.docker.compose.service=catalog-'+args.engine,'--network',network,'--network-alias','database',
                '--memory','768m','--cpus','1','--env-file',str(env),'--mount','type=volume,src='+volume+',dst=/var/lib/mysql',args.database_image])
            containers.append(cid)
            helper=command([docker,'create','--pull','never','--name',prefix+'-helper',*label_args,
                '--label','com.docker.compose.service=catalog-client','--network',network,'--memory','128m','--cpus','0.5',
                '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--env-file',str(env),
                '--mount','type=bind,src='+str(path)+',dst=/fixture,readonly','--workdir','/fixture','--entrypoint','python',
                args.helper_image,'-B','live_migration_mysql_catalog.py','--inside','--engine',args.engine])
            containers.append(helper)
            process=subprocess.run([docker,'start','-a',helper],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=150)
            output=process.stdout.decode().strip()
            if output: result=json.loads(output.splitlines()[-1])
            if not result: result={'passed':False,'error':'helper_failed','exit':process.returncode}
        finally:
            for cid in reversed(containers):
                observed=json.loads(command([docker,'inspect',cid]))[0]
                assert observed['Id']==cid and observed['Config']['Labels'].get(LABEL)==nonce
                command([docker,'rm','-f','-v',cid])
            if volume:
                observed=json.loads(command([docker,'volume','inspect',volume]))[0]
                assert observed['Name']==volume and observed['Labels'].get(LABEL)==nonce
                assert not command([docker,'ps','-aq','--filter','volume='+volume])
                command([docker,'volume','rm',volume])
            if network:
                observed=json.loads(command([docker,'network','inspect',network]))[0]
                assert observed['Id']==network and observed['Labels'].get(LABEL)==nonce and not observed['Containers']
                command([docker,'network','rm',network])
        result.update({'engine':args.engine,'source_hashes':hashes,'database_image':args.database_image,
                       'helper_image':args.helper_image,'exact_cleanup':True})
        if args.evidence: args.evidence.write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result,indent=2))
        return 0 if result['passed'] else 1


if __name__=='__main__':sys.exit(main())
