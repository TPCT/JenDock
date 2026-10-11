"""Bounded Docker change metadata against a local Unix HTTP fixture."""
import json
import os
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import patch

import container_file_changes as changes
import container_files


class ContainerFileChangesTests(unittest.TestCase):
    identifier = 'a' * 64

    def response(self, body, status=200):
        requests = []
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(2)
                requests.append(self.rfile.readline(4096).decode('ascii').strip())
                while self.rfile.readline(4096) not in (b'\r\n', b'\n', b''):
                    pass
                try:
                    self.wfile.write(('HTTP/1.1 %d Fixture\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % (status,len(body))).encode())
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    # Oversized responses can be disconnected before their tail.
                    pass
        with tempfile.TemporaryDirectory() as root:
            endpoint = str(Path(root) / 'docker.sock')
            with socketserver.UnixStreamServer(endpoint, Handler) as server:
                server.timeout = 2
                thread = threading.Thread(target=server.handle_request, daemon=True)
                thread.start()
                with patch.dict(os.environ, {'DOCKER_HOST':'unix://' + endpoint}):
                    result = changes.read(self.identifier)
                thread.join(3)
                self.assertFalse(thread.is_alive(), 'Fixture server did not finish')
        self.assertEqual(requests, ['GET /containers/' + self.identifier + '/changes HTTP/1.1'])
        return result

    def test_valid_list_and_empty_list_use_exact_immutable_id(self):
        metadata = [{'Path':'/app','Kind':0}, {'Path':'/app/changed.py','Kind':0}, {'Path':'/app/new','Kind':1}]
        self.assertEqual(self.response(json.dumps(metadata).encode()), metadata)
        self.assertEqual(self.response(b'[]'), [])

    def test_response_size_and_item_count_bounds(self):
        self.assertIsNone(self.response(b' ' * 524289))
        self.assertIsNone(self.response(json.dumps([{'Path':'/a','Kind':0}] * 4097).encode()))
        self.assertEqual(len(self.response(json.dumps([{'Path':'/a','Kind':0}] * 4096).encode())), 4096)
        # The inclusive byte boundary is accepted when the body is valid JSON.
        self.assertEqual(self.response(b'[]' + b' ' * (524288-2)), [])

    def test_non_success_invalid_json_and_wrong_top_level_are_unknown(self):
        for body, status in ((b'[]',404), (b'private-daemon-error',500), (b'{broken',200),
                             (b'{"private":"data"}',200), (b'null',200)):
            with self.subTest(body=body,status=status):
                self.assertIsNone(self.response(body,status))

    def test_invalid_endpoint_or_identity_never_opens_socket(self):
        for endpoint in ('tcp://localhost:2375','https://docker.example','/var/run/docker.sock'):
            with self.subTest(endpoint=endpoint), patch.dict(os.environ, {'DOCKER_HOST':endpoint}), patch.object(changes.socket,'socket') as connect:
                self.assertIsNone(changes.read(self.identifier)); connect.assert_not_called()
        for identifier in ('', 'a'*63, 'A'*64, '../container', None, 123, {}):
            with self.subTest(identifier=identifier), patch.object(changes.socket,'socket') as connect:
                self.assertIsNone(changes.read(identifier)); connect.assert_not_called()

    def test_socket_error_is_unknown_without_echoing_details(self):
        with patch.dict(os.environ, {'DOCKER_HOST':'unix:///fixture/socket'}), patch.object(changes.socket,'socket',side_effect=OSError('private daemon path')):
            self.assertIsNone(changes.read(self.identifier))
        with patch.dict(os.environ, {'DOCKER_HOST':'unix:///fixture/socket'}), patch.object(changes.socket,'socket',side_effect=socket.timeout('private timeout')):
            self.assertIsNone(changes.read(self.identifier))

    def test_classification_consumer_validates_rows_and_preserves_change_kinds(self):
        metadata = self.response(json.dumps([{'Path':'/app','Kind':0}, {'Path':'/app/changed.py','Kind':0},
                                            {'Path':'/app/new','Kind':1}]).encode())
        browser = container_files.FileBrowser(lambda *args: {}, change_provider=lambda identifier: metadata)
        rows = browser._changes(self.identifier)
        self.assertEqual(browser._storage({},'/app','unchanged.py',rows), {'storage':'image'})
        self.assertEqual(browser._storage({},'/app','changed.py',rows), {'storage':'writable-layer'})
        self.assertEqual(browser._storage({},'/app','new/source.py',rows), {'storage':'writable-layer'})
        for metadata in ([{'Path':'/app','Kind':99}], [{'Path':42,'Kind':0}], ['invalid'], None):
            with self.subTest(metadata=metadata):
                browser.change_provider=lambda identifier: metadata
                rows=browser._changes(self.identifier)
                self.assertIsNone(rows)
                self.assertEqual(browser._storage({},'/app','source.py',rows), {'storage':'unknown'})
        record={'Mounts':[{'Destination':'/app','Type':'volume','Source':'PRIVATE-HOST-PATH','RW':False}]}
        self.assertNotIn('PRIVATE-HOST-PATH',str(browser._storage(record,'/app','source.py',None)))
        self.assertEqual(browser._storage(record,'/app','source.py',None)['storage'],'mount')

if __name__ == '__main__': unittest.main()
