<?php
require getenv('DM_PACKAGE').'/vendor/autoload.php';
class FixtureJob implements \Illuminate\Contracts\Queue\ShouldQueue {
 use \Illuminate\Bus\Queueable;
 public function __construct(public string $label,public bool $fail=false){}
 public function handle(): void {
  if($this->fail && !\Illuminate\Support\Facades\DB::table('business_flags')->where('name','retry-enabled')->exists())throw new RuntimeException('fixture-private-exception-do-not-expose');
  \Illuminate\Support\Facades\DB::table('business_results')->insert(['label'=>$this->label]);
 }
}
$app=\Illuminate\Foundation\Application::configure(basePath:getenv('DM_APP_ROOT'))
 ->withExceptions(function(\Illuminate\Foundation\Configuration\Exceptions $exceptions){})
 ->withProviders([\Shiftcodes\Workloads\WorkloadServiceProvider::class])
 ->withSchedule(function(\Illuminate\Console\Scheduling\Schedule $schedule){$schedule->call(function(){\Illuminate\Support\Facades\DB::table('business_results')->insert(['label'=>'schedule-tick']);})->everyMinute()->name('Fixture task');})
 ->create();
$kernel=$app->make(\Illuminate\Contracts\Console\Kernel::class);$kernel->bootstrap();
if(($argv[1]??'')==='dispatch'){$app['queue']->connection('database')->push(new FixtureJob($argv[2],($argv[3]??'')==='fail'),'','default');exit(0);}
if(($argv[1]??'')==='schedule-once'){try{$kernel->all();$r=$app->make(\Shiftcodes\Workloads\Runtime::class);$r->register($app->make(\Illuminate\Console\Scheduling\Schedule::class));$r->heartbeat();$kernel->call('schedule:run');exit(0);}catch(Throwable $e){echo json_encode(['fixture_error'=>get_class($e),'message'=>$e->getMessage()]),"\n";exit(1);}}
if(($argv[1]??'')==='protocol-direct'){
 try{$request=json_decode(stream_get_contents(STDIN),true,32,JSON_THROW_ON_ERROR);echo \Shiftcodes\Workloads\Protocol::canonical($app->make(\Shiftcodes\Workloads\Runtime::class)->protocol->handle($request)),"\n";}catch(Throwable $e){echo json_encode(['fixture_error'=>get_class($e),'message'=>$e instanceof \Shiftcodes\Workloads\ProtocolError?$e->getMessage():'dependency_unavailable']),"\n";exit(1);}exit(0);
}
$status=$kernel->handle(new \Symfony\Component\Console\Input\ArgvInput(),new \Symfony\Component\Console\Output\ConsoleOutput());$kernel->terminate(new \Symfony\Component\Console\Input\ArgvInput(),$status);exit($status);
