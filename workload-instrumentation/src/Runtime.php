<?php
namespace Shiftcodes\Workloads;
use Illuminate\Console\Scheduling\Schedule;
final class Runtime
{
 public string $instance;
 private bool $healthy=true;
 private array $scheduleIds=[];
 private array $executing=[];
 private array $hooked=[];
 public function __construct(public Protocol $protocol,private \PDO $db,private array $config) {$this->instance=substr(gethostname(),0,60).':'.getmypid().':'.bin2hex(random_bytes(8));}
 private function execute(string $sql,array $args): void {$s=$this->db->prepare($sql);foreach($args as $index=>$value)$s->bindValue($index+1,$value,is_bool($value)?\PDO::PARAM_BOOL:(is_int($value)?\PDO::PARAM_INT:($value===null?\PDO::PARAM_NULL:\PDO::PARAM_STR)));$s->execute();}
 public function heartbeat(?string $outcome=null): void {
  if(!$this->healthy)return;
  try{$this->execute("INSERT INTO dm_workload_heartbeats VALUES(?,?,now(),?) ON CONFLICT(scope_id,instance_id) DO UPDATE SET observed_at=excluded.observed_at,last_outcome=COALESCE(excluded.last_outcome,dm_workload_heartbeats.last_outcome)",[$this->protocol->scopeId(),$this->instance,$outcome]);}
  catch(\Throwable){$this->healthy=false;}
 }
 public function queueEvent(object $event,string $outcome): void {
  if(($event->connectionName??null)!==$this->config['connection_id']||!in_array($event->job->getQueue(),$this->config['queues'],true))return;
  $id=(string)$event->job->getJobId();if(strlen($id)>100)return;
  try{$this->execute('INSERT INTO dm_workload_events(scope_id,instance_id,job_id,outcome,observed_at) VALUES(?,?,?,?,now())',[$this->protocol->scopeId(),$this->instance,$id,$outcome]);$this->heartbeat($outcome);}
  catch(\Throwable){$this->healthy=false;}
 }
 public function register(Schedule $schedule): void {
  $events=$schedule->events();if(count($events)>100)throw new ProtocolError('registry_limit');
  $entries=[];$names=[];
  foreach($events as $event){
   $name=$event->description;
   if(!is_string($name)||!in_array($name,$this->config['schedule_names']??[],true)||!preg_match('/^[A-Za-z0-9 _.-]{1,100}$/D',$name)||isset($names[$name])||$event->runInBackground||$event->isRepeatable())throw new ProtocolError('unsupported_schedule');
   $names[$name]=true;$timezone=$event->timezone instanceof \DateTimeZone?$event->timezone->getName():($event->timezone?:'UTC');
   $id=hash('sha256',Protocol::canonical([$this->protocol->scopeId(),$name,$event->expression,$timezone,(bool)$event->withoutOverlapping]));
   $this->scheduleIds[spl_object_id($event)]=$id;$entries[]=[$this->protocol->scopeId(),$id,$name,$event->expression,$timezone,(bool)$event->withoutOverlapping];
  }
  $this->db->beginTransaction();try{
   // An immutable release scope may only register the exact same task set.
   $registration=$this->db->prepare('INSERT INTO dm_workload_registry VALUES(?,now()) ON CONFLICT DO NOTHING RETURNING scope_id');$registration->execute([$this->protocol->scopeId()]);$created=$registration->fetchColumn()!==false;$registration->closeCursor();
   $existing=$this->db->prepare('SELECT schedule_id FROM dm_workload_schedules WHERE scope_id=? ORDER BY schedule_id');$existing->execute([$this->protocol->scopeId()]);$ids=$existing->fetchAll(\PDO::FETCH_COLUMN);
   $next=array_column($entries,1);sort($next);if(!$created && $ids!==$next)throw new ProtocolError('registry_changed');
   foreach($entries as $entry)$this->execute('INSERT INTO dm_workload_schedules(scope_id,schedule_id,name,expression,timezone,without_overlapping) VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING',$entry);
   $this->db->commit();
   foreach($events as $event){$key=spl_object_id($event);if(!isset($this->hooked[$key])){$this->hooked[$key]=true;$event->before(function()use($key){$this->executing[$key]=true;$this->recordSchedule($this->scheduleIds[$key],'started');});}}
  }catch(\Throwable $e){$this->db->rollBack();throw $e;}
 }
 public function scheduleEvent(object $event,string $outcome): void {
  $key=spl_object_id($event->task);$id=$this->scheduleIds[$key]??null;if(!$id)return;
  if($outcome==='started'){$this->executing[$key]=false;return;}
  if($outcome==='completed'&&!($this->executing[$key]??false))$outcome='skipped';
  if($outcome==='completed' && ($event->task->exitCode??0)!==0)$outcome='failed';
  $this->recordSchedule($id,$outcome);$this->executing[$key]=false;
 }
 private function recordSchedule(string $id,string $outcome): void {
  try{$this->execute('INSERT INTO dm_workload_events(scope_id,instance_id,schedule_id,outcome,observed_at) VALUES(?,?,?,?,now())',[$this->protocol->scopeId(),$this->instance,$id,$outcome]);$this->execute('UPDATE dm_workload_schedules SET last_outcome=? WHERE scope_id=? AND schedule_id=?',[$outcome,$this->protocol->scopeId(),$id]);$this->heartbeat();}
  catch(\Throwable){$this->healthy=false;}
 }
 public function schedulerLock(): bool {
  $scope=$this->protocol->scope();$name=Protocol::canonical([$scope['project'],$scope['environment'],'scheduler']);
  $s=$this->db->prepare('SELECT pg_try_advisory_lock(hashtextextended(?,0))');$s->execute([$name]);return (bool)$s->fetchColumn();
 }
 public function healthy(): bool {
  $s=$this->db->prepare("SELECT 1 FROM dm_workload_heartbeats WHERE scope_id=? AND instance_id LIKE ? AND observed_at>? AND observed_at<=? LIMIT 1");
  $s->execute([$this->protocol->scopeId(),substr(gethostname(),0,60).':%',gmdate('c',time()-($this->config['heartbeat_max_age']??90)),gmdate('c',time()+5)]);return $s->fetchColumn()!==false;
 }
}
