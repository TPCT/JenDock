"""Route chunks are served without exposing arbitrary files or symlink targets."""
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from server import handler


class StaticAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'web/assets').mkdir(parents=True)
        (self.root / 'web/assets/ContainerPage-AbC_123.js').write_text('export default 42;')
        (self.root / 'outside.js').write_text('private')
        (self.root / 'web/assets/escape.js').symlink_to(self.root / 'outside.js')
        self.patch = patch('server.__file__', str(self.root / 'server.py'))
        self.patch.start()
        app = SimpleNamespace(session=lambda *args, **kwargs: None, web_root=(self.root / 'web').resolve())
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), handler(app))
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.http.server_port}'

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.patch.stop()
        self.temp.cleanup()

    def test_route_chunk_has_javascript_mime_and_exact_bytes(self):
        for prefix in ('', '/_deploy'):
            with self.subTest(prefix=prefix), urlopen(self.url + prefix + '/assets/ContainerPage-AbC_123.js?v=1') as response:
                self.assertEqual(response.headers.get_content_type(), 'text/javascript')
                self.assertEqual(response.read(), b'export default 42;')

    def test_missing_nested_encoded_and_external_assets_return_404(self):
        for path in ('/assets/missing.js', '/assets/escape.js', '/assets/../outside.js',
                     '/assets/%2e%2e/outside.js', '/assets/nested/chunk.js',
                     '/assets/chunk.js.map', '/server.py'):
            with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                urlopen(self.url + path)
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()
