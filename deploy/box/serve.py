#!/usr/bin/env python3
"""Serve bbwatch's dashboard from the box itself.

    /            web/index.html (it reads its data from /data/ when not on GitHub Pages)
    /data/NAME   the state files the dashboard reads: status, changes, catalog, latest, scope-NN
    /healthz     200 while the watcher's state is fresh, 503 when it is not — what the wake pinger hits

JSON is gzipped and ETagged, because egress is the free plan's scarce resource and the catalog is the
bulk of every page load. CORS is open so the GitHub Pages copy of the page can read live data here.
"""
import calendar
import gzip
import json
import os
import re
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE = os.environ.get("STATE_DIR") or os.path.join(ROOT, ".state")
PORT = int(os.environ.get("PORT") or 3000)
DATA = re.compile(r"/data/((?:status|changes|catalog|latest)\.json|scope-\d\d\.json)")
STALE = 30 * 60
_gz = {}  # (path, mtime, size) -> gzipped bytes, so repeat loads don't recompress


class Handler(BaseHTTPRequestHandler):
    server_version = "bbwatch"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def file(self, path, ctype):
        try:
            st = os.stat(path)
        except OSError:
            return self.send(404, b"not found\n", "text/plain")
        etag = f'"{int(st.st_mtime)}-{st.st_size}"'
        head = {"ETag": etag, "Cache-Control": "no-cache", "Last-Modified": formatdate(st.st_mtime, usegmt=True)}
        if self.headers.get("If-None-Match") == etag:
            return self.send(304, b"", ctype, head)
        key = (path, st.st_mtime, st.st_size)
        if "gzip" in (self.headers.get("Accept-Encoding") or ""):
            if key not in _gz:
                if len(_gz) > 64:
                    _gz.clear()
                with open(path, "rb") as f:
                    _gz[key] = gzip.compress(f.read(), 6)
            head["Content-Encoding"] = "gzip"
            head["Vary"] = "Accept-Encoding"
            return self.send(200, _gz[key], ctype, head)
        with open(path, "rb") as f:
            return self.send(200, f.read(), ctype, head)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            return self.file(os.path.join(ROOT, "web", "index.html"), "text/html; charset=utf-8")
        if path == "/healthz":
            try:
                with open(os.path.join(STATE, "status.json")) as f:
                    updated = json.load(f)["updated"]
                age = time.time() - calendar.timegm(time.strptime(updated, "%Y-%m-%dT%H:%M:%SZ"))
            except Exception as e:
                return self.send(503, json.dumps({"ok": False, "error": str(e)}).encode(), "application/json")
            ok = age < STALE
            return self.send(200 if ok else 503, json.dumps({"ok": ok, "updated": updated, "age_s": int(age)}).encode(),
                             "application/json", {"Cache-Control": "no-store"})
        m = DATA.fullmatch(path)
        if m:
            return self.file(os.path.join(STATE, m.group(1)), "application/json")
        return self.send(404, b"not found\n", "text/plain")

    do_HEAD = do_GET


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
