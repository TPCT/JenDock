<?php
namespace Fixture;
final class SentinelJob implements \Illuminate\Contracts\Queue\ShouldQueue
{
 public function __construct(public string $identifier, public bool $failFirst=false) {}
 public function handle(): void {
  $db=\Illuminate\Support\Facades\DB::connection('pgsql');
  $row=$db->selectOne('INSERT INTO fixture_jobs(identifier,attempts,completed) VALUES(?,1,false) ON CONFLICT(identifier) DO UPDATE SET attempts=fixture_jobs.attempts+1 RETURNING attempts',[$this->identifier]);
  if($this->failFirst && (int)$row->attempts===1)throw new \RuntimeException('Synthetic first-attempt failure.');
  $db->update('UPDATE fixture_jobs SET completed=true WHERE identifier=?',[$this->identifier]);
 }
}
