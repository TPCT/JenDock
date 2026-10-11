<?php
namespace Shiftcodes\Workloads;

/** No payload deserialization or request-selected SQL/connection/commands. */
final class Protocol
{
    public const VERSION = '1.0.0';
    public const FRAMEWORK = '12.69.3';
    private string $scopeId;
    private ?float $deadline=null;
    public function __construct(private \PDO $db, private array $config, private string $key, private string $framework)
    {
        if ($db->getAttribute(\PDO::ATTR_DRIVER_NAME)!=='pgsql') throw new ProtocolError('unsupported_driver');
        if ($framework!==self::FRAMEWORK || strlen($key)<32) throw new ProtocolError('unsupported_compatibility');
        self::validateConfig($config);
        $this->scopeId=hash('sha256',self::canonical($config['scope']));
    }
    public static function canonical(mixed $value): string
    {
        if(is_array($value)){if(!array_is_list($value))ksort($value);foreach($value as &$v)$v=json_decode(self::canonical($v),true,64,JSON_THROW_ON_ERROR);}
        return json_encode($value,JSON_THROW_ON_ERROR|JSON_UNESCAPED_SLASHES);
    }
    public static function validateConfig(array $c): void
    {
        foreach(['scope','connection_id','queues','jobs_table','failed_table','retry_classes','role'] as $field)if(!array_key_exists($field,$c))throw new ProtocolError('instrumentation_not_configured');
        if(array_keys(array_intersect_key($c['scope'],array_flip(['project','environment','service_id','definition_digest'])))===[])throw new ProtocolError('invalid_scope');
        if(count($c['scope'])!==4 || !in_array($c['scope']['environment']??null,['development','production'],true))throw new ProtocolError('invalid_scope');
        foreach(['project','service_id'] as $field)if(!is_string($c['scope'][$field]??null)||!preg_match('/^[a-zA-Z0-9_-]{1,80}$/D',$c['scope'][$field]))throw new ProtocolError('invalid_scope');
        if(!preg_match('/^[a-f0-9]{64}$/D',$c['scope']['definition_digest']??''))throw new ProtocolError('invalid_scope');
        foreach(['connection_id','jobs_table','failed_table'] as $field)if(!is_string($c[$field])||!preg_match('/^[a-zA-Z_][a-zA-Z0-9_]{0,62}$/D',$c[$field]))throw new ProtocolError('invalid_configuration');
        if(!in_array($c['role'],['queue','scheduler'],true)||!is_array($c['queues'])||count($c['queues'])<1||count($c['queues'])>20||count(array_unique($c['queues']))!==count($c['queues']))throw new ProtocolError('invalid_configuration');
        foreach($c['queues'] as $queue)if(!is_string($queue)||!preg_match('/^[a-zA-Z0-9_-]{1,80}$/D',$queue))throw new ProtocolError('invalid_configuration');
        if(!is_array($c['retry_classes'])||count($c['retry_classes'])>100)throw new ProtocolError('invalid_configuration');
    }
    private function query(string $sql,array $values=[]): \PDOStatement {
        if($this->deadline!==null){$remaining=(int)(($this->deadline-microtime(true))*1000);if($remaining<1)throw new ProtocolError('observation_timeout');$this->db->exec("SET LOCAL statement_timeout='".min(1500,$remaining)."ms'");}
        $s=$this->db->prepare($sql);$s->execute($values);return $s;}
    private function table(string $name): string {return '"'.$this->config[$name].'"';}
    public function scopeId(): string {return $this->scopeId;}
    public function scope(): array {return $this->config['scope'];}
    public function configuration(): array {return $this->config;}
    public function handle(array $request): array
    {
        $operation=$request['operation']??'';
        $extra=match($operation){'capabilities','queue.observe','scheduler.observe'=>[], 'queue.failures'=>['queue','limit','cursor'], 'scheduler.history'=>['limit','cursor'], 'queue.retry'=>['request_id','queue','failed_job_id','failure_revision'], 'queue.receipt'=>['request_id'], default=>throw new ProtocolError('unsupported_operation')};
        $allowed=array_merge(['protocol','operation','scope'],$extra);
        if(array_diff(array_keys($request),$allowed)||($request['protocol']??null)!==1||self::canonical($request['scope']??null)!==self::canonical($this->scope()))throw new ProtocolError('invalid_request_scope');
        if(str_starts_with($operation,'queue.') && $this->config['role']!=='queue' || str_starts_with($operation,'scheduler.') && $this->config['role']!=='scheduler')throw new ProtocolError('unsupported_operation');
        if(isset($request['queue'])&&!in_array($request['queue'],$this->config['queues'],true))throw new ProtocolError('unregistered_queue');
        if(isset($request['request_id'])&&(!is_string($request['request_id'])||!preg_match('/^[A-Za-z0-9_-]{16,80}$/D',$request['request_id'])))throw new ProtocolError('invalid_request_id');
        if(isset($request['limit'])&&(!is_int($request['limit'])||$request['limit']<1||$request['limit']>100))throw new ProtocolError('invalid_limit');
        if($this->db->inTransaction())throw new ProtocolError('transaction_unavailable');
        $this->deadline=microtime(true)+8;
        $this->db->beginTransaction();
        try {
            $this->db->exec("SET LOCAL statement_timeout='1500ms'; SET LOCAL lock_timeout='1000ms'; SET LOCAL idle_in_transaction_session_timeout='5000ms'");
            if($operation!=='queue.retry')$this->db->exec('SET TRANSACTION READ ONLY');
            $this->installed();
            $result=match($operation){
                'capabilities'=>['package_version'=>self::VERSION,'package_digest'=>PackageIdentity::digest(),'framework_version'=>$this->framework,'adapter_revision'=>1,'role'=>$this->config['role'],'operations'=>$this->config['role']==='queue'?['queue.observe','queue.failures','queue.retry','queue.receipt']:['scheduler.observe','scheduler.history'],'retry_policy'=>'registered_plain_jobs_without_retry_until'],
                'queue.observe'=>$this->queueObserve(),
                'queue.failures'=>$this->failures($request),
                'queue.retry'=>$this->retry($request),
                'queue.receipt'=>$this->receipt($request['request_id']??throw new ProtocolError('invalid_request')),
                'scheduler.observe'=>$this->schedules(),
                'scheduler.history'=>$this->history($request),
            };
            $response=['protocol'=>1,'status'=>'ok','observed_at'=>gmdate('c'),'scope'=>$this->scope(),'result'=>$result];
            if(strlen(self::canonical($response))>262144)throw new ProtocolError('response_limit');
            $this->db->commit();return $response;
        } catch(\Throwable $e){if($this->db->inTransaction())$this->db->rollBack();throw $e;}
    }
    private function queueObserve(): array
    {
        // Failed storage is mandatory, so absence cannot appear as zero failures.
        $this->query('SELECT id,uuid FROM '.$this->table('failed_table').' LIMIT 0');
        $queues=[];$now=time();
        foreach($this->config['queues'] as $queue){
            $r=$this->query('SELECT count(*) FILTER(WHERE reserved_at IS NULL AND available_at<=?) AS ready,count(*) FILTER(WHERE reserved_at IS NULL AND available_at>?) AS delayed,count(*) FILTER(WHERE reserved_at IS NOT NULL) AS reserved FROM '.$this->table('jobs_table').' WHERE queue=?',[$now,$now,$queue])->fetch(\PDO::FETCH_ASSOC);
            $queues[]=['name'=>$queue,'ready'=>(int)$r['ready'],'delayed'=>(int)$r['delayed'],'reserved'=>(int)$r['reserved']];
        }
        $workers=$this->query('SELECT instance_id,observed_at,last_outcome FROM dm_workload_heartbeats WHERE scope_id=? ORDER BY observed_at DESC LIMIT 100',[$this->scopeId])->fetchAll(\PDO::FETCH_ASSOC);
        foreach($workers as &$worker)$worker['observed_at']=self::iso($worker['observed_at']);unset($worker);
        return ['connection_id'=>$this->config['connection_id'],'driver'=>'database','queues'=>$queues,'workers'=>$workers];
    }
    private function revision(array $row): string {unset($row['payload']);return hash_hmac('sha256',self::canonical($row),$this->key);}
    public static function retryPayload(string $raw,array $classes): string
    {
        if(strlen($raw)>65536)throw new ProtocolError('unsupported_payload');
        try{$p=json_decode($raw,true,64,JSON_THROW_ON_ERROR);}catch(\Throwable){throw new ProtocolError('unsupported_payload');}
        if(!is_array($p)||!is_string($p['uuid']??null)||!preg_match('/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/Di',$p['uuid'])||($p['retryUntil']??null)!==null||($p['job']??null)!=='Illuminate\\Queue\\CallQueuedHandler@call')throw new ProtocolError('unsupported_payload');
        $class=$p['data']['commandName']??null;$serialized=$p['data']['command']??null;
        if(!is_string($class)||!in_array($class,$classes,true)||!is_string($serialized)||!str_starts_with($serialized,'O:'.strlen($class).':"'.$class.'":'))throw new ProtocolError('unsupported_payload');
        $reflection=new \ReflectionClass($class);
        if($reflection->hasMethod('retryUntil')||$reflection->hasMethod('__wakeup')||$reflection->hasMethod('__unserialize'))throw new ProtocolError('unsupported_retry_policy');
        if(isset($p['attempts']))$p['attempts']=0;
        return json_encode($p,JSON_THROW_ON_ERROR);
    }
    private function cursor(string $operation,string $queue,int $last): string
    {
        $body=base64_encode(self::canonical([$this->scopeId,$operation,$queue,$last]));return $body.'.'.hash_hmac('sha256',$body,$this->key);
    }
    private function position(array $r,string $queue=''): int
    {
        if(($r['cursor']??null)===null)return 0;
        if(!is_string($r['cursor'])||strlen($r['cursor'])>1024)throw new ProtocolError('invalid_cursor');
        $parts=explode('.',$r['cursor']);if(count($parts)!==2||!hash_equals(hash_hmac('sha256',$parts[0],$this->key),$parts[1]))throw new ProtocolError('invalid_cursor');
        $v=json_decode(base64_decode($parts[0],true),true);
        if(!is_array($v)||count($v)!==4||array_slice($v,0,3)!==[$this->scopeId,$r['operation'],$queue]||!is_int($v[3])||$v[3]<0)throw new ProtocolError('invalid_cursor');return $v[3];
    }
    private function failures(array $r): array
    {
        $queue=$r['queue']??throw new ProtocolError('invalid_request');$limit=$r['limit']??25;
        $rows=$this->query('SELECT id,uuid,connection,queue,CASE WHEN octet_length(payload)<=65536 THEN payload ELSE NULL END AS payload,encode(sha256(convert_to(payload,\'UTF8\')),\'hex\') AS payload_digest,failed_at FROM '.$this->table('failed_table').' WHERE connection=? AND queue=? AND id>? ORDER BY id LIMIT '.($limit+1),[$this->config['connection_id'],$queue,$this->position($r,$queue)])->fetchAll(\PDO::FETCH_ASSOC);
        $more=count($rows)>$limit;$rows=array_slice($rows,0,$limit);$items=[];
        foreach($rows as $row){try{self::retryPayload($row['payload'],$this->config['retry_classes']);$supported=true;}catch(\Throwable){$supported=false;}
            $items[]=['failed_job_id'=>$row['uuid'],'queue'=>$row['queue'],'failed_at'=>self::iso($row['failed_at']),'classification'=>'job_failed','failure_revision'=>$this->revision($row),'retry_supported'=>$supported];}
        return ['items'=>$items,'next_cursor'=>$more?$this->cursor($r['operation'],$queue,(int)end($rows)['id']):null];
    }
    private function receipt(string $requestId): array
    {
        $r=$this->query('SELECT result FROM dm_workload_retries WHERE scope_id=? AND request_id=?',[$this->scopeId,$requestId])->fetchColumn();
        return $r===false?['status'=>'absent','request_id'=>$requestId]:json_decode($r,true,64,JSON_THROW_ON_ERROR);
    }
    private function retry(array $r): array
    {
        foreach(['request_id','queue','failed_job_id','failure_revision'] as $field)if(!is_string($r[$field]??null))throw new ProtocolError('invalid_request');
        if(!preg_match('/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/Di',$r['failed_job_id'])||!preg_match('/^[a-f0-9]{64}$/D',$r['failure_revision']))throw new ProtocolError('invalid_request');
        $hash=hash('sha256',self::canonical($r));
        // Same request serialization precedes receipt read; failed-row lock fences other IDs/scopes.
        $this->query('SELECT pg_advisory_xact_lock(hashtextextended(?,0))',[$this->scopeId.':'.$r['request_id']]);
        $prior=$this->query('SELECT request_hash,result FROM dm_workload_retries WHERE scope_id=? AND request_id=?',[$this->scopeId,$r['request_id']])->fetch(\PDO::FETCH_ASSOC);
        if($prior){if(!hash_equals($prior['request_hash'],$hash))throw new ProtocolError('request_conflict');return json_decode($prior['result'],true,64,JSON_THROW_ON_ERROR);}
        $row=$this->query('SELECT id,uuid,connection,queue,CASE WHEN octet_length(payload)<=65536 THEN payload ELSE NULL END AS payload,encode(sha256(convert_to(payload,\'UTF8\')),\'hex\') AS payload_digest,failed_at FROM '.$this->table('failed_table').' WHERE uuid=? FOR UPDATE',[$r['failed_job_id']])->fetch(\PDO::FETCH_ASSOC);
        if(!$row||$row['connection']!==$this->config['connection_id']||$row['queue']!==$r['queue']||!hash_equals($this->revision($row),$r['failure_revision']))throw new ProtocolError('failure_changed');
        $payload=self::retryPayload($row['payload'],$this->config['retry_classes']);$now=time();
        $id=$this->query('INSERT INTO '.$this->table('jobs_table').' (queue,payload,attempts,reserved_at,available_at,created_at) VALUES(?,?,0,NULL,?,?) RETURNING id',[$r['queue'],$payload,$now,$now])->fetchColumn();
        $this->query('DELETE FROM '.$this->table('failed_table').' WHERE id=?',[$row['id']]);
        $result=['status'=>'requeued','request_id'=>$r['request_id'],'failed_job_id'=>$r['failed_job_id'],'queue'=>$r['queue'],'queue_job_id'=>(string)$id,'committed_at'=>gmdate('c')];
        $this->query('INSERT INTO dm_workload_retries(scope_id,request_id,request_hash,failure_id,result) VALUES(?,?,?,?,?)',[$this->scopeId,$r['request_id'],$hash,$r['failed_job_id'],self::canonical($result)]);return $result;
    }
    private function schedules(): array
    {
        if(!$this->query('SELECT 1 FROM dm_workload_registry WHERE scope_id=?',[$this->scopeId])->fetchColumn())throw new ProtocolError('registry_not_observed');
        $rows=$this->query('SELECT schedule_id,name,expression,timezone,without_overlapping,last_outcome FROM dm_workload_schedules WHERE scope_id=? ORDER BY schedule_id LIMIT 101',[$this->scopeId])->fetchAll(\PDO::FETCH_ASSOC);
        if(count($rows)>100)throw new ProtocolError('registry_limit');
        foreach($rows as &$row){$row['without_overlapping']=filter_var($row['without_overlapping'],FILTER_VALIDATE_BOOLEAN);$row['next_due_at']=(new \Cron\CronExpression($row['expression']))->getNextRunDate('now',0,false,$row['timezone'])->format(DATE_ATOM);$row['background_supported']=false;}
        return ['registry_revision'=>$this->config['scope']['definition_digest'],'items'=>$rows,'next_cursor'=>null];
    }
    private function history(array $r): array
    {
        $limit=$r['limit']??25;$rows=$this->query('SELECT id AS event_id,schedule_id,outcome,observed_at FROM dm_workload_events WHERE scope_id=? AND id>? AND schedule_id IS NOT NULL ORDER BY id LIMIT '.($limit+1),[$this->scopeId,$this->position($r)])->fetchAll(\PDO::FETCH_ASSOC);
        $more=count($rows)>$limit;$rows=array_slice($rows,0,$limit);
        foreach($rows as &$row){$row['event_id']=(string)$row['event_id'];$row['observed_at']=self::iso($row['observed_at']);}unset($row);
        return ['items'=>$rows,'next_cursor'=>$more?$this->cursor($r['operation'],'',(int)end($rows)['event_id']):null];
    }
    private function installed(): void
    {
        foreach(['dm_workload_heartbeats'=>'scope_id,instance_id,observed_at,last_outcome','dm_workload_registry'=>'scope_id,registered_at','dm_workload_schedules'=>'scope_id,schedule_id,name,expression,timezone,without_overlapping,last_outcome','dm_workload_events'=>'id,scope_id,instance_id,schedule_id,job_id,outcome,observed_at','dm_workload_retries'=>'scope_id,request_id,request_hash,failure_id,result'] as $table=>$columns)$this->query('SELECT '.$columns.' FROM '.$table.' LIMIT 0');
    }
    private static function iso(string $value): string {return (new \DateTimeImmutable($value,new \DateTimeZone('UTC')))->format(DATE_ATOM);}
}
