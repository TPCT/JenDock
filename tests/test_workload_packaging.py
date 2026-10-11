"""Deployment configuration only; no Docker effects or runtime acceptance claims."""
from pathlib import Path
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[1]
class WorkloadPackagingTests(unittest.TestCase):
 def setUp(self):
  self.base=yaml.safe_load((ROOT/'compose.yml').read_text())['services']
  self.mcp=yaml.safe_load((ROOT/'compose.mcp.yml').read_text())['services']
 def test_console_channel_explicit_peer_opt_in(self):
  self.assertEqual(self.base['worker']['environment']['WORKLOAD_RECEIVER_UID'],'${WORKLOAD_RECEIVER_UID:-}')
  self.assertEqual(self.base['receiver']['environment']['WORKLOAD_WORKER_UID'],'0')
  self.assertEqual(self.base['worker']['user'],'0:65534')
  self.assertIn('USER 65534:65534',(ROOT/'Dockerfile').read_text())
  for name in ('receiver','worker'):self.assertIn('state:/state',self.base[name]['volumes'])
 def test_mcp_channel_independent_opt_in_and_existing_socket(self):
  self.assertEqual(self.mcp['worker']['environment']['WORKLOAD_MCP_UID'],'${WORKLOAD_MCP_UID:-}')
  env=self.mcp['mcp']['environment']
  self.assertEqual(env['MCP_WORKLOAD_SOCKET'],'${MCP_WORKLOAD_SOCKET:-}')
  self.assertEqual(env['WORKLOAD_WORKER_UID'],'0')
  self.assertIn('state:/state',self.mcp['mcp']['volumes'])
  self.assertNotIn('MCP_DATABASE_SOCKET',env)
  self.assertNotIn('DATABASE_MCP_UID',self.mcp['worker']['environment'])
 def test_config_does_not_create_public_control_port_or_expose_docker(self):
  for service in (self.base['receiver'],self.mcp['mcp']):
   self.assertTrue(all(port.startswith('127.0.0.1:') for port in service['ports']))
   self.assertNotIn('docker-isolation.sock',str(service['volumes']))
if __name__=='__main__':unittest.main()
