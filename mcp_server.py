#!/usr/bin/env python3
"""bbwatch as an MCP server: every bug-bounty program, target and change, queryable by an agent.

Local (stdio), standard library only — the agent starts it, it fetches the watcher's data and answers:

    claude mcp add bbwatch -- python3 /path/to/bbwatch/mcp_server.py

Remote (Streamable HTTP) — the box serves the same tools at /mcp (deploy/box/serve.py):

    claude mcp add --transport http bbwatch https://<box>/mcp

Data, first that answers: $BBWATCH_STATE_DIR (a local state directory), then $BBWATCH_URL (the box, live),
then the `state` branch on GitHub (the box's hourly backup). Refreshed every DATA_TTL seconds.
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from query import CATEGORIES, Data  # noqa: E402

VERSION = "1.0.0"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
BOX = os.environ.get("BBWATCH_URL", "").rstrip("/")
RAW = "https://raw.githubusercontent.com/atharva80/bbwatch/state"
DATA_TTL = int(os.environ.get("BBWATCH_TTL", "300"))

PLATFORMS = ["h1", "bugcrowd", "intigriti", "ywh", "immunefi", "cantina", "hackenproof", "sherlock", "code4rena",
             "codehawks", "standoff", "issuehunt", "bugrap", "bugbase", "huntr"]
_str_list = {"type": "array", "items": {"type": "string"}}
PAGING = {"offset": {"type": "integer", "minimum": 0, "default": 0},
          "limit": {"type": "integer", "minimum": 1, "maximum": 500}}

TOOLS = [
    {"name": "search_programs", "title": "Search bug-bounty programs",
     "description": "Find programs across 15 platforms with exact filters. Each result: platform, status, reward "
                    "range (and approx USD), listing/update dates, scope categories, target counts, hunter facts "
                    "(reputation, submission fee, reports, response rate). Defaults: accepting reports, contests "
                    "excluded, newest listed first. Use get_program for a program's full scope.",
     "inputSchema": {"type": "object", "properties": {
         "q": {"type": "string", "description": "words that must all appear in name, url, tags or any target"},
         "platform": {**_str_list, "description": f"any of {PLATFORMS}"},
         "status": {"type": "string", "enum": ["live", "any", "closed", "open", "paused", "upcoming", "judging"],
                    "default": "live", "description": "live = accepting reports now"},
         "bounty_only": {"type": "boolean", "default": False, "description": "exclude VDPs"},
         "contests": {"type": "string", "enum": ["exclude", "include", "only"], "default": "exclude"},
         "category": {**_str_list, "description": f"program has in-scope targets of any of {CATEGORIES}"},
         "min_reward_usd": {"type": "number", "description": "max advertised reward, roughly in USD"},
         "listed_within_days": {"type": "number"}, "updated_within_days": {"type": "number"},
         "scope_grew_within_days": {"type": "number", "description": "bbwatch saw new targets added"},
         "max_reputation": {"type": "integer", "description": "HackenProof minimum reputation <= this"},
         "no_submission_fee": {"type": "boolean", "default": False},
         "sort": {"type": "string", "enum": ["listed", "updated", "reward", "targets", "name"], "default": "listed"},
         **PAGING}}},
    {"name": "get_program", "title": "Get one program in full",
     "description": "Everything about one program: summary, rewards table per tier, every target (in and out of "
                    "scope) with category, bounty eligibility, severity cap, tier and its newest date (platform "
                    "added/updated, or when bbwatch saw it added), newest first; plus its change history and "
                    "target events. Accepts an id like 'h1:inspectorio' or an exact program name.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {
         "id": {"type": "string"},
         "include_out_of_scope": {"type": "boolean", "default": True},
         "history_limit": {"type": "integer", "default": 50}}}},
    {"name": "search_targets", "title": "Search targets across all programs",
     "description": "Every target of every program, filterable: substring words on the identifier (e.g. "
                    "'github.com', '*.example.com', '0x'), category, platform, bounty eligibility, scope, tier, "
                    "minimum program reward, added within N days. Newest first by default.",
     "inputSchema": {"type": "object", "properties": {
         "q": {"type": "string", "description": "words that must all appear in the target identifier"},
         "platform": _str_list, "category": {**_str_list, "description": f"any of {CATEGORIES}"},
         "bounty_only": {"type": "boolean", "default": False},
         "in_scope": {"type": ["boolean", "null"], "default": True, "description": "null = both"},
         "program_status": {"type": "string", "enum": ["live", "any"], "default": "live"},
         "min_reward_usd": {"type": "number"}, "added_within_days": {"type": "number"},
         "tier": {**_str_list, "description": "exact tier labels, e.g. 'Tier 1', 'Critical'"},
         "sort": {"type": "string", "enum": ["date", "reward", "program"], "default": "date"},
         **PAGING}}},
    {"name": "latest_targets", "title": "Recently added targets",
     "description": "In-scope targets added recently: new programs' scope, targets added to existing programs, "
                    "targets that became bounty-eligible. Platform per-target dates where published (H1, "
                    "Immunefi), otherwise when bbwatch saw the addition.",
     "inputSchema": {"type": "object", "properties": {
         "within_days": {"type": "number", "default": 7}, "platform": _str_list, "category": _str_list,
         "bounty_only": {"type": "boolean", "default": True},
         "why": {**_str_list, "description": "any of new, added, paid"}, **PAGING}}},
    {"name": "recent_changes", "title": "Recent program changes",
     "description": "The watcher's change log: new programs, scope added/reduced, reopened, paused/closed, bounty "
                    "changes, delistings, renames. Kinds: NEW, SCOPE, SCOPE-, OPEN, STATE, BOUNTY, GONE, META.",
     "inputSchema": {"type": "object", "properties": {
         "within_hours": {"type": "number", "default": 24}, "kind": _str_list, "platform": _str_list,
         "q": {"type": "string"}, **PAGING}}},
    {"name": "stats", "title": "Coverage and health",
     "description": "Programs and in-scope targets per platform, each source's health, and how fresh the data is.",
     "inputSchema": {"type": "object", "properties": {}}},
]
for t in TOOLS:
    t["annotations"] = {"readOnlyHint": True, "openWorldHint": False}

_cache = {"data": None, "at": 0, "source": None}


def _get(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": f"bbwatch-mcp/{VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
    return body if binary else json.loads(body)


def load_data():
    """The freshest complete data available, cached for DATA_TTL seconds."""
    if _cache["data"] is not None and time.time() - _cache["at"] < DATA_TTL:
        return _cache["data"]
    local = os.environ.get("BBWATCH_STATE_DIR")
    if local:
        d, src = Data.from_dir(local), f"dir:{local}"
    else:
        d = src = None
        bust = f"?v={int(time.time() // 60)}"
        for base, name in ((BOX + "/data", "box") if BOX else (None, None), (RAW, "github")):
            if not base:
                continue
            try:
                d = Data.from_bytes(_get(f"{base}/state.json.gz{bust}", binary=True),
                                    *(_get(f"{base}/{n}.json{bust}") for n in ("changes", "targets", "status")))
                src = name
                break
            except Exception as e:  # try the next source
                print(f"bbwatch-mcp: {name} unavailable: {e}", file=sys.stderr)
        if d is None:
            if _cache["data"] is not None:
                return _cache["data"]
            raise RuntimeError("no data source answered (box and GitHub)")
    _cache.update(data=d, at=time.time(), source=src)
    return d


def call_tool(name, args, data=None):
    d = data or load_data()
    args = {k: v for k, v in (args or {}).items() if v is not None or k == "in_scope"}
    fn = {"search_programs": d.search_programs, "get_program": d.get_program, "search_targets": d.search_targets,
          "latest_targets": d.latest_targets, "recent_changes": d.recent_changes, "stats": d.stats}.get(name)
    if fn is None:
        raise KeyError(name)
    out = fn(**args)
    if isinstance(out, dict):
        out.setdefault("data_updated_at", d.status.get("updated"))
    return out


def handle(msg, data=None):
    """One JSON-RPC message -> response dict, or None for a notification."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:  # notifications (initialized, cancelled…) need no answer
        return None
    try:
        if method == "initialize":
            want = params.get("protocolVersion")
            result = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "bbwatch", "title": "bbwatch bug-bounty radar", "version": VERSION},
                      "instructions": "Public bug-bounty programs from 15 platforms with full scope, tiers, rewards "
                                      "and change history, refreshed every 30 s - 3 min. Start with search_programs "
                                      "or search_targets; get_program returns a program's complete scope."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                out = call_tool(params.get("name"), params.get("arguments"), data)
                result = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                          "structuredContent": out, "isError": False}
            except KeyError:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {params.get('name')}"}}
            except TypeError as e:  # bad argument names/types: tell the agent, as a tool error it can fix
                result = {"content": [{"type": "text", "text": f"invalid arguments: {e}"}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            out = [r for r in (handle(m) for m in msg) if r] if isinstance(msg, list) else handle(msg)
        if out:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
