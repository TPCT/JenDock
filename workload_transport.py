"""Private fixed-command workload transport. Caller holds control.LOCK.

No caller-provided commands, Docker settings or protocol scope are accepted. A
queue retry requires the caller's durable intent before invoking this module.
Killing a timed-out Docker client does not prove its in-container process ended;
post-dispatch retry failures are always uncertain and must use receipt recovery.
"""
import copy
import json
import os
import re
import time

from database_helper import exchange
import managed_services
import registry
import resource_policy
import service_runtime
import service_provisioning

MAX_REQUEST = 16384
MAX_RESPONSE = 262144
MAX_INSPECT = 1048576
COMMAND = ('php','artisan','deploy-monitor:protocol','--no-ansi','--no-interaction')
OPERATIONS = frozenset({'capabilities','queue.observe','queue.failures','queue.retry','queue.receipt','scheduler.observe','scheduler.history'})


class TransportError(ValueError):
    def __init__(self, code, *, outcome_unknown=False):
        self.code=code
        self.outcome_unknown=outcome_unknown
        super().__init__('Workload protocol could not complete: '+code+'.')


def _json(raw):
    def pairs(entries):
        result={}
        for key,value in entries:
            if key in result:raise ValueError('duplicate')
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda value:(_ for _ in ()).throw(ValueError('number')))


