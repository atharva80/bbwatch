#!/usr/bin/env python3
"""Serve bbwatch's dashboard from the box itself.

    /            web/index.html (it reads its data from /data/ when not on GitHub Pages)
    /data/NAME   the state files the dashboard reads: status, changes, catalog, latest, scope-NN
    /healthz     200 while the watcher's state is fresh and pushes are going out, 503 otherwise — what the
                 wake pinger hits, so its failure emails cover a dead watcher and stuck notifications alike
    /api/…       the query engine (query.py) as JSON: programs, programs/<id>, targets, latest-targets,
                 changes, stats; GET /api lists the filters
    /mcp         the same as an MCP server (Streamable HTTP, JSON responses): mcp_server.py's tools

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
import sys
from urllib.parse import parse_qsl, unquote, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
import mcp_server  # noqa: E402
from query import Data  # noqa: E402
STATE = os.environ.get("STATE_DIR") or os.path.join(ROOT, ".state")
PORT = int(os.environ.get("PORT") or 3000)
DATA = re.compile(r"/data/((?:status|changes|catalog|latest|targets)\.json|scope-\d\d\.json|state\.json\.gz)")
API = {"/api/programs": "search_programs", "/api/targets": "search_targets", "/api/latest-targets": "latest_targets",
       "/api/changes": "recent_changes", "/api/stats": "stats"}
BOOLS = {"bounty_only", "no_submission_fee", "include_out_of_scope", "in_scope"}
_data = {"mtime": None, "data": None}


def data():
    """The query engine over the current state, rebuilt when the watcher saves a new one."""
    st = os.stat(os.path.join(STATE, "state.json.gz")).st_mtime
    if _data["mtime"] != st:
        _data.update(mtime=st, data=Data.from_dir(STATE))
    return _data["data"]


def args_from(query):
    """Query-string values to the engine's types: a,b lists stay strings; booleans and null are parsed."""
    out = {}
    for k, v in parse_qsl(query, keep_blank_values=False):
        if k in BOOLS:
            out[k] = None if v.lower() in ("null", "any", "both") else v.lower() in ("1", "true", "yes")
        else:
            out[k] = v
    return out
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
        if path == "/sw.js":
            return self.file(os.path.join(ROOT, "web", "sw.js"), "application/javascript")
        if path == "/healthz":
            try:
                with open(os.path.join(STATE, "status.json")) as f:
                    status = json.load(f)
                updated = status["updated"]
                age = time.time() - calendar.timegm(time.strptime(updated, "%Y-%m-%dT%H:%M:%SZ"))
            except Exception as e:
                return self.send(503, json.dumps({"ok": False, "error": str(e)}).encode(), "application/json")
            stuck = time.time() - status["outbox_since"] if status.get("outbox_since") else 0
            failing = sorted(k for k, s in (status.get("sources") or {}).items() if s.get("error"))
            problems = ([f"state not saved for {int(age // 60)} min"] if age >= STALE else []) + \
                       ([f"{status.get('outbox')} pushes stuck for {int(stuck // 60)} min"] if stuck >= STALE else [])
            body = {"ok": not problems, "updated": updated, "age_s": int(age), "queued_pushes": status.get("outbox", 0),
                    "failing_sources": failing, "problems": problems}
            return self.send(200 if not problems else 503, json.dumps(body).encode(), "application/json",
                             {"Cache-Control": "no-store"})
        m = DATA.fullmatch(path)
        if m:
            name = m.group(1)
            return self.file(os.path.join(STATE, name), "application/gzip" if name.endswith(".gz") else "application/json")
        if path == "/api" or path == "/api/":
            return self.json(200, {"endpoints": {
                "/api/programs": "search programs", "/api/programs/<id>": "one program in full",
                "/api/targets": "search targets across all programs", "/api/latest-targets": "recently added targets",
                "/api/changes": "change log", "/api/stats": "coverage and health", "/mcp": "MCP (Streamable HTTP)"},
                "filters": {t["name"]: sorted(t["inputSchema"]["properties"]) for t in mcp_server.TOOLS},
                "lists": "comma-separated, e.g. platform=h1,immunefi&category=contract,code"})
        if path.startswith("/api/programs/"):
            return self.query("get_program", dict(args_from(urlsplit(self.path).query), id=unquote(path[len("/api/programs/"):])))
        if path in API:
            return self.query(API[path], args_from(urlsplit(self.path).query))
        if path == "/mcp":
            return self.send(405, b"POST JSON-RPC to /mcp\n", "text/plain", {"Allow": "POST"})
        return self.send(404, b"not found\n", "text/plain")

    do_HEAD = do_GET

    def json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        head = {"Cache-Control": "no-store"}
        if len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body, head["Content-Encoding"] = gzip.compress(body, 6), "gzip"
        return self.send(code, body, "application/json", head)

    def query(self, tool, args):
        try:
            out = mcp_server.call_tool(tool, args, data())
        except TypeError as e:
            return self.json(400, {"error": "invalid_arguments", "message": str(e)})
        except Exception as e:
            return self.json(500, {"error": type(e).__name__, "message": str(e)})
        return self.json(404 if isinstance(out, dict) and out.get("error") == "not_found" else 200, out)

    def do_OPTIONS(self):
        self.send(204, b"", "text/plain", {"Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                                            "Access-Control-Allow-Headers": "Content-Type, Mcp-Session-Id, MCP-Protocol-Version"})

    def do_POST(self):
        if urlsplit(self.path).path != "/mcp":
            return self.send(404, b"not found\n", "text/plain")
        try:
            msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
        except ValueError:
            return self.json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        try:
            d = data()
        except Exception as e:
            return self.json(503, {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": f"no data: {e}"}})
        out = [r for r in (mcp_server.handle(m, d) for m in msg) if r] if isinstance(msg, list) else mcp_server.handle(msg, d)
        if not out:  # notifications only
            return self.send(202, b"", "text/plain")
        return self.json(200, out)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
