"""Tiny local server for the viewer (stdlib only; swappable for FastAPI later).

Routes:
  /                  viewer page
  /viewer/*          viewer modules
  /constructs/*      saved specs
  /samples/*         sample specs
  /api/ping          identifies this server (so a second `ring` reuses it)
  /api/latest        newest saved construct
  /api/constructs    list of saved constructs
"""

import json
import os
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from urllib.request import urlopen

from . import ROOT, store

PORT = int(os.environ.get("RING_PORT", "8765"))
HOST = "127.0.0.1"
STATIC_DIRS = {"viewer", "constructs", "samples"}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.path = "/viewer/index.html"
            return super().do_GET()
        if path == "/api/ping":
            return self._json({"app": "emerald-ring"})
        if path == "/api/latest":
            p = store.latest()
            return self._json(store.load(p)) if p else self._json({"error": "no constructs yet"}, 404)
        if path == "/api/constructs":
            return self._json([{"id": p.stem, "url": store.web_path(p)} for p in store.list_constructs()])
        if path.strip("/").split("/")[0] in STATIC_DIRS:
            return super().do_GET()
        self.send_error(404)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _already_running() -> bool:
    try:
        with urlopen(f"http://{HOST}:{PORT}/api/ping", timeout=1) as r:
            return json.load(r).get("app") == "emerald-ring"
    except Exception:
        return False


def url_for(spec_web_path: str | None = None) -> str:
    base = f"http://{HOST}:{PORT}/"
    return f"{base}?spec={spec_web_path}" if spec_web_path else base


_httpd: ThreadingHTTPServer | None = None


def ensure_running() -> bool:
    """Start the server in a background thread unless one is already up.

    Returns True if this process started it (and so must keep running to serve it).
    """
    global _httpd
    if _httpd is not None or _already_running():
        return _httpd is not None
    _httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=_httpd.serve_forever, daemon=True).start()
    return True


def wait_forever() -> None:
    """Keep this process's server alive until Ctrl-C."""
    if _httpd is None:
        return
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        _httpd.shutdown()
        _httpd.server_close()


def open_viewer(spec_web_path: str | None = None, block: bool = True) -> str:
    """Open the viewer in a browser, starting the server unless one is already up.

    If we started the server and block=True, serve until Ctrl-C.
    """
    url = url_for(spec_web_path)
    ensure_running()
    webbrowser.open(url)
    if block:
        wait_forever()
    return url
