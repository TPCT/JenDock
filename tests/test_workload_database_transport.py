"""Real SQLite attachment/candidate/network domain, simulated bounded Docker bytes."""
import copy
import json
import time
import unittest
from unittest.mock import patch
import database_candidates
import application_database_networks as bridges
from workload_transport import WorkloadTransport, MAX_INSPECT
import test_application_database_networks as network_fixtures

class WorkloadDatabaseTransportTests(unittest.TestCase):
 def setUp(self):
  self.f=network_fixtures.DatabaseNetworkTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.descriptor=self.f.ensure();self.calls=[]
  self.c=self.f.candidate_fixture
  self.c.row['State'].update(Paused=False,Restarting=False)
  self.definition={'project':'example','environment':'development','service_id':self.f.f.service,'runtime':{'database_attachment':self.f.f.pin(self.f.saved)}}
  self.transport=WorkloadTransport(self.f.db,docker=self.f.docker,socket='unix:///tmp/exact-workload.sock',runner=self.runner)
  self.original_resolve=database_candidates.resolve
 def runner(self,argv,payload,**options):
  self.calls.append(argv)
  self.assertEqual(options['env']['DOCKER_HOST'],'unix:///tmp/exact-workload.sock')
  self.assertGreater(options['timeout'],0);self.assertLessEqual(options['timeout'],8)
  self.assertLessEqual(options['limit'],MAX_INSPECT);self.assertFalse(self.f.db.in_transaction)
  args=argv[1:]
  if args==['ps','-aq','--no-trunc']:return (self.c.row['Id']+'\n').encode()
  if args[:4]==['inspect','--type','container','--format']:return json.dumps(self.c.row).encode()+b'\n'
  if args[:2]==['volume','inspect']:return json.dumps(self.c.inventory['volumes']['data']).encode()+b'\n'
  return self.f.command(argv[0],args).encode()
 def resolve(self,db,project,environment,reference,**options):
  # Existing fixture's trusted recorded provenance; real fresh inventory/candidate
  # validation remains exercised, without requiring a host Compose installation.
  options.pop('provenance_resolver')
  return self.original_resolve(db,project,environment,reference,**options)
 def verify(self):
  with patch.object(database_candidates,'resolve',side_effect=self.resolve),patch.object(bridges,'_command',side_effect=AssertionError('Unbounded default runner used')):
   return self.transport._database_attachment(self.definition,lambda:None,time.monotonic()+8)
 def test_strong_verifier_uses_bounded_same_socket_injected_commands(self):
  with patch.object(bridges,'verify_attachment',wraps=bridges.verify_attachment) as verify:
   self.assertEqual(self.verify(),self.descriptor);self.assertEqual(verify.call_count,1)
  self.assertTrue(any(a[1:3]==['network','inspect'] for a in self.calls))
  self.assertTrue(any(a[1]=='ps' for a in self.calls))
  self.assertFalse(any(a[1]=='exec' for a in self.calls))
 def test_standalone_compose_keeps_same_socket_and_deadline(self):
  import control
  invoked=[]
  def runner(argv,payload,**options):
   if argv[0]=='/trusted/docker-compose':
    self.assertEqual(options['env']['DOCKER_HOST'],'unix:///tmp/exact-workload.sock')
    self.assertLessEqual(options['timeout'],8);self.assertEqual(options['limit'],MAX_INSPECT)
    invoked.append(argv);return b'{}'
   return self.runner(argv,payload,**options)
  self.transport.runner=runner
  def review(recipe,service,files,root,command,compose):return command([compose,'config','--format','json'])
  def resolve(db,project,environment,reference,**options):
   self.assertEqual(options['provenance_resolver']({},'db',[]),'{}')
   return self.resolve(db,project,environment,reference,**options)
  with patch.object(control,'COMPOSE','/trusted/docker-compose'),patch('compose_review.review',side_effect=review),patch.object(database_candidates,'resolve',side_effect=resolve):
   self.assertEqual(self.transport._database_attachment(self.definition,lambda:None,time.monotonic()+8),self.descriptor)
  self.assertTrue(invoked);self.assertFalse(any(a[1]=='compose' for a in self.calls))
 def test_restart_storage_and_network_drift_fail_before_exec(self):
  initial=copy.deepcopy(self.c.inventory)
  for change in ('restart','storage','network'):
   self.c.inventory=copy.deepcopy(initial);self.c.row=self.c.inventory['containers'][0];self.calls=[]
   if change=='restart':self.c.row['State']['StartedAt']='2026-10-11T01:00:00Z'
   elif change=='storage':self.c.inventory['volumes']['data']['CreatedAt']='2026-10-12'
   else:self.c.row['NetworkSettings']['Networks']['foreign']={'NetworkID':'9'*64,'EndpointID':'8'*64,'IPAddress':'172.40.0.2','Aliases':['foreign']}
   with self.subTest(change=change),self.assertRaises(ValueError):self.verify()
   self.assertFalse(any(a[1]=='exec' for a in self.calls))
if __name__=='__main__':unittest.main()
