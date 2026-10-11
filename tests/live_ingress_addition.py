"""Sequential route addition over the actual deploy-monitor Docker fixture.

All runtime effects and exact cleanup remain owned by live_ingress_runtime.main.
No provider, deployed application, host Apache, or production effects.
"""
import json
import http.client
from pathlib import Path
import uuid
from unittest.mock import patch
import live_ingress_runtime as fixture
import ingress_runtime as runtime
import ingress_bindings
import ingress_route_generations as generations


def verify_public_proofs(db,definition,port):
    assert definition['schema_version']==3
    tokens={}
    for route in ingress_bindings.current_routes(db,definition):
        client=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
        try:
            client.request('GET',runtime.ROUTE_PROOF_PATH,headers={'Host':route['hostname']})
            response=client.getresponse();body=response.read(4096)
            expected=runtime.route_proof_token(definition['project'],definition['environment'],route).encode()
            assert response.status==200 and body==expected and len(body)==64
            assert response.getheader('Content-Type')=='text/plain'
            assert response.getheader('Cache-Control')=='no-store'
            tokens[route['hostname']]=body.decode()
        finally:client.close()
    assert len(set(tokens.values()))==len(tokens),'Different hostnames must not share proof tokens'
    assert fixture.request(port,'unknown.fixture.test',runtime.ROUTE_PROOF_PATH)[0]==421
    return tokens