class WorkloadTransport:
    def __init__(self, db, *, docker, socket, runner=exchange):
        if (not isinstance(docker,str) or not os.path.isabs(docker) or os.path.normpath(docker)!=docker
                or not isinstance(socket,str) or not socket.startswith('unix:///')
                or os.path.normpath(socket[7:])!=socket[7:] or not callable(runner)):
            raise TransportError('invalid_configuration')
        self.db,self.docker,self.runner=db,docker,runner
        self.env={'PATH':'/usr/local/bin:/usr/bin:/bin','DOCKER_HOST':socket}

    def _run(self,args,payload=b'',*,limit,deadline):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TransportError('timeout')
        raw=self.runner([self.docker,*args],payload,timeout=min(12,remaining),env=self.env,limit=limit)
        if not isinstance(raw,bytes) or len(raw)>limit:raise TransportError('response_limit')
        return raw

    def _inspect(self,args,deadline):
        rows=_json(self._run(args,limit=MAX_INSPECT,deadline=deadline))
        if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict):raise TransportError('identity_unavailable')
        return rows[0]

    def _current(self,project,environment,service_id,container_id,definition_digest,authorize):
        authorize()
        if self.db.in_transaction:raise TransportError('transaction_active')
        service=managed_services.get(self.db,service_id)
        definition,binding=service['definition'],service['binding']
        if (service['project']!=project or service['environment']!=environment
                or definition.get('project')!=project or definition.get('environment')!=environment
                or definition.get('service_id')!=service_id or managed_services.digest(definition)!=definition_digest
                or not binding or binding['container_id']!=container_id or service['state']!='running' or binding['state']!='running'):
            raise TransportError('target_changed_or_stopped')
        artifact=managed_services.artifact_for_service(self.db,project,definition['artifact_id'])
        if artifact!=definition['artifact'] or not artifact.get('workload'):raise TransportError('adapter_unavailable')
        managed_services.require_role_capability(self.db,artifact)
        resources=resource_policy.read(self.db,project)['document']['environments'][environment]['services'].get(binding['container_name'])
        if resources is None:raise TransportError('resource_authority_unavailable')
        return service,copy.deepcopy(resources)

    def _database_attachment(self,definition,authorize,deadline):
        """Fresh retained-DB proof on the same socket and overall request deadline."""
        import application_database_bindings as bindings
        import application_database_networks as bridges
        import database_candidates
        from database_inventory import DockerInventory
        from compose_review import review
        import control
        pin=definition['runtime']['database_attachment']
        metadata=bindings.resolve_runtime(self.db,definition['project'],definition['environment'],definition['service_id'],pin,authorize=authorize)
        def command(docker,args):
            if docker!=self.docker:raise TransportError('invalid_configuration')
            return self._run(args,limit=MAX_INSPECT,deadline=deadline).decode()
        def inventory_runner(argv,**options):
            if argv[0]!=self.docker:raise TransportError('invalid_configuration')
            return self._run(argv[1:],limit=min(MAX_INSPECT,options['limit']),deadline=min(deadline,time.monotonic()+options['timeout']))
        def compose_command(argv):
            if argv[0]!=control.COMPOSE or not os.path.isabs(control.COMPOSE):raise TransportError('invalid_configuration')
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TransportError('timeout')
            raw=self.runner(argv,b'',timeout=min(12,remaining),env=self.env,limit=MAX_INSPECT)
            if not isinstance(raw,bytes) or len(raw)>MAX_INSPECT:raise TransportError('response_limit')
            return raw.decode()
        inventory=DockerInventory(docker=self.docker,socket=self.env['DOCKER_HOST'],runner=inventory_runner,timeout=8)
        def candidate(project,environment,reference,**options):
            return database_candidates.resolve(self.db,project,environment,reference,inventory=inventory,
                provenance_resolver=lambda recipe,service,files:review(recipe,service,files,control.ROOT,compose_command,control.COMPOSE),**options)
        import application_database_probe
        proof=application_database_probe.verify(self.db,metadata,docker=self.docker,socket=self.env['DOCKER_HOST'],
            key_path=os.environ.get('DATABASE_INSPECTION_KEY_FILE',''),resolve_candidate=candidate,
            authorize=authorize,runner=self.runner,deadline=deadline)
        candidate=application_database_probe.bind_candidate(metadata,proof,candidate)
        return bridges.verify_attachment(self.db,metadata,docker=self.docker,authorize=authorize,resolve_candidate=candidate,command=command)

    def _verify(self,service,resources,authorize,deadline):
        definition,binding=service['definition'],service['binding'];artifact=definition['artifact']
        actual=self._inspect(['inspect',binding['container_id']],deadline)
        image=self._inspect(['image','inspect',artifact['image']],deadline)
        if (actual.get('Id')!=binding['container_id'] or actual.get('Image')!=artifact['image'] or image.get('Id')!=artifact['image']
                or actual.get('Name')!='/'+binding['container_name'] or actual.get('State',{}).get('Running') is not True
                or actual['State'].get('Paused') or actual['State'].get('Restarting') or actual['State'].get('Dead')):
            raise TransportError('runtime_identity_changed')
        config,host=actual.get('Config',{}),actual.get('HostConfig',{})
        name,network_name=service_provisioning._names(definition)
        paths=binding.get('compose_files')
        if not isinstance(paths,list) or len(paths)!=1:raise TransportError('binding_invalid')
        labels=service_provisioning._labels(definition,service['operation_id'])
        labels.update({'com.docker.compose.project':definition['project'],'com.docker.compose.service':name,
                       'com.docker.compose.project.config_files':paths[0]})
        context=service_runtime.Context(self.db,authorize)
        descriptor=context.recorded_attachment(definition)
        if descriptor!=binding.get('database_network'):raise TransportError('database_network_changed')
        expected={network_name:binding['network_ids'][0]}
        if descriptor:expected[descriptor['name']]=descriptor['id']
        networks=actual.get('NetworkSettings',{}).get('Networks',{})
        if (config.get('User')!=artifact['user'] or config.get('Entrypoint')!=artifact['command'][:1]
                or (config.get('Cmd') or [])!=artifact['command'][1:]
                or config.get('Healthcheck',{}).get('Test')!=['CMD',*artifact['healthcheck']]
                or type(config.get('StopTimeout')) is not int or config['StopTimeout']!=service_provisioning.stop_timeout(definition)
                or any((config.get('Labels') or {}).get(k)!=v for k,v in labels.items())
                or host.get('Privileged') or host.get('ReadonlyRootfs') is not True or host.get('CapAdd') or host.get('CapDrop')!=['ALL']
                or host.get('SecurityOpt')!=['no-new-privileges:true'] or host.get('Binds') or host.get('PortBindings') or actual.get('Mounts')
                or host.get('Devices') or host.get('DeviceRequests') or host.get('PidMode') or host.get('IpcMode') not in ('private','')
                or host.get('Memory')!=int(resources['memory_mib']*1048576) or host.get('MemorySwap')!=int(resources['memory_mib']*1048576)
                or host.get('NanoCpus')!=round(resources['cpu']*1_000_000_000) or host.get('PidsLimit')!=128
                or host.get('NetworkMode')!=network_name or set(networks)!=set(expected)
                or any(networks[k].get('NetworkID')!=v for k,v in expected.items())):
            raise TransportError('runtime_configuration_changed')
        context.verify(definition,actual,image.get('Config',{}).get('Env'))
        service_provisioning.verify_role_layout(actual,image.get('Config',{}))
        for network_name,network_id in expected.items():
            network=self._inspect(['network','inspect',network_id],deadline)
            if descriptor and network_name==descriptor['name']:
                import application_database_bindings as bindings
                import application_database_networks as bridges
                pin=definition['runtime']['database_attachment']
                metadata=bindings.resolve_runtime(self.db,definition['project'],definition['environment'],definition['service_id'],pin,authorize=authorize)
                bridges._network(network,metadata,bridges._load(self.db,metadata))
                if set(network.get('Containers',{}))-{binding['container_id'],descriptor['database_container_id']}:raise TransportError('database_network_changed')
            else:service_provisioning._network(network,definition,network_id)
            endpoint=networks[network_name].get('EndpointID')
            if not isinstance(endpoint,str) or not re.fullmatch('[a-f0-9]{64}',endpoint) or network.get('Containers',{}).get(binding['container_id'],{}).get('EndpointID')!=endpoint:
                raise TransportError('network_endpoint_changed')
        if descriptor and self._database_attachment(definition,authorize,deadline)!=descriptor:
            raise TransportError('database_network_changed')
        return actual

    def _protocol(self,service,request,deadline):
        import workload_protocol
        request=workload_protocol.validate_request(request)
        payload=json.dumps(request,separators=(',',':'),allow_nan=False).encode()+b'\n'
        if len(payload)>MAX_REQUEST:raise TransportError('request_limit')
        raw=self._run(['exec','--user',service['definition']['artifact']['user'],'-i',service['binding']['container_id'],*COMMAND],payload,limit=MAX_RESPONSE,deadline=deadline)
        response=_json(raw)
        return workload_protocol.validate_response(response,request)

    def check_target(self,project,environment,service_id,container_id,definition_digest,*,adapter_revision=None,authorize):
        """Live target validation without bootstrapping application code or running jobs."""
        if not callable(authorize):raise TransportError('authorization_required')
        try:
            service,resources=self._current(project,environment,service_id,container_id,definition_digest,authorize)
            workload=service['definition']['artifact']['workload']
            if adapter_revision is not None and (type(adapter_revision) is not int or adapter_revision!=workload['adapter_revision']):raise TransportError('adapter_identity_changed')
            self._verify(service,resources,authorize,time.monotonic()+60)
            current,current_resources=self._current(project,environment,service_id,container_id,definition_digest,authorize)
            if current!=service or current_resources!=resources:raise TransportError('target_changed')
            return copy.deepcopy({'definition':service['definition'],'workload':workload,'binding':service['binding']})
        except Exception as error:
            raise TransportError(error.code if isinstance(error,TransportError) else 'identity_unavailable') from None

    def call(self,project,environment,service_id,container_id,definition_digest,operation,arguments=None,*,adapter_revision=None,authorize):
        if not callable(authorize):raise TransportError('authorization_required')
        if operation not in OPERATIONS or arguments is not None and not isinstance(arguments,dict):raise TransportError('invalid_request')
        if not isinstance(container_id,str) or not re.fullmatch('[a-f0-9]{64}',container_id) or not isinstance(definition_digest,str) or not re.fullmatch('[a-f0-9]{64}',definition_digest):raise TransportError('invalid_target')
        dispatched=False;deadline=time.monotonic()+60
        try:
            service,resources=self._current(project,environment,service_id,container_id,definition_digest,authorize)
            scope={'project':project,'environment':environment,'service_id':service_id,'definition_digest':definition_digest}
            if set(arguments or {}) & {'scope','protocol','operation'}:raise TransportError('invalid_request')
            request={'protocol':1,'operation':operation,'scope':scope,**(arguments or {})}
            import workload_protocol
            request=workload_protocol.validate_request(request)
            workload=service['definition']['artifact']['workload']
            if adapter_revision is not None and (type(adapter_revision) is not int or adapter_revision!=workload['adapter_revision']):raise TransportError('adapter_identity_changed')
            if 'queue' in request and request['queue'] not in workload['configuration'].get('queues',[]):raise TransportError('unregistered_queue')
            if operation!='capabilities' and not operation.startswith(workload['role']+'.'):raise TransportError('unsupported_operation')
            self._verify(service,resources,authorize,deadline)
            capability=self._protocol(service,{'protocol':1,'operation':'capabilities','scope':scope},deadline)
            result=capability.get('result',{})
            expected={'role':workload['role'],'adapter_revision':workload['adapter_revision'],'package_version':workload['instrumentation']['version'],
                      'package_digest':workload['instrumentation']['digest'],'framework_version':workload['framework_version']}
            if capability.get('status')!='ok' or any(result.get(k)!=v for k,v in expected.items()):raise TransportError('adapter_identity_changed')
            current,current_resources=self._current(project,environment,service_id,container_id,definition_digest,authorize)
            if current!=service or current_resources!=resources:raise TransportError('target_changed')
            self._verify(service,resources,authorize,deadline)
            dispatched=operation=='queue.retry'
            response=capability if operation=='capabilities' else self._protocol(service,request,deadline)
            current,current_resources=self._current(project,environment,service_id,container_id,definition_digest,authorize)
            if current!=service or current_resources!=resources:raise TransportError('target_changed')
            self._verify(service,resources,authorize,deadline)
            if response.get('status')=='ok' and operation=='queue.observe':
                result=response['result'];config=workload['configuration']
                if result['connection_id']!=config['connection'] or [row['name'] for row in result['queues']]!=config['queues']:raise TransportError('adapter_configuration_changed')
            if response.get('status')=='ok' and operation in ('queue.retry','queue.receipt') and response['result'].get('status')=='requeued':
                if response['result']['queue'] not in workload['configuration']['queues']:raise TransportError('unregistered_queue')
            authorize()
            return response
        except Exception as error:
            code=error.code if isinstance(error,TransportError) else 'protocol_unavailable'
            raise TransportError(code,outcome_unknown=dispatched) from None
