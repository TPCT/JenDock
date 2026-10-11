<?php
namespace Shiftcodes\Workloads;
final class WorkloadServiceProvider extends \Illuminate\Support\ServiceProvider
{
 public function register(): void {
  $this->mergeConfigFrom(__DIR__.'/../config/deploy-monitor.php','deploy-monitor');
  $this->app->singleton(Runtime::class,function($app){
   $c=Configuration::fromEnvironment($app['config']->get('deploy-monitor',[]),getenv());
   $id=$c['connection_id'];
   if($c['role']==='queue'){
    $queue=$app['config']->get('queue.connections.'.$id);$failed=$app['config']->get('queue.failed');
    if(($queue['driver']??null)!=='database'||($failed['driver']??null)!=='database-uuids'||get_class($app['queue.failer'])!==\Illuminate\Queue\Failed\DatabaseUuidFailedJobProvider::class||get_class($app['queue']->connection($id))!==\Illuminate\Queue\DatabaseQueue::class)throw new ProtocolError('unsupported_provider');
    $database=$queue['connection']??$app['config']->get('database.default');
    if(($failed['database']??null)!==$database)throw new ProtocolError('unsupported_cross_database');
    if(($queue['retry_after']??null)!==$c['workload']['configuration']['retry_after_seconds'])throw new ProtocolError('queue_configuration_changed');
    $c['jobs_table']=$queue['table'];$c['failed_table']=$failed['table'];
   }else{$database=$id;}
   $pdo=$app['db']->connection($database)->getPdo();
   $key=$app['config']->get('app.key');if(!is_string($key))throw new ProtocolError('instrumentation_not_configured');
   if(str_starts_with($key,'base64:'))$key=base64_decode(substr($key,7),true);
   if(!is_string($key)||strlen($key)<32)throw new ProtocolError('instrumentation_not_configured');
   $key=hash_hmac('sha256','deploy-monitor/workload-protocol/v1',$key,true);
   return new Runtime(new Protocol($pdo,$c,$key,$app->version()),$pdo,$c);
  });
 }
 public function boot(): void {
  if(!$this->app->runningInConsole())return;
  $this->commands([ProtocolCommand::class,HealthCommand::class,SchedulerCommand::class]);
  $this->publishes([__DIR__.'/../database/migrations'=>database_path('migrations'),__DIR__.'/../database/schema.sql'=>database_path('schema.sql')],'deploy-monitor-migrations');
  $events=$this->app['events'];
  foreach([\Illuminate\Queue\Events\JobProcessing::class=>'processing',\Illuminate\Queue\Events\JobProcessed::class=>'completed',\Illuminate\Queue\Events\JobFailed::class=>'failed'] as $event=>$outcome)$events->listen($event,fn($e)=>$this->app->make(Runtime::class)->queueEvent($e,$outcome));
  $events->listen(\Illuminate\Queue\Events\Looping::class,function($e){$runtime=$this->app->make(Runtime::class);if($runtime->protocol->configuration()['role']==='queue')$runtime->heartbeat();});
  foreach([\Illuminate\Console\Events\ScheduledTaskStarting::class=>'started',\Illuminate\Console\Events\ScheduledTaskFinished::class=>'completed',\Illuminate\Console\Events\ScheduledTaskFailed::class=>'failed',\Illuminate\Console\Events\ScheduledTaskSkipped::class=>'skipped'] as $event=>$outcome)$events->listen($event,fn($e)=>$this->app->make(Runtime::class)->scheduleEvent($e,$outcome));
 }
}