def acceptance(db,docker,apps,ports):
    row=db.execute('SELECT * FROM ingress_runtime_intents WHERE project=? AND environment=?',(fixture.PROJECT,'development')).fetchone()
    d=json.loads(row['definition']);root=Path(row['root']);authorize=lambda:None
    prior=runtime.verify_gateway(db,d,root=root,docker=docker,authorize=authorize)
    gateway_before=fixture.identity(fixture.inspect(docker,prior['container_id']))
    production=fixture.identity(fixture.inspect(docker,apps['production']['binding']['container_id']))
    production_routes=ingress_bindings.capture(db,fixture.PROJECT,'production',apps['production']['id'])
    initial=generations.head(db,d);events=[]
    proof_before=verify_public_proofs(db,d,ports['development'])
    original=runtime._command
    for hostname,lost in [('added.development.fixture.test',False),('lost-reply.development.fixture.test',True)]:
        operation=str(uuid.uuid4());signals=[]
        prepared=runtime.prepare_route_addition(db,d,operation,{'hostname':hostname,'service_id':apps['development']['id']},root=root,docker=docker,authorize=authorize)
        def command(args,**kwargs):
            result=original(args,**kwargs)
            if args[:3]==[docker,'exec',prior['container_id']] and '-s' in args:
                signals.append(list(args))
                if lost:raise runtime.GatewayError('Simulated lost reply after actual nginx reload')
            return result
        with patch.object(runtime,'_command',side_effect=command):
            if lost:
                try:runtime.apply_route_addition(db,d,operation,root=root,docker=docker,authorize=authorize)
                except runtime.GatewayError:pass
                else:raise AssertionError('Lost reload reply must be recorded')
                assert generations.operation(db,operation)['phase']=='reload-intent'
                result=runtime.recover_route_addition(db,d,operation,root=root,docker=docker,authorize=authorize)
            else:result=runtime.apply_route_addition(db,d,operation,root=root,docker=docker,authorize=authorize)
            runtime.apply_route_addition(db,d,operation,root=root,docker=docker,authorize=authorize)
        assert len(signals)==1,'Recovery/replay must not repeat reload'
        assert result['container_id']==prior['container_id']
        fixture.wait_body(ports['development'],hostname,fixture.PROJECT+':development:v1')
        fixture.wait_body(ports['development'],'development.fixture.test',fixture.PROJECT+':development:v1')
        fixture.wait_body(ports['development'],'peer.development.fixture.test',fixture.PROJECT+':development:peer')
        fixture.wait_body(ports['production'],'production.fixture.test',fixture.PROJECT+':production:v1')
        assert fixture.identity(fixture.inspect(docker,prior['container_id']))==gateway_before
        assert fixture.identity(fixture.inspect(docker,apps['production']['binding']['container_id']))==production
        assert ingress_bindings.capture(db,fixture.PROJECT,'production',apps['production']['id'])==production_routes
        proof_now=verify_public_proofs(db,d,ports['development'])
        assert all(proof_now[host]==token for host,token in proof_before.items())
        current=generations.head(db,d)
        runtime._verify_generation(docker,prior['container_id'],generations.token(d,current))
        for application in apps.values():assert not fixture.inspect(docker,application['binding']['container_id'])['HostConfig']['PortBindings']
        events.append({'hostname':hostname,'generation':current['generation'],'gateway_id':prior['container_id'],
                       'port':ports['development'],'one_actual_reload':True,'lost_reply_reconciled':lost,
                       'old_routes_unchanged':True,'generation_http_verified':True,'backends_private':True,'public_proof_exact_body_headers':True,'hostname_proofs_distinct':True,'prior_proofs_preserved':True})
    assert generations.head(db,d)['generation']==initial['generation']+2
    # A real nginx -t failure restores the old file without a reload. Explicit
    # worker reconciliation refreshes the candidate and preserves the failed attempt.
    import domain_worker
    from unittest.mock import Mock
    hostname='retry.development.fixture.test';operation=str(uuid.uuid4())
    domain={'project':fixture.PROJECT,'environment':'development','hostname':hostname,
            'service_id':apps['development']['id'],'http_endpoint':d['routes'][0]['http_endpoint']}
    gateway=domain_worker.Gateway(root=root,docker=docker,compose='unused',host_client=Mock(),image=d['image'],resources=d['resources'])
    render=runtime.render_nginx;signals=[]
    def invalid_candidate(definition,**kwargs):
        text=render(definition,**kwargs)
        return text+'\ninvalid_fixture_directive;\n' if any(r['hostname']==hostname for r in definition['routes']) else text
    def count_signal(args,**kwargs):
        if args[:3]==[docker,'exec',prior['container_id']] and '-s' in args:signals.append(list(args))
        return original(args,**kwargs)
    with patch.object(runtime,'_command',side_effect=count_signal):
        with patch.object(runtime,'render_nginx',side_effect=invalid_candidate):
            try:gateway(db,domain,operation,authorize=authorize)
            except runtime.GatewayError:pass
            else:raise AssertionError('Actual invalid nginx candidate must fail configtest')
        assert generations.operation(db,operation)['phase']=='aborted'
        assert not signals
        fixture.wait_body(ports['development'],'development.fixture.test',fixture.PROJECT+':development:v1')
        runtime.verify_gateway(db,d,root=root,docker=docker,authorize=authorize)
        try:gateway(db,domain,operation,authorize=authorize)
        except ValueError:pass
        else:raise AssertionError('Normal worker invocation must not retry an aborted addition')
        assert not signals
        result=gateway(db,domain,operation,authorize=authorize,reconcile=True)
        assert result['container_id']==prior['container_id'] and len(signals)==1
    completed=generations.operation(db,operation)
    assert completed['phase']=='committed' and len(completed['document']['attempt_history'])==1
    assert completed['document']['attempt_history'][0]['document']['abort_reason']=='pre-signal-failure'
    fixture.wait_body(ports['development'],hostname,fixture.PROJECT+':development:v1')
    assert fixture.identity(fixture.inspect(docker,prior['container_id']))==gateway_before
    assert generations.head(db,d)['generation']==initial['generation']+3
    verify_public_proofs(db,d,ports['development'])
    return {'public_proof_schema':3,'unknown_host_proof_rejected':True,'sequential_additions':events,'sibling_environment_unchanged':True,
            'actual_invalid_candidate_aborted':True,'normal_retry_refused':True,
            'explicit_worker_resume_verified':True,'aborted_attempt_history_retained':True,'retry_single_reload':True}


if __name__=='__main__':
    fixture.main(edge_acceptance=acceptance)
    source=fixture.ROOT/'.runtime/ingress-runtime-acceptance.json'
    (fixture.ROOT/'.runtime/ingress-addition-acceptance.json').write_text(source.read_text())
