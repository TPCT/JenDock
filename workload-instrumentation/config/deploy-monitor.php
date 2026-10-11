<?php
return [
 'scope'=>['project'=>env('DEPLOY_MONITOR_PROJECT'),'environment'=>env('DEPLOY_MONITOR_ENVIRONMENT'),'service_id'=>env('DEPLOY_MONITOR_SERVICE_ID'),'definition_digest'=>env('DEPLOY_MONITOR_DEFINITION_DIGEST')],
 'role'=>env('DEPLOY_MONITOR_ROLE'),
 'connection_id'=>env('DEPLOY_MONITOR_QUEUE_CONNECTION','database'),
 'queues'=>explode(',',env('DEPLOY_MONITOR_QUEUES','default')),
 // Reviewed application source may extend this, never protocol request input.
 'retry_classes'=>[],
 'schedule_names'=>[],
];
