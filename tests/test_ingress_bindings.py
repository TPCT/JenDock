import copy
import json
import sqlite3
import unittest
import ingress_bindings as bindings

class BindingTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:',isolation_level=None);self.db.row_factory=sqlite3.Row
        bindings.initialize(self.db)
        self.db.execute('CREATE TABLE service_definitions(id,document,digest)')
        self.db.execute('CREATE TABLE managed_services(id PRIMARY KEY,project,environment,definition_id)')
        self.db.execute('CREATE TABLE service_bindings(service_id PRIMARY KEY,document)')
        self.definition={'project':'app','environment':'development','routes':[self.route('one','a'),self.route('two','b')]}
        with self.transaction():bindings.seed(self.db,self.definition)
    def tearDown(self):self.db.close()
    def transaction(self):
        import registry
        return registry.transaction(self.db)
    def route(self,sid,char):return {'hostname':sid+'.test','service_id':sid,'definition_id':'old-'+sid,'definition_digest':char*64,'container_id':char*64,'alias':'development-'+sid,'http_endpoint':{'port':8080,'health_path':'/'}}
    def capture(self,sid='one'):return bindings.capture(self.db,'app','development',sid)
    def target(self,sid='one',label='new',char='c',environment='development'):
        document={'project':'app','environment':environment,'service_id':sid,'name':sid,'artifact':{'http_endpoint':{'port':8080,'health_path':'/'}}}
        document['label']=label
        digest=bindings._digest(document)
        self.db.execute('INSERT INTO service_definitions VALUES(?,?,?)',(label,bindings._canonical(document),digest))
        return {'definition_id':label,'definition_digest':digest,'container_id':char*64,'alias':'development-'+sid,'http_endpoint':document['artifact']['http_endpoint']}
    def advance(self,snapshot,**target):
        self.db.execute('INSERT OR REPLACE INTO managed_services VALUES(?,?,?,?)',(snapshot['service_id'],snapshot['project'],snapshot['environment'],target['definition_id']))
        self.db.execute('INSERT OR REPLACE INTO service_bindings VALUES(?,?)',(snapshot['service_id'],bindings._canonical({'container_id':target['container_id'],'compose_service':target['alias'],'definition_digest':target['definition_digest']})))
        return bindings.advance(self.db,snapshot,**target)
    def test_siblings_and_exact_replay(self):
        one,two=self.capture(),self.capture('two');target=self.target();other=self.target('two','other','d')
        with self.transaction():
            result=self.advance(one,**target)
            self.advance(two,**other)
            self.assertEqual(self.advance(one,**target),result)
        self.assertEqual(self.capture()['generation'],2);self.assertEqual(self.capture('two')['generation'],2)
        with self.transaction():bindings.seed(self.db,self.definition)
        self.assertEqual(self.capture()['generation'],2)
    def test_stale_generation_and_rollback(self):
        original=self.capture();target=self.target();back=self.target(label='back',char='a')
        with self.transaction():current=self.advance(original,**target)
        with self.transaction():self.advance(current,**back)
        with self.transaction(),self.assertRaises(ValueError):self.advance(original,**target)
        self.assertEqual(self.capture()['generation'],3)
    def test_transaction_abort_preserves_generation(self):
        target=self.target();original=self.capture()
        with self.assertRaises(RuntimeError),self.transaction():
            self.advance(original,**target);raise RuntimeError()
        self.assertEqual(self.capture(),original)
    def test_endpoint_service_environment_and_transaction_fences(self):
        original=self.capture();target=self.target(environment='production')
        with self.transaction(),self.assertRaises(ValueError):self.advance(original,**target)
        with self.assertRaises(ValueError):self.advance(original,**target)
        with self.assertRaises(ValueError):bindings.preflight(original,alias='development-one',http_endpoint={'port':9000,'health_path':'/'})
        self.assertIsNone(bindings.capture(self.db,'app','production','one'))
    def test_missing_required_evidence_is_not_unrouted(self):
        self.db.execute('CREATE TABLE ingress_runtime_intents(project,environment,definition,digest)')
        self.db.execute('INSERT INTO ingress_runtime_intents VALUES(?,?,?,?)',('app','development',bindings._canonical(self.definition),bindings._digest(self.definition)))
        self.db.execute("DELETE FROM ingress_backend_bindings WHERE service_id='one'")
        with self.assertRaises(ValueError):self.capture()
        self.assertIsNone(self.capture('unrouted'))
    def test_multiple_hostnames_share_current_evidence(self):
        self.definition['routes'].append(dict(self.definition['routes'][0],hostname='alias.test'))
        self.db.execute('DELETE FROM ingress_binding_sets');self.db.execute('DELETE FROM ingress_backend_bindings')
        with self.transaction():bindings.seed(self.db,self.definition)
        target=self.target()
        with self.transaction():self.advance(self.capture(),**target)
        routes=bindings.current_routes(self.db,self.definition)
        self.assertEqual(routes[0]['container_id'],routes[2]['container_id'])
        self.assertEqual(routes[1]['container_id'],'b'*64)

    def test_orphan_evidence_and_uncommitted_target_are_rejected(self):
        target=self.target();snapshot=self.capture()
        with self.transaction(),self.assertRaisesRegex(ValueError,'committed application'):
            bindings.advance(self.db,snapshot,**target)
        self.db.execute('DELETE FROM ingress_binding_sets')
        with self.assertRaisesRegex(ValueError,'header'):bindings.current_routes(self.db,self.definition)

    def test_unchanged_current_evidence_does_not_increment_generation(self):
        target=self.target()
        with self.transaction():current=self.advance(self.capture(),**target)
        with self.transaction():self.assertEqual(self.advance(current,**target),current)
        self.assertEqual(self.capture()['generation'],2)

    def test_capture_rejects_corrupt_generation_shape_and_immutable_fields(self):
        self.db.execute('CREATE TABLE ingress_runtime_intents(project,environment,definition,digest)')
        self.db.execute('INSERT INTO ingress_runtime_intents VALUES(?,?,?,?)',('app','development',bindings._canonical(self.definition),bindings._digest(self.definition)))
        original=self.capture()['backend']
        corruptions=[dict(original,service_id='two'),dict(original,unexpected=True),
                     dict(original,container_id='invalid'),dict(original,definition_digest='invalid'),
                     dict(original,definition_id=''),dict(original,alias='development-two'),
                     dict(original,http_endpoint={'port':9000,'health_path':'/'})]
        for backend in corruptions:
            with self.subTest(backend=backend):
                self.db.execute("UPDATE ingress_backend_bindings SET backend=? WHERE service_id='one'",(bindings._canonical(backend),))
                with self.assertRaises(ValueError):self.capture()
        self.db.execute("UPDATE ingress_backend_bindings SET backend=? WHERE service_id='one'",(bindings._canonical(original),))
        for generation in (0,-1,1.5,'invalid'):
            with self.subTest(generation=generation):
                self.db.execute("UPDATE ingress_backend_bindings SET generation=? WHERE service_id='one'",(generation,))
                with self.assertRaises(ValueError):self.capture()
