<?php
spl_autoload_register(function($class){$prefix='Shiftcodes\\Workloads\\';if(str_starts_with($class,$prefix)){ $file=__DIR__.'/../src/'.substr($class,strlen($prefix)).'.php';if(is_file($file))require $file;}});
use Shiftcodes\Workloads\{Protocol,ProtocolError,Configuration};
function check($value,$message='assertion'): void {if(!$value)throw new RuntimeException($message);}
function rejects(callable $callback,string $code): void {try{$callback();}catch(ProtocolError $e){check($e->getMessage()===$code,'unexpected rejection '.$e->getMessage());return;}throw new RuntimeException('Expected '.$code);}
class SafeJob {public string $value='private';}
class DynamicRetry {public function retryUntil(){throw new RuntimeException('must not run');}}
class AwakeJob {public function __wakeup(){throw new RuntimeException('must not run');}}
$tests=[];
$tests['exact patched framework']=function(){check(Protocol::FRAMEWORK==='12.69.3');};
$workload=['role'=>'queue','adapter'=>'laravel-database-v1','adapter_revision'=>1,'framework_version'=>'12.69.3','instrumentation'=>['version'=>'1.0.0','digest'=>Shiftcodes\Workloads\PackageIdentity::digest()],'configuration'=>['connection'=>'database','queues'=>['default'],'timeout_seconds'=>60,'retry_after_seconds'=>90,'max_attempts'=>3,'sleep_seconds'=>1,'shutdown_grace_seconds'=>90]];
$environment=['DEPLOY_MONITOR_PROJECT'=>'fixture','DEPLOY_MONITOR_ENVIRONMENT'=>'development','DEPLOY_MONITOR_SERVICE_ID'=>'12345678-1234-4234-9234-123456789abc','DEPLOY_MONITOR_DEFINITION_DIGEST'=>str_repeat('b',64),'DEPLOY_MONITOR_WORKLOAD'=>json_encode($workload)];
$tests['trusted environment overrides cached config']=function()use($environment){$c=Configuration::fromEnvironment(['scope'=>['project'=>'foreign'],'retry_classes'=>[SafeJob::class]],$environment);check($c['scope']['project']==='fixture');check($c['connection_id']==='database');check($c['retry_classes']===[SafeJob::class]);};
$tests['missing environment is not cached authority']=function()use($environment){unset($environment['DEPLOY_MONITOR_SERVICE_ID']);rejects(fn()=>Configuration::fromEnvironment(['scope'=>['service_id'=>'forged']],$environment),'instrumentation_not_configured');};
$tests['unknown workload knobs rejected']=function()use($environment,$workload){$workload['retry_classes']=[SafeJob::class];$environment['DEPLOY_MONITOR_WORKLOAD']=json_encode($workload);rejects(fn()=>Configuration::fromEnvironment([],$environment),'invalid_workload');};
$tests['retry transformation does not instantiate']=function(){
 $payload=['uuid'=>'12345678-1234-4234-9234-123456789abc','job'=>'Illuminate\\Queue\\CallQueuedHandler@call','attempts'=>9,'retryUntil'=>null,'data'=>['commandName'=>SafeJob::class,'command'=>serialize(new SafeJob)]];
 $result=json_decode(Protocol::retryPayload(json_encode($payload),[SafeJob::class]),true);check($result['attempts']===0);check($result['data']===$payload['data']);
 rejects(fn()=>Protocol::retryPayload(json_encode($payload),[]),'unsupported_payload');
 $payload['retryUntil']=123;rejects(fn()=>Protocol::retryPayload(json_encode($payload),[SafeJob::class]),'unsupported_payload');
};
$tests['dynamic retry and magic methods never executed']=function(){foreach([DynamicRetry::class,AwakeJob::class] as $class){$p=['uuid'=>'12345678-1234-4234-9234-123456789abc','job'=>'Illuminate\\Queue\\CallQueuedHandler@call','data'=>['commandName'=>$class,'command'=>serialize(new $class)]];rejects(fn()=>Protocol::retryPayload(json_encode($p),[$class]),'unsupported_retry_policy');}};
$tests['strict uuid payload']=function(){$p=['uuid'=>str_repeat('-',36),'job'=>'Illuminate\\Queue\\CallQueuedHandler@call','data'=>['commandName'=>SafeJob::class,'command'=>serialize(new SafeJob)]];rejects(fn()=>Protocol::retryPayload(json_encode($p),[SafeJob::class]),'unsupported_payload');};
$tests['trusted workload digest validated']=function()use($environment){$bad=$environment;$w=json_decode($bad['DEPLOY_MONITOR_WORKLOAD'],true);$w['instrumentation']['digest']=str_repeat('c',64);$bad['DEPLOY_MONITOR_WORKLOAD']=json_encode($w);rejects(fn()=>Configuration::fromEnvironment([],$bad),'instrumentation_changed');};
$failed=0;foreach($tests as $name=>$test){try{$test();echo "PASS $name\n";}catch(Throwable $e){$failed++;echo 'FAIL '.$name.': '.get_class($e).' '.$e->getMessage()."\n";}}exit($failed?1:0);
