<?php
namespace Shiftcodes\Workloads;
final class ProtocolCommand extends \Illuminate\Console\Command
{
 protected $signature='deploy-monitor:protocol';
 protected $description='Bounded private workload protocol';
 public function handle(): int {
  try{
   $input=stream_get_contents(STDIN,16385);if(strlen($input)>16384)throw new ProtocolError('request_limit');
   $request=json_decode($input,true,32,JSON_THROW_ON_ERROR);if(!is_array($request)||array_is_list($request))throw new ProtocolError('invalid_request');
   $result=$this->laravel->make(Runtime::class)->protocol->handle($request);
  }catch(\Throwable $e){$result=['protocol'=>1,'status'=>$e instanceof ProtocolError?'unsupported':'error','observed_at'=>gmdate('c'),'error'=>['code'=>$e instanceof ProtocolError?$e->getMessage():'dependency_unavailable']];}
  fwrite(STDOUT,Protocol::canonical($result)."\n");return 0;
 }
}
