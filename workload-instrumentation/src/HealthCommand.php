<?php
namespace Shiftcodes\Workloads;
final class HealthCommand extends \Illuminate\Console\Command
{
 protected $signature='deploy-monitor:health';
 public function handle(): int {try{return $this->laravel->make(Runtime::class)->healthy()?0:1;}catch(\Throwable){return 1;}}
}
