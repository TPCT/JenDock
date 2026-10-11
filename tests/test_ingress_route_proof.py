import copy
import unittest
import ingress_runtime as runtime
import test_ingress_runtime as fixture


class RouteProofTests(unittest.TestCase):
    def definition(self):
        value=fixture.GatewayConfigurationTests().definition();value['schema_version']=3;return value
    def test_exact_plain_body_no_store_and_host_default_rejection(self):
        value=self.definition();token=runtime.expected_route_proof(value,'dev.example.test');config=runtime.render_nginx(value)
        self.assertIn('location = /.well-known/deploy-monitor/route-proof {',config)
        self.assertIn('return 200 "'+token+'";',config);self.assertIn('default_type text/plain;',config)
        self.assertIn('add_header Cache-Control "no-store" always;',config)
        self.assertIn('server { listen 8080 default_server; server_name _; return 421; }',config)
        for header in ('Route','Route-Proof','Generation','Backend'):self.assertIn('proxy_hide_header X-Deploy-Monitor-'+header+';',config)
    def test_proof_changes_with_each_backend_identity_pin(self):
        value=self.definition();original=runtime.expected_route_proof(value,'dev.example.test')
        for field,new in (('container_id','e'*64),('definition_digest','f'*64),('definition_id','other-definition'),('service_id','other-service'),('http_endpoint',{'port':8081,'health_path':'/ready'})):
            changed=copy.deepcopy(value);changed['routes'][0][field]=new
            self.assertNotEqual(original,runtime.expected_route_proof(changed,'dev.example.test'),field)
        self.assertEqual(original,runtime.expected_route_proof(copy.deepcopy(value),'dev.example.test'))
    def test_hostname_separation_and_no_normalization_of_caller_input(self):
        value=self.definition();value['routes'].append(dict(value['routes'][0],hostname='other.example.test'))
        self.assertNotEqual(runtime.expected_route_proof(value,'dev.example.test'),runtime.expected_route_proof(value,'other.example.test'))
        for host in ('DEV.EXAMPLE.TEST','dev.example.test.','foreign.example.test'):
            with self.assertRaises(ValueError):runtime.expected_route_proof(value,host)
    def test_existing_versions_do_not_implicitly_gain_new_configuration(self):
        for version in (1,2):
            value=self.definition();value['schema_version']=version
            self.assertNotIn(runtime.ROUTE_PROOF_PATH,runtime.render_nginx(value))
            with self.assertRaises(ValueError):runtime.expected_route_proof(value,'dev.example.test')
    def test_schema_three_retains_directory_mount_and_generation(self):
        value=self.definition();manifest=runtime.gateway_manifest(value,'/owned/nginx.conf','transport')
        service=manifest['services']['development-ingress']
        self.assertEqual(service['volumes'][0]['source'],'/owned')
        self.assertIn('/generation',runtime.render_nginx(value))
