"""Host-side issuance of grants derived from accepted domain and gateway evidence.

Callbacks are trusted daemon configuration, never request data. resolve_principal
returns current authority for the persisted ingress operation. verify_gateway must
run ingress_runtime.verify_gateway(check_backends=True) against actual Docker,
using the supplied persisted definition/root and authorization callback, then
perform bounded HTTP endpoint checks and unknown-Host rejection. It adds
http_verified=True to the returned binding only when those checks pass.
"""
import copy
import hashlib
import json
from pathlib import Path
import re
import time

import application_operations as ops
import ingress_bindings
import registry


def _digest(value):return hashlib.sha256(registry.canonical(value).encode()).hexdigest()

class DockerGatewayVerifier:
    """Trusted daemon adapter; the Docker executable is fixed at construction."""
    def __init__(self, docker):
        if not isinstance(docker,str) or not Path(docker).is_absolute() or '..' in Path(docker).parts:
            raise ValueError('An administrator-configured absolute Docker executable is required')
        self.docker=docker

    def __call__(self,db,definition,*,root,authorize):
        import ingress_runtime
        first=ingress_runtime.verify_gateway(db,definition,root=root,docker=self.docker,authorize=authorize,check_backends=True)
        routes=ingress_bindings.current_routes(db,definition)
        probe=copy.deepcopy(definition);probe['routes']=routes
        ingress_runtime._wait_http(probe,self.docker,first['container_id'],authorize)
        last=ingress_runtime.verify_gateway(db,definition,root=root,docker=self.docker,authorize=authorize,check_backends=True)
        if ({k:v for k,v in first.items() if k!='observed'}!={k:v for k,v in last.items() if k!='observed'}
                or ingress_bindings.current_routes(db,definition)!=routes):
            raise ValueError('Gateway or route identity changed during HTTP verification')
        return dict(last,http_verified=True)


