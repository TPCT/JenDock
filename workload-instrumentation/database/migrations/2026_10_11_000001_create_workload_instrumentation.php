<?php
use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;
return new class extends Migration {
 public function up(): void {
  $connection=config('queue.connections.'.config('deploy-monitor.connection_id').'.connection') ?: config('database.default');
  $db=DB::connection($connection);
  if($db->getDriverName()!=='pgsql')throw new RuntimeException('Instrumentation requires PostgreSQL.');
  $db->unprepared(file_get_contents(__DIR__.'/../schema.sql'));
 }
 public function down(): void {throw new RuntimeException('Instrumentation rollback requires explicit retention review.');}
};
