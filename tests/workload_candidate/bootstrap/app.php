<?php
return Illuminate\Foundation\Application::configure(basePath:dirname(__DIR__))
    ->withCommands([Fixture\DispatchCommand::class])
    ->withSchedule(function(Illuminate\Console\Scheduling\Schedule $schedule){
        $schedule->call(function(){
            Illuminate\Support\Facades\DB::connection('pgsql')->insert("INSERT INTO fixture_schedule(observed_at) VALUES(now())");
        })->name('Fixture heartbeat')->everyMinute()->timezone('UTC');
    })
    ->withExceptions(function(Illuminate\Foundation\Configuration\Exceptions $exceptions){})
    ->withProviders([Shiftcodes\Workloads\WorkloadServiceProvider::class])
    ->create();
