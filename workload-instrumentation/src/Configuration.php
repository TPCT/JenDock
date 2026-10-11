<?php
namespace Shiftcodes\Workloads;
/** Accepted runtime environment is authoritative even when Laravel config is cached. */
final class Configuration
{
 public static function fromEnvironment(array $source,array $environment): array {
  foreach(['PROJECT','ENVIRONMENT','SERVICE_ID','DEFINITION_DIGEST','WORKLOAD'] as $name)if(!is_string($environment['DEPLOY_MONITOR_'.$name]??null)||$environment['DEPLOY_MONITOR_'.$name]==='')throw new ProtocolError('instrumentation_not_configured');
  $raw=$environment['DEPLOY_MONITOR_WORKLOAD'];if(strlen($raw)>16384)throw new ProtocolError('invalid_workload');
  try{$w=json_decode($raw,true,32,JSON_THROW_ON_ERROR);}catch(\Throwable){throw new ProtocolError('invalid_workload');}
  if(!is_array($w)||!self::keys($w,['role','adapter','adapter_revision','framework_version','instrumentation','configuration'])||!in_array($w['role'],['queue','scheduler'],true)||$w['adapter_revision']!==1||$w['framework_version']!==Protocol::FRAMEWORK||$w['adapter']!==($w['role']==='queue'?'laravel-database-v1':'laravel-scheduler-v1'))throw new ProtocolError('invalid_workload');
  if(!is_array($w['instrumentation'])||!self::keys($w['instrumentation'],['version','digest'])||$w['instrumentation']['version']!==Protocol::VERSION||!is_string($w['instrumentation']['digest'])||!preg_match('/^[a-f0-9]{64}$/D',$w['instrumentation']['digest']))throw new ProtocolError('invalid_workload');
  if(!hash_equals(PackageIdentity::digest(),$w['instrumentation']['digest']))throw new ProtocolError('instrumentation_changed');
  $c=$w['configuration'];if(!is_array($c)||!is_string($c['connection']??null)||!preg_match('/^[A-Za-z_][A-Za-z0-9_]{0,62}$/D',$c['connection']))throw new ProtocolError('invalid_workload');
  if($w['role']==='queue'){
   if(!self::keys($c,['connection','queues','timeout_seconds','retry_after_seconds','max_attempts','sleep_seconds','shutdown_grace_seconds'])||!is_array($c['queues'])||!array_is_list($c['queues'])||count($c['queues'])<1||count($c['queues'])>16)throw new ProtocolError('invalid_workload');
   foreach($c['queues'] as $queue)if(!is_string($queue)||!preg_match('/^[A-Za-z][A-Za-z0-9_-]{0,63}$/D',$queue))throw new ProtocolError('invalid_workload');
   if(count(array_unique($c['queues']))!==count($c['queues']))throw new ProtocolError('invalid_workload');
   foreach(['timeout_seconds'=>[1,600],'retry_after_seconds'=>[2,3600],'max_attempts'=>[1,100],'sleep_seconds'=>[1,60],'shutdown_grace_seconds'=>[1,900]] as $key=>[$min,$max])if(!is_int($c[$key])||$c[$key]<$min||$c[$key]>$max)throw new ProtocolError('invalid_workload');
   if($c['timeout_seconds']+5>$c['retry_after_seconds']||$c['timeout_seconds']+5>$c['shutdown_grace_seconds'])throw new ProtocolError('invalid_workload');
  }else{
   if(!self::keys($c,['connection','timezone','scope'])||$c['scope']!=='default'||!is_string($c['timezone'])||strlen($c['timezone'])>64)throw new ProtocolError('invalid_workload');
   try{new \DateTimeZone($c['timezone']);}catch(\Throwable){throw new ProtocolError('invalid_workload');}
  }
  $scope=['project'=>$environment['DEPLOY_MONITOR_PROJECT'],'environment'=>$environment['DEPLOY_MONITOR_ENVIRONMENT'],'service_id'=>$environment['DEPLOY_MONITOR_SERVICE_ID'],'definition_digest'=>$environment['DEPLOY_MONITOR_DEFINITION_DIGEST']];
  if(!preg_match('/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/D',$scope['service_id']))throw new ProtocolError('invalid_scope');
  $result=['scope'=>$scope,'role'=>$w['role'],'connection_id'=>$c['connection'],'queues'=>$c['queues']??['default'],'retry_classes'=>$source['retry_classes']??[],'schedule_names'=>$source['schedule_names']??[],'jobs_table'=>'jobs','failed_table'=>'failed_jobs','workload'=>$w,'heartbeat_max_age'=>max(90,($c['timeout_seconds']??0)+($c['sleep_seconds']??0)+15)];
  Protocol::validateConfig($result);return $result;
 }
 private static function keys(array $a,array $keys): bool {$actual=array_keys($a);sort($actual);sort($keys);return $actual===$keys;}
}