class HostGrantIssuer:
    def __init__(self,adapter,db,*,verify_gateway,resolve_principal):
        if not callable(verify_gateway) or not callable(resolve_principal):raise ValueError('Trusted gateway and authority verification are required')
        self.adapter=adapter;self.db=db;self.verify_gateway=verify_gateway;self.resolve_principal=resolve_principal

    def _accepted(self,request):
        row=self.db.execute('SELECT r.*,d.document,d.digest FROM ingress_routes r JOIN ingress_definitions d ON d.id=r.definition_id WHERE r.id=?',(request['route_id'],)).fetchone()
        operation=self.db.execute('SELECT * FROM ingress_operations WHERE id=? AND route_id=?',(request['operation_id'],request['route_id'])).fetchone()
        if not row or not operation or row['status'] not in ('accepted','active'):raise ValueError('Accepted domain operation is unavailable')
        definition=json.loads(row['document'])
        if _digest(definition)!=row['digest'] or any(definition[k]!=row[k] for k in ('project','environment','hostname')):raise ValueError('Accepted domain identity changed')
        def authorize():
            principal=self.resolve_principal(self.db,dict(operation))
            if not isinstance(principal,ops.Principal) or principal.subject_id!=operation['subject'] or principal.credential_id!=operation['credential_id']:raise ValueError('Domain operation authority changed')
            ops._service_authorize(self.db,principal,'domains:create',definition['project'],definition['environment'])
            ops.recheck_credential(self.db,principal)
            source=definition.get('source_environment')
            if source in registry.ENVIRONMENTS and principal.allowed_environments is not None and (definition['project'],source) not in principal.allowed_environments:raise ValueError('Domain source environment is not authorized')
            # Initial grant/activation retains all accepted pins. Only an exact
            # committed observation journal may follow managed delivery CAS;
            # the original definition still identifies the published grant.
            import ingress
            current=ingress._definition(self.db,principal,definition['project'],definition['environment'],
                                        {key:definition[key] for key in ingress.FIELDS},'domains:create')
            if current!=definition:
                execution=None
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='domain_executions'").fetchone():
                    execution=self.db.execute('SELECT * FROM domain_executions WHERE operation_id=?',(operation['id'],)).fetchone()
                if (not execution or execution['definition_id']!=row['definition_id']
                        or execution['definition_digest']!=row['digest']):
                    raise ValueError('Domain evidence changed before a committed observation')
                import domain_execution
                domain_execution._current_authority(self.db,principal,dict(operation),definition,execution)
        authorize()
        runtime=self.db.execute('SELECT * FROM ingress_runtime_intents WHERE project=? AND environment=?',(definition['project'],definition['environment'])).fetchone()
        reserved=self.db.execute('SELECT * FROM ingress_ports WHERE project=? AND environment=?',(definition['project'],definition['environment'])).fetchone()
        capacity=self.db.execute('SELECT * FROM ingress_capacity WHERE project=? AND environment=?',(definition['project'],definition['environment'])).fetchone()
        if not runtime or not reserved or not capacity or capacity['state']!='bound' or capacity['operation_id']!=reserved['operation_id'] or capacity['container_id']!=reserved['container_id'] or not reserved['container_id']:raise ValueError('Verified bound environment gateway is required')
        gateway=json.loads(runtime['definition']);journal=json.loads(runtime['journal'])
        if (runtime['phase']!='verified' or (journal.get('binding') or {}).get('container_id')!=reserved['container_id'] or _digest(gateway)!=runtime['digest'] or gateway['project']!=definition['project'] or gateway['environment']!=definition['environment']
                or gateway['operation_id']!=reserved['operation_id'] or gateway['port']!=reserved['port'] or journal.get('container_id')!=reserved['container_id']):raise ValueError('Gateway reservation or environment identity changed')
        routes=ingress_bindings.current_routes(self.db,gateway)
        matching=[route for route in routes if route['hostname']==definition['hostname']]
        if len(matching)!=1 or matching[0]['service_id']!=definition['service_id'] or matching[0]['http_endpoint']!=definition['http_endpoint']:raise ValueError('Gateway does not verify the selected hostname and application endpoint')
        binding=self.verify_gateway(self.db,gateway,root=runtime['root'],authorize=authorize)
        if (not isinstance(binding,dict) or binding.get('http_verified') is not True or binding.get('container_id')!=reserved['container_id'] or binding.get('port')!=reserved['port']
                or binding.get('definition_digest')!=runtime['digest'] or binding.get('application_network_id')!=gateway['application_network_id']
                or binding.get('transport_network_id')!=journal.get('transport_id') or binding.get('compose_project')!=definition['project']
                or type(binding.get('observed')) not in (int,float) or not 0<=time.time()-binding['observed']<=30):raise ValueError('Fresh gateway identity verification failed')
        authorize()
        return definition,gateway,authorize

    def _binding_identity(self, definition):
        row=self.db.execute('SELECT journal FROM ingress_runtime_intents WHERE project=? AND environment=?',
                            (definition['project'],definition['environment'])).fetchone()
        if not row:raise ValueError('Gateway runtime evidence is missing')
        journal=json.loads(row['journal'])
        return {'container_id':journal.get('container_id'),'transport_id':journal.get('transport_id')}

    def authorize_route(self, route_id, grant):
        """Revalidate under the caller's existing host lock; never reacquire it.

        Production daemon wiring MUST enable this callback on HostAdapter.
        Static administrator grants without it are only the bootstrap mode.
        """
        if not isinstance(route_id,str) or not re.fullmatch('[a-f0-9]{32}',route_id):raise ValueError('Invalid route identity')
        policy=self.adapter._policy()
        _,state=self.adapter._paths(policy,route_id)
        proof=self.adapter._state(state.with_name(route_id+'.grant.json'))
        if not proof or proof.get('status')!='published' or proof.get('grant')!=grant or policy['routes'].get(route_id)!=grant:
            raise ValueError('Published owned host grant is required')
        request=proof.get('request')
        if (not isinstance(request,dict) or set(request)!={'route_id','operation_id','expected_revision'}
                or request['route_id']!=route_id or request['operation_id']!=proof.get('operation_id')
                or type(request['expected_revision']) is not int or request['expected_revision']<0):
            raise ValueError('Grant authority journal is invalid')
        with registry.transaction(self.db):
            definition,gateway,authorize=self._accepted(request)
            expected={'hostname':definition['hostname'],'zone':definition['zone_name'],'port':gateway['port'],
                      'revision':request['expected_revision']+1}
            if (grant!=expected or proof.get('definition_digest')!=_digest(definition)
                    or proof.get('gateway_digest')!=_digest(gateway)
                    or proof.get('binding_identity')!=self._binding_identity(definition)
                    or proof.get('project')!=definition['project'] or proof.get('environment')!=definition['environment']):
                raise ValueError('Granted domain or gateway identity changed')
            authorize()

    def authorize_certificate(self, route_id, grant):
        """Certificate effects require the route authority plus recent owned DNS."""
        self.authorize_route(route_id, grant)
        import ingress
        row = self.db.execute('SELECT d.document FROM ingress_routes r JOIN ingress_definitions d ON d.id=r.definition_id WHERE r.id=?', (route_id,)).fetchone()
        if not row: raise ValueError('Certificate domain definition is unavailable')
        definition = json.loads(row['document'])
        evidence = ingress._dns_evidence(self.db, route_id, definition)
        if (evidence['status'] != 'verified' or type(evidence.get('observed_at')) not in (int, float)
                or not 0 <= time.time()-evidence['observed_at'] <= 900):
            raise ValueError('Recent owned DNS evidence is required before certificate effects')

    def issue(self,request):
        if not isinstance(request,dict) or set(request)!={'route_id','operation_id','expected_revision'} or any(not isinstance(request[k],str) or not re.fullmatch('[a-f0-9]{32}',request[k]) for k in ('route_id','operation_id')) or type(request['expected_revision']) is not int or request['expected_revision']<0:raise ValueError('Only opaque domain identities and an expected revision are accepted')
        with self.adapter._locked() as policy:
            with registry.transaction(self.db):
                definition,gateway,authorize=self._accepted(request)
                route_id=request['route_id'];path,state=self.adapter._paths(policy,route_id)
                journal_path=state.with_name(route_id+'.grant.json')
                previous=self.adapter._state(journal_path)
                accepted_digest=_digest(definition)
                current=policy['routes'].get(route_id)
                if previous and previous['operation_id']==request['operation_id']:
                    if previous['request']!=request or previous['definition_digest']!=accepted_digest or previous['gateway_digest']!=_digest(gateway) or previous.get('binding_identity')!=self._binding_identity(definition):raise ValueError('Grant replay differs from accepted intent')
                    grant=previous['grant']
                    if current not in (previous['prior_grant'],grant):raise ValueError('Host grant changed outside its issuance operation')
                    if current==grant:
                        previous['status']='published';self.adapter._save(journal_path,previous)
                        return {'route_id':route_id,'operation_id':request['operation_id'],'grant':copy.deepcopy(grant),'status':'granted','ready':False}
                else:
                    if current is not None and (not previous or previous.get('status')!='published' or previous['grant']!=current):raise ValueError('Existing host grant is foreign or unresolved')
                    if request['expected_revision']!=(current['revision'] if current else 0):raise ValueError('Host grant revision changed')
                    if self.adapter._state(state) is not None or self.adapter._read(path) is not None:raise ValueError('Existing host activation must be explicitly reconciled before changing its grant')
                    grant={'hostname':definition['hostname'],'zone':definition['zone_name'],'port':gateway['port'],'revision':request['expected_revision']+1}
                    previous={'request':copy.deepcopy(request),'operation_id':request['operation_id'],'definition_digest':accepted_digest,'gateway_digest':_digest(gateway),
                              'project':definition['project'],'environment':definition['environment'],'binding_identity':self._binding_identity(definition),'prior_grant':current,'grant':grant,'status':'publishing'}
                candidate=copy.deepcopy(policy);candidate['routes'][route_id]=grant
                self.adapter._grant(candidate,route_id)
                for other,other_grant in policy['routes'].items():
                    if other==route_id:continue
                    if other_grant['hostname']==grant['hostname']:raise ValueError('Hostname already has another host grant')
                    if other_grant['port']==grant['port']:
                        _,other_state=self.adapter._paths(policy,other)
                        proof=self.adapter._state(other_state.with_name(other+'.grant.json'))
                        if not proof or proof.get('status')!='published' or proof['grant']!=other_grant or any(proof.get(k)!=previous[k] for k in ('project','environment','gateway_digest')):raise ValueError('Gateway port is reserved by another host grant')
                self.adapter._collision(grant['hostname'],path,owned=current is not None)
                authorize()
                self.adapter._save(journal_path,previous)
                self.adapter._write(self.adapter.policy_path,json.dumps(candidate,sort_keys=True))
                previous['status']='published';self.adapter._save(journal_path,previous)
                return {'route_id':route_id,'operation_id':request['operation_id'],'grant':copy.deepcopy(grant),'status':'granted','ready':False}
