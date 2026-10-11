import copy
import unittest
import ingress_runtime as ingress


class GatewayConfigurationTests(unittest.TestCase):
    def definition(self):
        return {'schema_version':1,'project':'app','environment':'development','operation_id':'gateway-operation-12345',
            'image':'nginxinc/nginx-unprivileged@sha256:'+'a'*64,'image_id':'sha256:'+'a'*64,
            'port':19080,'resources':{'memory_mib':64,'cpu':.25},
            'application_network_id':'b'*64,'application_network_name':'app-development-managed',
            'routes':[{'hostname':'dev.example.test','service_id':'service-id','definition_id':'definition-id',
                'definition_digest':'c'*64,'container_id':'d'*64,'alias':'development-web',
                'http_endpoint':{'port':8080,'health_path':'/ready'}}]}

    def test_exact_host_dynamic_dns_default_rejection_and_sanitized_headers(self):
        config=ingress.render_nginx(self.definition())
        self.assertIn('resolver 127.0.0.11',config)
        self.assertIn('development-web:8080 resolve',config)
        self.assertIn('server_name dev.example.test;',config)
        self.assertIn('return 421;',config)
        self.assertIn('proxy_set_header X-Forwarded-For $remote_addr;',config)
        self.assertNotIn('$http_host',config)

    def test_closed_definition_rejects_arbitrary_upstreams_and_injection(self):
        for field,value in [('hostname','x;include /etc/passwd;'),('alias','http://host'),
                            ('http_endpoint',{'port':80,'health_path':'//host'})]:
            definition=self.definition();definition['routes'][0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):ingress.render_nginx(definition)
        definition=self.definition();definition['image']='nginx:latest'
        with self.assertRaises(ValueError):ingress.render_nginx(definition)

    def test_gateway_manifest_has_only_two_networks_loopback_port_and_exact_limits(self):
        definition=self.definition()
        manifest=ingress.gateway_manifest(definition,'/managed/app/development/ingress/nginx.conf','transport-id')
        service=manifest['services']['development-ingress']
        self.assertEqual(set(service['networks']),{'application','transport'})
        self.assertEqual(service['ports'],[{'target':8080,'published':'19080','host_ip':'127.0.0.1','protocol':'tcp'}])
        self.assertEqual(service['cap_drop'],['ALL']);self.assertTrue(service['read_only'])
        self.assertEqual(service['user'],'101:101')
        self.assertEqual(service['mem_limit'],'64m');self.assertEqual(service['cpus'],.25)
        self.assertEqual(service['memswap_limit'],'64m')
        self.assertEqual(len(service['volumes']),1)
        self.assertTrue(service['volumes'][0]['read_only'])
        self.assertNotIn('privileged',service)

    def test_no_gateway_members_lookup_is_read_only(self):
        import sqlite3
        db=sqlite3.connect(':memory:',isolation_level=None);db.row_factory=sqlite3.Row
        statements=[];db.set_trace_callback(statements.append)
        self.assertEqual(ingress.verified_network_members(db,'app','development',docker='docker',application_network_id='b'*64),frozenset())
        self.assertFalse(any(statement.lstrip().upper().startswith(('CREATE','INSERT','UPDATE','DELETE','BEGIN')) for statement in statements))
        db.close()

    def test_intent_replay_rejects_changed_definition_and_preserves_unknown_creation(self):
        import sqlite3
        from pathlib import Path
        db=sqlite3.connect(':memory:',isolation_level=None);db.row_factory=sqlite3.Row
        definition=self.definition();journal={'create_started':True}
        ingress._save(db,definition,Path('/managed'),journal,'container-intent')
        self.assertEqual(ingress._load(db,definition,Path('/managed')),journal)
        changed=copy.deepcopy(definition);changed['port']+=1
        with self.assertRaises(ingress.GatewayError):ingress._save(db,changed,Path('/managed'),{},'prepared')
        self.assertEqual(ingress._load(db,definition,Path('/managed')),journal)
        db.close()

    def test_transport_network_rejects_unknown_peer_even_with_matching_labels(self):
        definition=self.definition()
        network={'Id':'e'*64,'Name':'app-development-ingress-transport','Driver':'bridge','Internal':False,
                 'EnableIPv6':False,'Options':{},'Labels':ingress._labels(definition,'transport'),'Containers':{}}
        ingress._transport(network,definition)
        network['Containers']={'f'*64:{}}
        with self.assertRaises(ingress.GatewayError):ingress._transport(network,definition)

    def test_health_probe_total_deadline_bounds_large_route_batch(self):
        from unittest.mock import patch
        definition=self.definition();definition['routes']*=100
        clock=[0.0];timeouts=[]
        def probe(*args,timeout):
            timeouts.append(timeout);clock[0]+=timeout;return 200
        with patch.object(ingress.time,'monotonic',side_effect=lambda:clock[0]),\
             patch.object(ingress.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)),\
             patch.object(ingress,'_json',return_value=[{'State':{'Health':{'Status':'healthy'}}}]),\
             patch.object(ingress,'_http_status',side_effect=probe):
            with self.assertRaises(ingress.GatewayError):ingress._wait_http(definition,'docker','d'*64,lambda:None)
        self.assertLessEqual(sum(timeouts),20)
        self.assertLess(len(timeouts),100)

    def test_read_only_paths_do_not_recreate_missing_configuration(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises(ingress.GatewayError):ingress._paths(root,self.definition())
            self.assertEqual(list(root.iterdir()),[])

    def test_http_probe_keeps_input_open_for_delayed_proxy_response(self):
        import subprocess
        import sys
        from unittest.mock import patch
        real_popen=subprocess.Popen
        script="import sys,select,time;[sys.stdin.readline() for _ in range(4)];ready=select.select([sys.stdin],[],[],0.05)[0];sys.exit(1) if ready and sys.stdin.read(1)=='' else None;time.sleep(0.05);sys.stdout.write('HTTP/1.1 200 OK\\r\\n\\r\\n');sys.stdout.flush()"
        def delayed(*args,**kwargs):return real_popen([sys.executable,'-c',script],**kwargs)
        with patch.object(ingress.subprocess,'Popen',side_effect=delayed):
            self.assertEqual(ingress._http_status('docker','d'*64,'dev.example.test','/ready',timeout=2),200)

    def test_bound_reservations_reject_another_container_identity(self):
        import sqlite3
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.executescript('CREATE TABLE ingress_capacity(project,environment,operation_id,memory_mib,cpu,container_id); CREATE TABLE ingress_ports(project,environment,operation_id,port,container_id);')
        definition=self.definition()
        db.execute('INSERT INTO ingress_capacity VALUES(?,?,?,?,?,?)',('app','development',definition['operation_id'],64,.25,'d'*64))
        db.execute('INSERT INTO ingress_ports VALUES(?,?,?,?,?)',('app','development',definition['operation_id'],19080,'d'*64))
        ingress._capacity(db,definition,'d'*64)
        with self.assertRaises(ingress.GatewayError):ingress._capacity(db,definition,'e'*64)
        db.close()

    def test_backend_verification_uses_current_effective_caps(self):
        from unittest.mock import patch
        endpoint={'port':8080,'health_path':'/ready'}
        definition={'artifact_id':'artifact','artifact':{'http_endpoint':endpoint}}
        binding={'container_id':'d'*64,'compose_files':['/managed/compose.yaml'],'network_ids':['b'*64],
                 'image_id':'sha256:'+'a'*64,'container_name':'app-development-web-1','compose_service':'development-web'}
        service={'project':'app','environment':'development','definition':definition,'binding':binding,
                 'operation_id':'operation','definition_id':'definition-id'}
        network={'Id':'b'*64,'Name':'app-development-managed','Containers':{'d'*64:{}}}
        actual={'Id':'d'*64,'State':{'Running':True,'Health':{'Status':'healthy'}},
                'NetworkSettings':{'Networks':{network['Name']:{'Aliases':['development-web']}}}}
        caps={'memory_mib':128,'cpu':.5}
        with patch.object(ingress.managed_services,'get',return_value=service),\
             patch.object(ingress.managed_services,'artifact_for_service',return_value=definition['artifact']),\
             patch.object(ingress.managed_services,'environment_definitions',return_value=[{'id':'service-id','effective_resources':caps}]),\
             patch.object(ingress,'_json',side_effect=[[actual],[network],[actual]]),\
             patch.object(ingress.service_provisioning,'_verify') as verify,\
             patch.object(ingress.service_provisioning,'_network'):
            ingress._backend(None,'app','development','service-id','docker')
        self.assertEqual(verify.call_args.args[-1],caps)

    def test_application_membership_rejects_unknown_peer_before_effects(self):
        from unittest.mock import patch
        definition=self.definition()
        network={'Id':'b'*64,'Name':'app-development-managed','Internal':True,
                 'Labels':{ingress.service_provisioning.PREFIX+'project':'app',ingress.service_provisioning.PREFIX+'environment':'development'},
                 'Containers':{'d'*64:{},'e'*64:{}}}
        with patch.object(ingress,'_json',return_value=[network]),\
             patch.object(ingress.managed_services,'environment_definitions',return_value=[{'binding':{'container_id':'d'*64}}]):
            with self.assertRaises(ingress.GatewayError):ingress._application_membership(None,definition,'docker')
            ingress._application_membership(None,definition,'docker','e'*64)
            network['Containers']['f'*64]={}
            with self.assertRaises(ingress.GatewayError):ingress._application_membership(None,definition,'docker','e'*64)

    def test_matching_existing_resources_require_persisted_creation_intent(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        definition=self.definition()
        for kind in ('network','container'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                journal={} if kind=='network' else {'network_started':True,'transport_id':'e'*64}
                network={'Id':'e'*64}
                with patch.object(ingress,'_capacity'),patch.object(ingress,'_recheck_backends'),\
                     patch.object(ingress,'_application_membership'),patch.object(ingress,'_paths',return_value=(root,root/'nginx.conf',root/'compose.yaml')),\
                     patch.object(ingress,'_load',return_value=journal),patch.object(ingress,'_save'),\
                     patch.object(ingress,'_write_once'),patch.object(ingress,'_transport'),\
                     patch.object(ingress,'_find',side_effect=[network] if kind=='network' else [network,{'Id':'d'*64}]),\
                     patch.object(ingress,'_command') as command:
                    with self.assertRaisesRegex(ingress.GatewayError,'creation intent'):
                        ingress.apply_gateway(None,definition,root=root,docker='docker',compose='compose',authorize=lambda:None)
                    command.assert_not_called()
