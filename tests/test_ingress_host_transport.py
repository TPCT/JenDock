import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from ingress_host_transport import HostServer, HostClient, TransportError, MAX_FRAME

class Adapter:
    def __init__(self): self.calls=[]
    def inspect(self, request): self.calls.append(request);return {'status':'absent'}
    def stage_http(self, request):raise RuntimeError('/secret/path password=secret')

class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'host.sock';self.adapter=Adapter()
    def server(self, uids=None):
        server=HostServer(self.path,self.adapter,allowed_uids={os.getuid()} if uids is None else uids,owner_uid=os.getuid(),timeout=.2)
        server.open();self.addCleanup(server.close)
        thread=threading.Thread(target=server.serve_once);thread.start();self.addCleanup(thread.join,1)
        return server,thread
    def raw(self,payload):
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(1);s.connect(str(self.path));s.sendall(payload);s.shutdown(socket.SHUT_WR)
            header=s.recv(4)
            size=struct.unpack('!I',header)[0];result=b''
            while len(result)<size:result+=s.recv(size-len(result))
            return json.loads(result)
    def test_real_socket_success(self):
        _,t=self.server();result=HostClient(self.path,expected_server_uid=os.getuid(),timeout=1).call('inspect',{'route_id':'a'*32});t.join(1)
        self.assertEqual(result,{'status':'absent'});self.assertEqual(len(self.adapter.calls),1)
    def test_real_peer_uid_rejected(self):
        self.server({os.getuid()+1})
        with self.assertRaises(TransportError):HostClient(self.path,expected_server_uid=os.getuid(),timeout=1).call('inspect',{})
        self.assertFalse(self.adapter.calls)
    def test_malformed_truncated_and_oversized(self):
        for payload in (b'\x00',struct.pack('!I',4)+b'{',struct.pack('!I',MAX_FRAME+1),struct.pack('!I',1)+b'x'):
            with self.subTest(payload=payload):
                server,t=self.server();r=self.raw(payload);t.join(1);server.close()
                self.assertEqual(r['error'],'invalid_request')
        self.assertFalse(self.adapter.calls)
    def test_arbitrary_method_rejected(self):
        self.server();body=json.dumps({'method':'__getattribute__','request':{}}).encode()
        self.assertEqual(self.raw(struct.pack('!I',len(body))+body)['error'],'invalid_request')
    def test_exception_redacted(self):
        self.server()
        with self.assertRaisesRegex(TransportError,'operation_failed') as error:HostClient(self.path,expected_server_uid=os.getuid()).call('stage_http',{})
        self.assertNotIn('secret',str(error.exception))
    def test_existing_socket_not_replaced(self):
        s=socket.socket(socket.AF_UNIX);s.bind(str(self.path));self.addCleanup(s.close)
        with self.assertRaises(ValueError):HostServer(self.path,self.adapter,allowed_uids={os.getuid()},owner_uid=os.getuid()).open()

    def test_socket_permissions_and_replacement_cleanup(self):
        server=HostServer(self.path,self.adapter,allowed_uids={os.getuid()},owner_uid=os.getuid()).open()
        self.addCleanup(server.close)
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
        self.path.unlink();self.path.write_text('foreign')
        server.close();self.assertEqual(self.path.read_text(),'foreign')

    def test_unsafe_directory_and_link_rejected(self):
        self.path.parent.chmod(0o777)
        with self.assertRaises(ValueError):HostServer(self.path,self.adapter,allowed_uids={os.getuid()},owner_uid=os.getuid()).open()
        self.path.parent.chmod(0o700)
        self.path.symlink_to(self.path.parent/'missing')
        with self.assertRaises(ValueError):HostServer(self.path,self.adapter,allowed_uids={os.getuid()},owner_uid=os.getuid()).open()

    def test_deadline_and_lost_reply_never_replay(self):
        import time
        calls=[]
        def slow(request):
            calls.append(request);time.sleep(.08);return {'status':'active'}
        self.adapter.stage_http=slow
        server,t=self.server()
        with self.assertRaisesRegex(TransportError,'outcome unknown'):
            HostClient(self.path,expected_server_uid=os.getuid(),timeout=.02).call('stage_http',{'operation_id':'a'*32})
        t.join(1);self.assertEqual(len(calls),1)
        self.assertFalse(t.is_alive())

    def test_slow_incomplete_frame_releases_server(self):
        server,t=self.server()
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(1);s.connect(str(self.path));s.sendall(b'\x00')
            t.join(1);self.assertFalse(t.is_alive())
        self.assertFalse(self.adapter.calls)

    def test_duplicate_keys_rejected(self):
        self.server();body=b'{"method":"inspect","method":"verify","request":{}}'
        self.assertEqual(self.raw(struct.pack('!I',len(body))+body)['error'],'invalid_request')
        self.assertFalse(self.adapter.calls)

    def test_client_bounds_response_and_redacts_malicious_error(self):
        for response in (struct.pack('!I',MAX_FRAME+1),struct.pack('!I',1)+b'x'):
            with self.subTest(response=response):
                listener=socket.socket(socket.AF_UNIX);listener.bind(str(self.path));listener.listen()
                def reply():
                    connection,_=listener.accept()
                    with connection:
                        connection.recv(MAX_FRAME);connection.sendall(response)
                thread=threading.Thread(target=reply);thread.start()
                try:
                    with self.assertRaisesRegex(TransportError,'outcome unknown'):
                        HostClient(self.path,expected_server_uid=os.getuid()).call('inspect',{})
                finally:
                    thread.join(1);listener.close();self.path.unlink()

    def test_client_rejects_wrong_server_before_sending_payload(self):
        received=[]
        listener=socket.socket(socket.AF_UNIX);listener.bind(str(self.path));listener.listen()
        def accept():
            connection,_=listener.accept()
            with connection:
                connection.settimeout(1);received.append(connection.recv(MAX_FRAME))
        thread=threading.Thread(target=accept);thread.start()
        try:
            with self.assertRaisesRegex(TransportError,'Untrusted host helper'):
                HostClient(self.path,expected_server_uid=os.getuid()+1).call('stage_http',{'sensitive':'not sent'})
        finally:thread.join(1);listener.close()
        self.assertEqual(received,[b''])

    def test_writable_ancestor_rejected(self):
        parent=self.path.parent/'unsafe';parent.mkdir(mode=0o777);parent.chmod(0o777)
        child=parent/'safe';child.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            HostServer(child/'socket',self.adapter,allowed_uids={os.getuid()},owner_uid=os.getuid()).open()
