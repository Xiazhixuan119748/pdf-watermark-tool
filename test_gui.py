"""End-to-end local HTTP tests with a synthetic PDF (no personal files)."""
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import quote

from pypdf import PdfReader
from gui import LocalServer, clean_filename
from test_remove_pdf_watermark import fixture


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = LocalServer()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'synthetic.pdf'
            fixture(path)
            cls.pdf = path.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, route, content=None, headers=None):
        connection = HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=10)
        defaults = {'Origin': self.server.origin, 'X-Session-Token': self.server.token}
        if headers:
            defaults.update(headers)
        connection.request('POST' if content is not None else 'GET', self.server.base+route,
                           body=content, headers=defaults)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_page_and_static_assets(self):
        status, headers, body = self.request('')
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), body)
        self.assertIn(b'frame-ancestors', headers['Content-Security-Policy'].encode())
        for asset in ('gui.js', 'gui.css'):
            self.assertEqual(self.request(asset)[0], 200)

    def test_inspection_does_not_leak_temporary_paths(self):
        status, _, body = self.request('api/inspect?name=example.pdf', self.pdf)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertNotIn('input', data)
        self.assertEqual(sum(item['selected'] for item in data['objects']), 2)

    def test_clean_returns_downloadable_valid_pdf(self):
        status, headers, body = self.request('api/clean?name='+quote('测试.pdf'), self.pdf)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/pdf')
        self.assertEqual(headers['X-Removed-Paints'], '2')
        self.assertIn(quote('测试.clean.pdf'), headers['Content-Disposition'])
        reader = PdfReader(io.BytesIO(body))
        self.assertEqual(len(reader.pages), 2)
        self.assertNotIn('/WM', reader.pages[0]['/Resources']['/XObject'])
        self.assertIn('/Photo', reader.pages[0]['/Resources']['/XObject'])

    def test_page_selection_and_manual_names(self):
        status, _, body = self.request('api/clean?name=example.pdf&pages=1&xobject=WM', self.pdf)
        self.assertEqual(status, 200)
        reader = PdfReader(io.BytesIO(body))
        self.assertNotIn('/WM', reader.pages[0]['/Resources']['/XObject'])
        self.assertIn('/WM', reader.pages[1]['/Resources']['/XObject'])

    def test_unmatched_selection_produces_explanation(self):
        status, _, body = self.request('api/clean?xobject=Unknown&name=example.pdf', self.pdf)
        self.assertEqual(status, 422)
        self.assertIn('没有识别到', json.loads(body)['error'])

    def test_bad_input_and_invalid_pages(self):
        self.assertEqual(self.request('api/clean?name=example.pdf', b'not a PDF')[0], 400)
        self.assertEqual(self.request('api/clean?name=example.pdf&pages=99', self.pdf)[0], 400)
        self.assertEqual(self.request('api/clean?name=example.txt', self.pdf)[0], 400)

    def test_cross_origin_and_missing_session_are_rejected(self):
        self.assertEqual(self.request('api/clean', self.pdf, {'Origin': 'https://example.com'})[0], 403)
        self.assertEqual(self.request('api/clean', self.pdf, {'X-Session-Token': ''})[0], 403)
        self.assertEqual(self.request('', headers={'Host': 'example.com'})[0], 403)

    def test_paths_cannot_read_arbitrary_local_files(self):
        self.assertEqual(self.request('../remove_pdf_watermark.py')[0], 404)
        self.assertEqual(clean_filename('../../resume.pdf'), 'resume.clean.pdf')
        self.assertEqual(clean_filename('C:\\private\\resume.pdf'), 'resume.clean.pdf')


if __name__ == '__main__':
    unittest.main()
