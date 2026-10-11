import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import ingress_host_daemon as daemon
from ingress_host_transport import HostClient

class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in ('managed','state'):(self.root/name).mkdir(mode=0o700)
        self.endpoint=socket.socket(socket.AF_UNIX);self.endpoint.bind(str(self.root/'docker.sock'));(self.root/'docker.sock').chmod(0o600);self.addCleanup(self.endpoint.close)
        for name,text in [('credentials.json','{"username":"owner"}'),('apache.conf',''),('docker',''),('policy.json',json.dumps({'managed_dir':str(self.root/'managed'),'state_dir':str(self.root/'state'),'listener':{'address':'127.0.0.1','port':8081},'routes':{}}))]:
            p=self.root/name;p.write_text(text);p.chmod(0o600)
        db=sqlite3.connect(self.root/'state.sqlite')
        for table in ('ingress_routes','ingress_definitions','ingress_operations','registry_projects','registry_environments'):db.execute('CREATE TABLE '+table+'(id)')
        db.commit();db.close();(self.root/'state.sqlite').chmod(0o600)
        self.config={'schema_version':1,'database':str(self.root/'state.sqlite'),'database_uid':os.getuid(),'credentials_file':str(self.root/'credentials.json'),'policy_file':str(self.root/'policy.json'),'socket_path':str(self.root/'helper.sock'),'allowed_uids':[os.getuid()],'socket_group_gid':None,'docker_executable':str(self.root/'docker'),'docker_socket':str(self.root/'docker.sock'),'gateway_ports':{'first':31001,'last':31003},'timeout_seconds':2,'apache':{'executable':'/usr/sbin/apache2','config':str(self.root/'apache.conf'),'pid_file':str(self.root/'apache.pid'),'listener':{'address':'127.0.0.1','port':8081},'expected_argv':['/usr/sbin/apache2','-f',str(self.root/'apache.conf'),'-DFOREGROUND'],'source_roots':[str(self.root)],'server_root':str(self.root)}}
        self.path=self.root/'daemon.json';self.save()
    def save(self):self.path.write_text(json.dumps(self.config));self.path.chmod(0o600)
    def test_closed_configuration_and_no_accidental_database_creation(self):
        self.assertEqual(daemon.load_config(self.path,owner_uid=os.getuid()),self.config)
        self.config['command']='evil';self.save()
        with self.assertRaises(ValueError):daemon.load_config(self.path,owner_uid=os.getuid())
        self.config.pop('command');self.config['database']=str(self.root/'missing.sqlite');self.save()
        with self.assertRaises(OSError):daemon.load_config(self.path,owner_uid=os.getuid())
        self.assertFalse((self.root/'missing.sqlite').exists())
    def assembly(self):
        context=patch.object(daemon,'ApacheExecutor');context.start();self.addCleanup(context.stop)
        assembly=daemon.Assembly(daemon.load_config(self.path,owner_uid=os.getuid()),owner_uid=os.getuid());self.addCleanup(assembly.close)
        return assembly
    def test_owner_rechecked_and_console_only_creation_scope(self):
        assembly=self.assembly()
        principal=assembly.resolve_principal(assembly.db,{'subject':'owner','credential_id':None})
        self.assertIn('domains:create',principal.scopes)
        (self.root/'credentials.json').write_text('{"username":"replacement"}')
        with self.assertRaises(ValueError):assembly.resolve_principal(assembly.db,{'subject':'owner','credential_id':None})
    def test_port_discovery_excludes_real_listener_and_docker_binding(self):
        assembly=self.assembly()
        listener=socket.socket();listener.bind(('127.0.0.1',0));self.addCleanup(listener.close)
        occupied=listener.getsockname()[1];assembly.config['gateway_ports']={'first':occupied,'last':occupied}
        with patch.object(daemon,'bounded_run',return_value=''):
            self.assertEqual(assembly.available_ports()['ports'],[])
        listener.close()
        with patch.object(daemon,'bounded_run',side_effect=['a'*64+'\n',json.dumps({'configured':{'80/tcp':[{'HostIp':'0.0.0.0','HostPort':'0'}]},'runtime':{'80/tcp':[{'HostIp':'0.0.0.0','HostPort':str(occupied)}]}})+'\n']):
            self.assertEqual(assembly.available_ports()['ports'],[])
        with patch.object(daemon,'bounded_run',return_value=''):
            self.assertEqual(assembly.available_ports()['ports'],[occupied])
    def test_port_rpc_closed_and_requires_provider(self):
        assembly=self.assembly()
        with self.assertRaises(ValueError):assembly.service.available_ports({'first':1})
        assembly.service._available_ports=None
        with self.assertRaises(ValueError):assembly.service.available_ports({})
    def test_real_socket_single_thread_shutdown_and_cleanup(self):
        stop=threading.Event();failures=[];previous=os.environ.get('CREDENTIALS_FILE')
        def serve():
            try:daemon.serve(self.path,owner_uid=os.getuid(),stop_event=stop,install_signals=False)
            except Exception as error:failures.append(error)
        with patch.object(daemon,'ApacheExecutor'):
            thread=threading.Thread(target=serve);thread.start()
            try:
                import time
                deadline=time.monotonic()+2
                while not Path(self.config['socket_path']).exists():
                    if failures:raise failures[0]
                    if time.monotonic()>deadline:self.fail('Socket did not open')
                    time.sleep(.01)
                client=HostClient(self.config['socket_path'],expected_server_uid=os.getuid())
                self.assertEqual(client.call('inspect',{'route_id':'a'*32})['status'],'absent')
            finally:stop.set();thread.join(3)
        self.assertFalse(thread.is_alive());self.assertFalse(failures)
        self.assertFalse(Path(self.config['socket_path']).exists());self.assertEqual(os.environ.get('CREDENTIALS_FILE'),previous)
    def test_check_does_not_open_socket(self):
        with patch.object(daemon,'ApacheExecutor'):daemon.serve(self.path,owner_uid=os.getuid(),check=True)
        self.assertFalse(Path(self.config['socket_path']).exists())

    def test_available_ports_rpc_has_no_sql_and_rejects_request_range(self):
        from ingress_host_transport import TransportError
        assembly=self.assembly();assembly.server.open();queries=[]
        assembly.db.set_trace_callback(queries.append)
        def call(request):
            result=[]
            def client():
                try:result.append(HostClient(self.config['socket_path'],expected_server_uid=os.getuid()).call('available_ports',request))
                except Exception as error:result.append(error)
            thread=threading.Thread(target=client);thread.start()
            try:assembly.server.serve_once()
            finally:thread.join(2)
            self.assertFalse(thread.is_alive());return result[0]
        with patch.object(daemon,'bounded_run',return_value=''):
            result=call({})
        self.assertEqual(set(result),{'ports','observed'})
        self.assertEqual(queries,[])
        self.assertIsInstance(call({'first':1}),TransportError)
