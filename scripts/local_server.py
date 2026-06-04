#!/usr/bin/env python3
from __future__ import annotations

import json
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
USER_DIR = ROOT / 'user_data'
USER_FILE = USER_DIR / 'wt_roster_user_data.json'

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/load-user-data':
            try:
                if USER_FILE.exists():
                    data = json.loads(USER_FILE.read_text(encoding='utf-8'))
                    self._json(200, data if isinstance(data, dict) else {})
                else:
                    self._json(200, {})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/save-user-data':
            try:
                n = int(self.headers.get('Content-Length', '0') or '0')
                raw = self.rfile.read(min(n, 10_000_000))
                data = json.loads(raw.decode('utf-8')) if raw else {}
                if not isinstance(data, dict):
                    raise ValueError('JSON root must be an object')
                USER_DIR.mkdir(parents=True, exist_ok=True)
                tmp = USER_FILE.with_suffix('.json.tmp')
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                tmp.replace(USER_FILE)
                self._json(200, {'ok': True, 'path': str(USER_FILE.relative_to(ROOT))})
            except Exception as e:
                self._json(500, {'ok': False, 'error': str(e)})
            return
        self._json(404, {'error': 'not found'})

if __name__ == '__main__':
    httpd = ThreadingHTTPServer(('127.0.0.1', 8765), Handler)
    print('WT Roster Manager server: http://127.0.0.1:8765/index.html')
    print('Portable user data:', USER_FILE)
    httpd.serve_forever()
