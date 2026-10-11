<?php
namespace Shiftcodes\Workloads;
final class SchedulerCommand extends \Illuminate\Console\Command
{
 protected $signature='deploy-monitor:scheduler';
 public function handle(): int {
  try{
   $runtime=$this->laravel->make(Runtime::class);
   if($runtime->protocol->configuration()['role']!=='scheduler'||!$runtime->schedulerLock())throw new ProtocolError('scheduler_scope_owned');
   $runtime->register($this->laravel->make(\Illuminate\Console\Scheduling\Schedule::class));
   while(true){$runtime->heartbeat();$this->callSilent('schedule:run');$until=microtime(true)+(60-(time()%60));while(microtime(true)<$until){$runtime->heartbeat();usleep(1000000);}}
  }catch(\Throwable){return 1;}
 }
}
