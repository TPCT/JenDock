"""Isolated real Laravel/PostgreSQL protocol acceptance, not public UI acceptance."""
import json,os,secrets,shutil,subprocess,tempfile,time,uuid
from pathlib import Path
import psycopg
from psycopg import sql
PACKAGE=Path(__file__).resolve().parents[1]
def command(args,**kwargs):return subprocess.check_output(args,stderr=subprocess.DEVNULL,timeout=30,**kwargs).decode().strip()
def peers():
 ids=command(['docker','ps','-aq','--no-trunc']).split()
 return {r['Id']:(r['Image'],r['State']['Running']) for r in json.loads(command(['docker','inspect',*ids]))} if ids else {}
def main():
 baseline=peers();identifier=None;nonce=uuid.uuid4().hex;db=None
 digest=command(['php','-r',"require 'vendor/autoload.php';echo Shiftcodes\\Workloads\\PackageIdentity::digest();"],cwd=PACKAGE)
 with tempfile.TemporaryDirectory(prefix='workload-laravel-') as tmp:
  root=Path(tmp);(root/'config').mkdir();(root/'bootstrap/cache').mkdir(parents=True);(root/'storage/framework/views').mkdir(parents=True);(root/'storage/logs').mkdir(parents=True)
  try:
   password=secrets.token_hex(24)
   identifier=command(['docker','run','-d','--name','deploy-monitor-workload-'+nonce,'--label','com.docker.compose.project=deploy-monitor','--label','net.shiftcodes.workload-fixture='+nonce,'--tmpfs','/var/lib/postgresql/data:rw,size=268435456','--memory','256m','--cpus','.5','-e','POSTGRES_PASSWORD','-e','POSTGRES_DB=fixture','-p','127.0.0.1::5432','postgres:16-alpine'],env=dict(os.environ,POSTGRES_PASSWORD=password))
   actual=json.loads(command(['docker','inspect',identifier]))[0];port=int(actual['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort'])
   for _ in range(80):
    try:db=psycopg.connect(host='127.0.0.1',port=port,user='postgres',password=password,dbname='fixture',autocommit=True,connect_timeout=1);break
    except psycopg.Error:time.sleep(.25)
   assert db
   apppassword=secrets.token_hex(24)
   db.execute(sql.SQL('CREATE ROLE fixture_app LOGIN PASSWORD {}').format(sql.Literal(apppassword)))
   db.execute('CREATE TABLE jobs(id bigserial PRIMARY KEY,queue varchar(255) NOT NULL,payload text NOT NULL,attempts smallint NOT NULL,reserved_at integer,available_at integer NOT NULL,created_at integer NOT NULL);CREATE INDEX jobs_queue ON jobs(queue);CREATE TABLE failed_jobs(id bigserial PRIMARY KEY,uuid varchar(255) NOT NULL UNIQUE,connection text NOT NULL,queue text NOT NULL,payload text NOT NULL,exception text NOT NULL,failed_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP);CREATE TABLE business_results(id bigserial PRIMARY KEY,label text);CREATE TABLE business_flags(name text PRIMARY KEY)')
   db.execute((PACKAGE/'database/schema.sql').read_text());db.execute('GRANT USAGE ON SCHEMA public TO fixture_app;GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO fixture_app;GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO fixture_app')
   # Credentials exist only in private temporary config/environment and never output.
   (root/'config/app.php').write_text("<?php return ['name'=>'Fixture','env'=>'testing','debug'=>false,'key'=>getenv('APP_KEY'),'cipher'=>'AES-256-CBC','timezone'=>'UTC','locale'=>'en'];")
   (root/'config/database.php').write_text("<?php return ['default'=>'pgsql','connections'=>['pgsql'=>['driver'=>'pgsql','host'=>'127.0.0.1','port'=>getenv('DB_PORT'),'database'=>'fixture','username'=>'fixture_app','password'=>getenv('DB_PASSWORD'),'charset'=>'utf8','prefix'=>'','schema'=>'public','sslmode'=>'prefer']]];")
   (root/'config/queue.php').write_text("<?php return ['default'=>'database','connections'=>['database'=>['driver'=>'database','connection'=>'pgsql','table'=>'jobs','queue'=>'default','retry_after'=>90]],'failed'=>['driver'=>'database-uuids','database'=>'pgsql','table'=>'failed_jobs']];")
   (root/'config/cache.php').write_text("<?php return ['default'=>'array','stores'=>['array'=>['driver'=>'array']]];")
   (root/'config/logging.php').write_text("<?php return ['default'=>'null','channels'=>['null'=>['driver'=>'monolog','handler'=>Monolog\\Handler\\NullHandler::class]]];")
   (root/'config/deploy-monitor.php').write_text("<?php return ['retry_classes'=>[FixtureJob::class],'schedule_names'=>['Fixture task']];")
   scope={'project':'fixture','environment':'development','service_id':str(uuid.uuid4()),'definition_digest':'b'*64}
   workload={'role':'queue','adapter':'laravel-database-v1','adapter_revision':1,'framework_version':'12.69.3','instrumentation':{'version':'1.0.0','digest':digest},'configuration':{'connection':'database','queues':['default'],'timeout_seconds':60,'retry_after_seconds':90,'max_attempts':1,'sleep_seconds':1,'shutdown_grace_seconds':90}}
   env=dict(os.environ,DM_PACKAGE=str(PACKAGE),DM_APP_ROOT=str(root),APP_KEY='base64:'+__import__('base64').b64encode(secrets.token_bytes(32)).decode(),DB_PORT=str(port),DB_PASSWORD=apppassword,DEPLOY_MONITOR_WORKLOAD=json.dumps(workload),**{'DEPLOY_MONITOR_'+k.upper():v for k,v in scope.items()})
   def php(args,input=None,environ=None):
    result=subprocess.run(['php',str(PACKAGE/'tests/fixture.php'),*args],input=input,text=True,capture_output=True,timeout=20,env=environ or env)
    if result.returncode:
     safe=(result.stdout+result.stderr).replace(password,'[redacted]').replace(apppassword,'[redacted]').replace(env['APP_KEY'],'[redacted]').replace('fixture-private-exception-do-not-expose','[redacted]')
     raise AssertionError('PHP fixture failed: '+safe[:1800])
    return result.stdout
   def request(operation,**fields):return {'protocol':1,'operation':operation,'scope':scope,**fields}
   def protocol(operation,**fields):
    output=php(['deploy-monitor:protocol','--no-ansi','--no-interaction'],json.dumps(request(operation,**fields)))
    assert 'fixture-private-exception-do-not-expose' not in output and 'O:10:' not in output
    result=json.loads(output);assert result['status']=='ok',result;return result['result']
   caps=protocol('capabilities');assert caps['framework_version']=='12.69.3'
   php(['dispatch','success']);php(['queue:work','database','--once','--tries=1','--sleep=1','--no-interaction'])
   assert db.execute("SELECT count(*) FROM business_results WHERE label='success'").fetchone()[0]==1
   observation=protocol('queue.observe');assert observation['queues'][0]['ready']==0 and observation['workers'][0]['last_outcome']=='completed'
   assert 'T' in observation['workers'][0]['observed_at']
   php(['dispatch','retry','fail']);php(['queue:work','database','--once','--tries=1','--sleep=1','--no-interaction'])
   failure=protocol('queue.failures',queue='default',limit=1)['items'][0];assert failure['retry_supported']
   rid=uuid.uuid4().hex;body={'request_id':rid,'queue':'default','failed_job_id':failure['failed_job_id'],'failure_revision':failure['failure_revision']}
   first=protocol('queue.retry',**body);assert first['status']=='requeued'
   assert protocol('queue.retry',**body)==first and protocol('queue.receipt',request_id=rid)==first
   assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==1 and db.execute('SELECT count(*) FROM failed_jobs').fetchone()[0]==0
   changed=json.loads(php(['deploy-monitor:protocol'],json.dumps(request('queue.retry',**dict(body,failure_revision='c'*64)))));assert changed['error']['code']=='request_conflict'
   db.execute("INSERT INTO business_flags VALUES('retry-enabled')");php(['queue:work','database','--once','--tries=1','--sleep=1','--no-interaction'])
   assert db.execute("SELECT count(*) FROM business_results WHERE label='retry'").fetchone()[0]==1
   assert db.execute('SELECT count(*) FROM dm_workload_retries').fetchone()[0]==1
   wrong=request('queue.observe');wrong['scope']=dict(scope,environment='production');assert json.loads(php(['deploy-monitor:protocol'],json.dumps(wrong)))['status']!='ok'
   workload.update(role='scheduler',adapter='laravel-scheduler-v1',configuration={'connection':'pgsql','timezone':'UTC','scope':'default'});env['DEPLOY_MONITOR_WORKLOAD']=json.dumps(workload);scope['service_id']=str(uuid.uuid4());env['DEPLOY_MONITOR_SERVICE_ID']=scope['service_id']
   absent=json.loads(php(['deploy-monitor:protocol'],json.dumps(request('scheduler.observe'))));assert absent['error']['code']=='registry_not_observed'
   php(['schedule-once']);schedule=protocol('scheduler.observe');assert len(schedule['items'])==1 and schedule['items'][0]['last_outcome']=='completed',schedule
   history=protocol('scheduler.history');assert [e['outcome'] for e in history['items']]==['started','completed']
   before=db.execute("SELECT count(*) FROM business_results WHERE label='schedule-tick'").fetchone()[0];protocol('scheduler.observe');protocol('scheduler.history');assert db.execute("SELECT count(*) FROM business_results WHERE label='schedule-tick'").fetchone()[0]==before==1
   print(json.dumps({'actual_laravel':'12.69.3','actual_postgresql':True,'queue_events':True,'atomic_retry_replay_receipt':True,'business_completion':True,'scheduler_events':True,'read_does_not_execute':True,'wrong_scope_rejected':True,'package_digest':digest}),flush=True)
  finally:
   if db:db.close()
   if identifier:
    row=json.loads(command(['docker','inspect',identifier]))[0];assert row['Config']['Labels']['net.shiftcodes.workload-fixture']==nonce;command(['docker','rm','-f',identifier])
   after=peers()
   if after!=baseline:print(json.dumps({'peer_diff':{k:{'before':baseline.get(k),'after':after.get(k)} for k in baseline.keys()|after.keys() if baseline.get(k)!=after.get(k)}}),flush=True)
   assert after==baseline
   assert digest==command(['php','-r',"require 'vendor/autoload.php';echo Shiftcodes\\Workloads\\PackageIdentity::digest();"],cwd=PACKAGE)
   print(json.dumps({'exact_cleanup':True,'peers_preserved':True,'package_hash_unchanged':True}),flush=True)
if __name__=='__main__':main()
