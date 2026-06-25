#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
USER_DIR = ROOT / 'user_data'
USER_FILE = USER_DIR / 'wt_roster_user_data.json'


def _setup_windows_console():
    """Set a friendlier title and icon for the local server console on Windows.

    This is intentionally best-effort: if Windows blocks the icon change or the
    app is running on Linux/macOS, the server continues normally.
    """
    if not sys.platform.startswith('win'):
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW('WT Roster Manager — Local Server')
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        icon_path = ROOT / 'assets' / 'favicon.ico'
        if hwnd and icon_path.exists():
            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x00000010
            WM_SETICON = 0x0080
            ICON_SMALL = 0
            ICON_BIG = 1
            hicon = ctypes.windll.user32.LoadImageW(None, str(icon_path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
            if hicon:
                ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon)
                ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon)
    except Exception:
        pass

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
        if path == '/api/latest-wt-version':
            self._latest_wt_version()
            return
        return super().do_GET()

    def _latest_wt_version(self):
        """Return the latest public War Thunder client version from the official changelog.

        The browser UI calls this same-origin endpoint so the app can have an in-app
        button without running the heavier API updater. A direct browser fetch to
        warthunder.com is not reliable because of normal browser CORS rules.
        """
        url = 'https://warthunder.com/en/game/changelog/'
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'WT-Roster-Manager/3.82 (+local version check)',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            })
            with urllib.request.urlopen(req, timeout=12) as resp:
                html = resp.read(1_500_000).decode('utf-8', errors='replace')

            # Examples usually look like "Update 2.55.1.142". Keep the parser
            # intentionally narrow: we only need the game client version, not news.
            found = re.findall(r'Update\s+((?:\d+\.){2,}\d+)', html, flags=re.I)
            if not found:
                found = re.findall(r'\b((?:\d+\.){2,}\d+)\b', html)
            # Preserve page order; the first changelog version is normally latest.
            seen = []
            for v in found:
                if v not in seen:
                    seen.append(v)
            if not seen:
                raise ValueError('Could not find a War Thunder version number on the changelog page')
            self._json(200, {
                'ok': True,
                'latest_version': seen[0],
                'candidates': seen[:5],
                'source_url': url,
                'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            })
        except Exception as e:
            self._json(502, {
                'ok': False,
                'error': str(e),
                'source_url': url,
                'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            })

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
    _setup_windows_console()
    httpd = ThreadingHTTPServer(('127.0.0.1', 8765), Handler)
    print('WT Roster Manager server: http://127.0.0.1:8765/index.html')
    print('Portable user data:', USER_FILE)
    httpd.serve_forever()
