#!/usr/bin/env python3
"""Local browser GUI for the PDF watermark engine (no web framework needed)."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import tempfile
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit
import webbrowser

from remove_pdf_watermark import process

MAX_UPLOAD = 100 * 1024 * 1024
ROOT = Path(__file__).resolve().parent


def clean_filename(name):
    # Treat browser-provided names as labels, never as filesystem paths.
    name = name.replace('\\', '/').rsplit('/', 1)[-1]
    name = ''.join(c for c in name if ord(c) >= 32 and c not in '\x7f')[:180]
    if not name.lower().endswith('.pdf'):
        raise ValueError('请选择 PDF 文件。')
    return (name[:-4] or 'document') + '.clean.pdf'


def make_options(query, inspect=False):
    def one(key):
        return query.get(key, [''])[0].strip()
    names = [v.strip() for v in one('xobject').split(',') if v.strip()]
    patterns = [v.strip() for v in one('pattern').split(',') if v.strip()]
    return SimpleNamespace(
        pages=one('pages') or None, inspect=inspect, dry_run=False,
        auto=False, xobject=names, sha256=[], pattern=patterns,
        watermark_annots=one('annotations') == '1', force=False,
    )


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address=('127.0.0.1', 0)):
        super().__init__(address, Handler)
        self.token = secrets.token_urlsafe(32)
        self.authority = f'127.0.0.1:{self.server_address[1]}'
        self.origin = f'http://{self.authority}'
        self.base = f'/{self.token}/'
        self.url = self.origin + self.base


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # File labels and session tokens should not be printed in logs.

    def respond(self, status, body, content_type='application/json; charset=utf-8', extra=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                         "frame-ancestors 'none'; base-uri 'none'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def error(self, status, message):
        self.respond(status, json.dumps({'error': message}, ensure_ascii=False).encode())

    def permitted(self, mutation=False):
        if self.headers.get('Host') != self.server.authority:
            self.error(403, '无效的本地访问地址。')
            return False
        if mutation and (self.headers.get('Origin') != self.server.origin
                         or not secrets.compare_digest(self.headers.get('X-Session-Token', ''), self.server.token)):
            self.error(403, '请从启动时打开的本地窗口操作。')
            return False
        return True

    def do_GET(self):
        if not self.permitted():
            return
        path = urlsplit(self.path).path
        route = path.removeprefix(self.server.base) if path.startswith(self.server.base) else None
        if route == '':
            page = (ROOT/'gui.html').read_text(encoding='utf-8').replace('{{TOKEN}}', self.server.token)
            self.respond(200, page.encode(), 'text/html; charset=utf-8')
        elif route in ('gui.js', 'gui.css'):
            mime = 'text/javascript; charset=utf-8' if route.endswith('.js') else 'text/css; charset=utf-8'
            self.respond(200, (ROOT/route).read_bytes(), mime)
        else:
            self.error(404, '页面不存在。')

    def do_POST(self):
        if not self.permitted(mutation=True):
            return
        url = urlsplit(self.path)
        route = url.path.removeprefix(self.server.base) if url.path.startswith(self.server.base) else None
        if route not in ('api/inspect', 'api/clean'):
            self.error(404, '操作不存在。')
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_UPLOAD:
                self.error(413, '请选择不超过 100 MB 的 PDF。')
                return
            self.connection.settimeout(60)
            content = self.rfile.read(length)
            if len(content) != length:
                self.error(400, '文件传输未完成，请重新选择。')
                return
            if b'%PDF-' not in content[:1024]:
                self.error(400, '该文件不是有效的 PDF。')
                return
            query = parse_qs(url.query)
            output_name = clean_filename(query.get('name', ['document.pdf'])[0])
            options = make_options(query, inspect=route == 'api/inspect')
            with tempfile.TemporaryDirectory(prefix='pdf-watermark-') as directory:
                source, output = Path(directory)/'input.pdf', Path(directory)/'output.pdf'
                source.write_bytes(content)
                report = process(source, output, options)
                if options.inspect:
                    report.pop('input', None)
                    report.pop('output', None)
                    self.respond(200, json.dumps(report, ensure_ascii=False).encode())
                elif not report['written']:
                    self.error(422, '没有识别到可自动去除的水印。可以在“高级选项”中查看并指定水印对象。')
                else:
                    self.respond(200, output.read_bytes(), 'application/pdf', {
                        'Content-Disposition': f"attachment; filename=clean.pdf; filename*=UTF-8''{quote(output_name)}",
                        'X-Removed-Paints': str(report['removed_paints']),
                    })
        except (ValueError, FileNotFoundError) as exc:
            self.error(400, str(exc))
        except Exception:
            self.error(400, 'PDF 无法处理，可能已损坏、加密或使用了不支持的水印结构。')


def main():
    parser = argparse.ArgumentParser(description='启动本地 PDF 去水印图形界面')
    parser.add_argument('--port', type=int, default=0, help='本地端口，默认自动分配')
    parser.add_argument('--no-browser', action='store_true', help='仅打印本地链接，不自动打开浏览器')
    args = parser.parse_args()
    server = LocalServer(('127.0.0.1', args.port))
    print(f'本地图形界面：{server.url}', flush=True)
    print('文件仅在本机处理。按 Ctrl+C 关闭服务。', flush=True)
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
