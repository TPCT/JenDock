import copy,json,unittest
from unittest.mock import patch
import application_database_networks as networks
import application_database_bindings as bindings
import registry
import test_application_database_bindings as fixtures

class DatabaseNetworkTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AttachmentTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db;self.saved=self.f.save();self.metadata=self.f.resolve(self.saved)
        self.candidate_fixture=self.f.f.f
        self.row=None;self.effects=[];self.fail=None
        self.docker='/trusted/docker'
        self.patch=patch.object(networks,'_command',side_effect=self.command);self.patch.start();self.addCleanup(self.patch.stop)
    def candidate(self,project,environment,reference,**kw):return self.candidate_fixture.resolve()
    def ensure(self):return networks.ensure_attachment(self.db,self.metadata,docker=self.docker,authorize=lambda:None,resolve_candidate=self.candidate)
    def verify(self):return networks.verify_attachment(self.db,self.metadata,docker=self.docker,authorize=lambda:None,resolve_candidate=self.candidate)
    def command(self,docker,args):
        self.assertFalse(self.db.in_transaction)
        if args[:2]==['network','ls']:return self.row['Id'] if self.row else ''
        if args[:2]==['network','inspect']:return json.dumps([self.row])
        if args[:2]==['network','create']:
            self.effects.append('create')
            if self.fail=='create-before':raise networks.NetworkError('lost')
            labels={args[i+1].split('=',1)[0]:args[i+1].split('=',1)[1] for i,v in enumerate(args) if v=='--label'}
            self.row={'Id':'f'*64,'Name':args[-1],'Labels':labels,'Driver':'bridge','Internal':True,'Scope':'local','EnableIPv6':False,'Ingress':False,'Attachable':False,'Options':{},'Containers':{}}
            if self.fail=='create-after':raise networks.NetworkError('lost')
            return self.row['Id']
        if args[:2]==['network','connect']:
            self.effects.append('connect')
            if self.fail=='connect-before':raise networks.NetworkError('lost')
            candidate=self.candidate_fixture.row
            candidate['NetworkSettings']['Networks'][self.row['Name']]={'NetworkID':self.row['Id'],'EndpointID':'e'*64,'IPAddress':'172.30.0.2','Aliases':[self.metadata['alias']]}
            self.row['Containers'][candidate['Id']]={'EndpointID':'e'*64}
            if self.fail=='connect-after':raise networks.NetworkError('lost')
            return ''
        if args[:1]==['inspect']:return json.dumps([{'Id':args[1],'Image':'sha256:'+'1'*64}])
        raise AssertionError(args)
    def test_stopping_database_and_revoked_credential_preserves_exact_identity(self):
        import application_credentials
        descriptor=self.ensure();self.f.register_pin(self.saved)
        application_credentials.revoke_credential(self.db,self.f.principal,'example','development',self.f.secret['id'],expected_revision=1)
        self.candidate_fixture.row['State']['Running']=False
        for endpoint in self.candidate_fixture.row['NetworkSettings']['Networks'].values():
            endpoint['EndpointID']='';endpoint['IPAddress']=''
        self.row['Containers'].pop(self.candidate_fixture.row['Id'])
        original=self.command
        def command(docker,args):
            if args==['inspect',self.candidate_fixture.row['Id']]:return json.dumps([self.candidate_fixture.row])
            return original(docker,args)
        with patch.object(networks,'_command',side_effect=command):
            self.assertEqual(descriptor,networks.verify_stopping_attachment(self.db,self.metadata,docker=self.docker,authorize=lambda:None))
            self.candidate_fixture.row['State']['Running']=True
            with self.assertRaises(ValueError):networks.verify_stopping_attachment(self.db,self.metadata,docker=self.docker,authorize=lambda:None)
            self.candidate_fixture.row['State']['Running']=False
            self.row['Containers']['9'*64]={'EndpointID':'8'*64}
            with self.assertRaises(ValueError):networks.verify_stopping_attachment(self.db,self.metadata,docker=self.docker,authorize=lambda:None)
            self.row['Containers'].pop('9'*64)
            self.candidate_fixture.row['NetworkSettings']['Networks']['foreign']={'NetworkID':'7'*64}
            with self.assertRaises(ValueError):networks.verify_stopping_attachment(self.db,self.metadata,docker=self.docker,authorize=lambda:None)
        self.assertEqual(self.effects,['create','connect'])

    def test_owned_bridge_replay_preserves_original_network_and_state(self):
        original=copy.deepcopy(self.candidate_fixture.row)
        descriptor=self.ensure();self.assertEqual(descriptor,self.ensure());self.assertEqual(descriptor,self.verify())
        self.assertEqual(self.effects,['create','connect'])
        self.assertEqual(self.candidate_fixture.row['State'],original['State'])
        self.assertEqual(self.candidate_fixture.row['NetworkSettings']['Networks']['default'],original['NetworkSettings']['Networks']['default'])
        self.db.execute("UPDATE registry_environments SET revision=revision+1 WHERE project='example' AND name='development'")
        self.assertEqual(descriptor,self.verify())
    def test_lost_connect_reply_recovers_observed_result_without_reconnect(self):
        self.fail='connect-after'
        with self.assertRaises(networks.NetworkError):self.ensure()
        self.fail=None;self.ensure();self.assertEqual(self.effects,['create','connect'])
    def test_lost_create_reply_recovers_nonce_owned_network(self):
        self.fail='create-after'
        with self.assertRaises(networks.NetworkError):self.ensure()
        self.fail=None;self.ensure();self.assertEqual(self.effects,['create','connect'])
    def test_unknown_absent_outcomes_never_repeat_effect(self):
        for failure in ('create-before','connect-before'):
            with self.subTest(failure=failure):
                # Separate logical binding journal for each independent scenario.
                self.db.execute('DROP TABLE IF EXISTS application_database_networks');self.row=None;self.effects=[];self.fail=failure
                with self.assertRaises(networks.NetworkError):self.ensure()
                before=list(self.effects);self.fail=None
                with self.assertRaises(networks.NetworkError):self.ensure()
                self.assertEqual(self.effects,before)
    def test_unknown_peer_alias_and_original_network_changes_rejected(self):
        self.ensure();self.row['Containers']['9'*64]={'EndpointID':'8'*64}
        with self.assertRaises(networks.NetworkError):self.verify()
        self.row['Containers'].pop('9'*64)
        self.candidate_fixture.row['NetworkSettings']['Networks']['default']['EndpointID']='9'*64
        with self.assertRaises(networks.NetworkError):self.verify()
    def test_two_authorized_apps_share_database_without_ignoring_other_networks(self):
        import uuid
        import managed_services
        self.ensure();first_metadata=self.metadata;first_row=copy.deepcopy(self.row)
        from test_application_database_bindings import managed_definition
        sid=str(uuid.uuid4());definition=managed_definition(sid,'queue')
        self.db.execute('INSERT INTO service_definitions VALUES(?,?,?,?)',(sid,registry.canonical(definition),managed_services.digest(definition),0))
        self.db.execute('INSERT INTO managed_services VALUES(?,?,?,?,?,?,?,?,?)',(sid,'example','development','queue',sid,'operation2','running',0,0))
        value=bindings.save_binding(self.db,self.f.principal,'example','development',dict(self.f.values,service_id=sid),expected_revision=0)
        pin={'binding_id':value['id'],'revision':value['revision'],'digest':value['definition_digest']}
        second=bindings.resolve_runtime(self.db,'example','development',sid,pin,authorize=lambda:None)
        rows={first_row['Name']:first_row};original=self.command
        def command(docker,args):
            if args[:2]==['network','ls']:
                row=rows.get(args[-1][6:-1]);return row['Id'] if row else ''
            if args[:2]==['network','inspect']:return json.dumps([next(r for r in rows.values() if r['Id']==args[-1])])
            if args[:2]==['network','create']:
                result=original(docker,args);self.row['Id']='2'*64;rows[self.row['Name']]=self.row;return self.row['Id']
            if args[:2]==['network','connect']:
                self.row=next(r for r in rows.values() if r['Id']==args[-2])
            return original(docker,args)
        self.metadata=second;self.row=None
        with patch.object(networks,'_command',side_effect=command):
            second_descriptor=self.ensure();self.verify()
            self.metadata=first_metadata;self.verify()
            self.metadata=second;self.assertEqual(self.verify(),second_descriptor)
            self.candidate_fixture.row['NetworkSettings']['Networks']['foreign']={'NetworkID':'7'*64,'EndpointID':'6'*64,'IPAddress':'172.40.0.2','Aliases':['foreign']}
            with self.assertRaises(networks.NetworkError):self.verify()

    def test_drifted_network_options_and_alias_fail_closed(self):
        self.ensure();self.row['Options']={'com.docker.network.bridge.enable_icc':'true'}
        with self.assertRaises(networks.NetworkError):self.verify()
        self.row['Options']={}
        self.candidate_fixture.row['NetworkSettings']['Networks'][self.row['Name']]['Aliases'].append('other-database')
        with self.assertRaises(networks.NetworkError):self.verify()

    def test_foreign_app_network_rejected_even_with_peer_callback(self):
        self.ensure();identifier='9'*64
        self.row['Containers'][identifier]={'EndpointID':'8'*64}
        actual={'Id':identifier,'Image':'sha256:'+'1'*64,'Config':{'Labels':{'net.shiftcodes.managed.service':self.metadata['service_id'],'net.shiftcodes.managed.project':'example','net.shiftcodes.managed.environment':'development'}},'NetworkSettings':{'Networks':{'example-development-managed':{},self.row['Name']:{'NetworkID':self.row['Id'],'EndpointID':'8'*64},'foreign-legacy':{}}}}
        original=self.command
        def command(docker,args):
            if args==['inspect',identifier]:return json.dumps([actual])
            return original(docker,args)
        with patch.object(networks,'_command',side_effect=command),self.assertRaises(networks.NetworkError):
            networks.verify_attachment(self.db,self.metadata,docker=self.docker,authorize=lambda:None,resolve_candidate=self.candidate,verify_application=lambda _:True)

    def test_existing_network_without_intent_is_not_adopted(self):
        self.row={'Id':'f'*64}
        with self.assertRaises(networks.NetworkError):self.ensure()
        self.assertEqual(self.effects,[])
    def test_uncertain_migration_blocks_before_any_effect(self):
        self.db.execute('CREATE TABLE job_migrations(job_id INTEGER,proof TEXT,state TEXT)')
        self.db.execute('CREATE TABLE job_migration_targets(job_id INTEGER,document TEXT)')
        target={'binding':{'project':'example','environment':'development','definition':{'ownership':self.metadata['definition']['ownership']}}}
        self.db.execute('INSERT INTO job_migrations VALUES(1,NULL,?)',('recovery-required',))
        self.db.execute('INSERT INTO job_migration_targets VALUES(1,?)',(json.dumps(target),))
        with self.assertRaises(networks.NetworkError):self.ensure()
        self.assertEqual(self.effects,[])

if __name__=='__main__':unittest.main()
