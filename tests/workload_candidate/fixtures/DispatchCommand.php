<?php
namespace Fixture;
final class DispatchCommand extends \Illuminate\Console\Command
{
 protected $signature='fixture:dispatch {identifier} {--fail-first}';
 public function handle(): int {
  $id=$this->argument('identifier');
  if(!is_string($id)||!preg_match('/^[a-z0-9-]{1,60}$/D',$id))return 1;
  $this->laravel['queue']->connection('database')->push(new SentinelJob($id,(bool)$this->option('fail-first')),'','default');
  $this->line('Synthetic job dispatched.');return 0;
 }
}
