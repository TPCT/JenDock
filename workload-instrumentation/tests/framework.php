<?php
require __DIR__.'/../vendor/autoload.php';
use Illuminate\Console\Scheduling\{Schedule,Event,EventMutex,SchedulingMutex};
use Shiftcodes\Workloads\{Protocol,Runtime};
// Isolated SQL type regression; SQLite is NOT PostgreSQL compatibility evidence.
final class TypedFixturePDO extends PDO {public function getAttribute(int $attribute):mixed{return $attribute===PDO::ATTR_DRIVER_NAME?'pgsql':parent::getAttribute($attribute);}}
$container=new Illuminate\Container\Container;Illuminate\Container\Container::setInstance($container);
$container->instance(EventMutex::class,new class implements EventMutex {public function create(Event $event){return true;}public function exists(Event $event){return false;}public function forget(Event $event){}});
$container->instance(SchedulingMutex::class,new class implements SchedulingMutex {public function create(Event $event,DateTimeInterface $time){return true;}public function exists(Event $event,DateTimeInterface $time){return false;}});
$db=new TypedFixturePDO('sqlite::memory:');$db->sqliteCreateFunction('now',fn()=>gmdate('c'));
$db->exec("CREATE TABLE dm_workload_registry(scope_id text PRIMARY KEY,registered_at text);CREATE TABLE dm_workload_schedules(scope_id text,schedule_id text,name text,expression text,timezone text,without_overlapping INTEGER NOT NULL CHECK(typeof(without_overlapping)='integer'),last_outcome text,PRIMARY KEY(scope_id,schedule_id))");
$c=['scope'=>['project'=>'fixture','environment'=>'development','service_id'=>'12345678-1234-4234-9234-123456789abc','definition_digest'=>str_repeat('b',64)],'role'=>'scheduler','connection_id'=>'pgsql','queues'=>['default'],'jobs_table'=>'jobs','failed_table'=>'failed_jobs','retry_classes'=>[],'schedule_names'=>['Fixture task']];
$runtime=new Runtime(new Protocol($db,$c,str_repeat('k',32),Protocol::FRAMEWORK),$db,$c);
$schedule=new Schedule('UTC');$executed=0;$schedule->call(function()use(&$executed){$executed++;})->everyMinute()->name('Fixture task');
$runtime->register($schedule);
if($executed!==0||$db->query('SELECT without_overlapping FROM dm_workload_schedules')->fetchColumn()!==0)throw new RuntimeException('Schedule registry executed work or lost boolean type');
$runtime->register($schedule);
if($db->query('SELECT count(*) FROM dm_workload_schedules')->fetchColumn()!==1)throw new RuntimeException('Registry replay duplicated tasks');
echo "PASS real Laravel schedule registration preserves typed false and never runs task\n";
// Empty is a registered immutable task set, not missing evidence.
$other=$c;$other['scope']['service_id']='22345678-1234-4234-9234-123456789abc';
$emptyRuntime=new Runtime(new Protocol($db,$other,str_repeat('k',32),Protocol::FRAMEWORK),$db,$other);
$empty=new Schedule('UTC');$emptyRuntime->register($empty);
try{$emptyRuntime->register($schedule);throw new RuntimeException('Empty registry accepted changed task set');}catch(Shiftcodes\Workloads\ProtocolError $e){if($e->getMessage()!=='registry_changed')throw $e;}
echo "PASS empty registry is immutable\n";
$db->exec('CREATE TABLE dm_workload_heartbeats(scope_id text,instance_id text,observed_at text,last_outcome text,PRIMARY KEY(scope_id,instance_id));CREATE TABLE dm_workload_events(id integer PRIMARY KEY AUTOINCREMENT,scope_id text,instance_id text,schedule_id text,job_id text,outcome text,observed_at text)');
// Simulate the real Laravel dispatch order when an overlap lock prevents run().
$mutex=new class implements EventMutex {public function create(Event $event){return false;}public function exists(Event $event){return true;}public function forget(Event $event){}};
$container->instance(EventMutex::class,$mutex);
$blocked=new Schedule('UTC');$task=$blocked->call(function()use(&$executed){$executed++;})->name('Fixture task')->withoutOverlapping();
$blockedConfig=$c;$blockedConfig['scope']['service_id']='32345678-1234-4234-9234-123456789abc';
$blockedRuntime=new Runtime(new Protocol($db,$blockedConfig,str_repeat('k',32),Protocol::FRAMEWORK),$db,$blockedConfig);$blockedRuntime->register($blocked);
$blockedRuntime->scheduleEvent(new Illuminate\Console\Events\ScheduledTaskStarting($task),'started');
$task->run($container);
$blockedRuntime->scheduleEvent(new Illuminate\Console\Events\ScheduledTaskFinished($task,0.0),'completed');
$outcomes=$db->query("SELECT outcome FROM dm_workload_events ORDER BY id")->fetchAll(PDO::FETCH_COLUMN);
if($executed!==0||$outcomes!==['skipped'])throw new RuntimeException('Overlap skip was reported as execution: '.json_encode($outcomes));
echo "PASS real Laravel overlap skip never reports completed\n";
$healthConfig=$c;$healthConfig['heartbeat_max_age']=180;
$healthRuntime=new Runtime(new Protocol($db,$healthConfig,str_repeat('k',32),Protocol::FRAMEWORK),$db,$healthConfig);
$stmt=$db->prepare('INSERT INTO dm_workload_heartbeats VALUES(?,?,?,?)');$stmt->execute([$healthRuntime->protocol->scopeId(),$healthRuntime->instance,gmdate('c',time()-120),'processing']);
if(!$healthRuntime->healthy())throw new RuntimeException('Accepted long worker timeout ignored');
$db->prepare('UPDATE dm_workload_heartbeats SET observed_at=?')->execute([gmdate('c',time()-200)]);
if($healthRuntime->healthy())throw new RuntimeException('Expired heartbeat accepted');
echo "PASS heartbeat freshness follows accepted timeout\n";
$db->exec('CREATE TABLE jobs(queue text,reserved_at integer,available_at integer);CREATE TABLE failed_jobs(id integer,uuid text)');
$db->prepare('UPDATE dm_workload_heartbeats SET observed_at=?')->execute(['2026-10-11 10:00:00+00']);
$method=new ReflectionMethod(Protocol::class,'queueObserve');$result=$method->invoke($healthRuntime->protocol);
if($result['workers'][0]['observed_at']!=='2026-10-11T10:00:00+00:00')throw new RuntimeException('Worker timestamp is not ISO8601');
echo "PASS worker timestamps use ISO8601\n";
