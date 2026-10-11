<?php
return Illuminate\Foundation\Application::configure(basePath:dirname(__DIR__))
    ->withExceptions(function(Illuminate\Foundation\Configuration\Exceptions $exceptions){})
    ->withProviders([Shiftcodes\Workloads\WorkloadServiceProvider::class])
    ->create();
