#!/usr/bin/env python3
"""Offline tests for the query engine and the MCP layer on a tiny synthetic state."""
import json, os, sys, tempfile
os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="bbwatch-q-"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mcp_server
from query import Data

def prog(name, state, bounty, mx, assets, **kw):
    return dict(name=name, url=f"https://x/{name}", state=state, bounty=bounty, max=mx, sig=None, assets=assets, **kw)

state = {"sources": {
    "hackenproof": {"programs": {
        "cheap": prog("Cheap SC", "open", True, "$250,000", {"smart_contract|https://github.com/a/b": [True, None, "critical"]},
                      meta={"rep": 50}, listed="2026-10-30T00:00:00Z"),
        "elite": prog("Elite SC", "open", True, "$500,000", {"smart_contract|0xabc": [True, None, "critical"]}, meta={"rep": 150}),
        "fee": prog("Fee Web", "open", True, "$5,000", {"url|fee.io": [True, None, None]}, meta={"rep": 0, "fee": "$1"})}},
    "h1": {"programs": {
        "vdp": prog("Some VDP", "open", False, None, {"url|vdp.io": [True, False, "medium"], "url|old.vdp.io": [False, False, None]}),
        "shut": prog("Closed", "closed", True, "$1,000", {"url|closed.io": [True, True, "high"]})}}}}
d = Data(state, {"entries": []}, {"entries": []}, {"updated": "2026-11-01T00:00:00Z"})
fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)
names = lambda r: [x["name"] for x in r["results"]]

check(names(d.search_programs(platform="hackenproof", max_reputation=80, no_submission_fee=True, bounty_only=True)) == ["Cheap SC"],
      "rep <= 80, no fee, bounty only")
check(sorted(names(d.search_programs(category="contract", sort="reward"))) == ["Cheap SC", "Elite SC"], "category filter")
check(names(d.search_programs(category="contract", sort="reward")) == ["Elite SC", "Cheap SC"], "reward sort")
check("Closed" not in names(d.search_programs()) and "Closed" in names(d.search_programs(status="any")), "status live vs any")
check(names(d.search_programs(bounty_only=True, platform="h1")) == [], "VDP excluded by bounty_only")
check(d.search_programs(q="github.com/a")["total"] == 1, "q matches target identifiers")
p = d.get_program("h1:vdp")
check(len(p["scope"]) == 2 and p["targets"]["out_of_scope"] == 1, "get_program includes out-of-scope targets")
check(len(d.get_program("h1:vdp", include_out_of_scope=False)["scope"]) == 1, "out-of-scope can be excluded")
check(d.get_program("Cheap SC")["id"] == "hackenproof:cheap", "get_program by exact name")
check(d.get_program("nope")["error"] == "not_found", "unknown program -> not_found")
t = d.search_targets(in_scope=None, program_status="any")
check(t["total"] == 6, f"search_targets sees every target in and out of scope ({t['total']})")
check(d.search_targets(bounty_only=True, program_status="any")["total"] == 4, "bounty-eligible targets")
check(d.search_programs(limit=1)["limit"] == 1 and d.search_programs(limit=10_000)["limit"] == 500, "paging clamps")

r = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}, d)
check(r["result"]["protocolVersion"] == "2025-03-26", "MCP negotiates a supported protocol version")
check(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, d) is None, "notifications get no reply")
r = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "search_programs", "arguments": {"platform": ["hackenproof"], "limit": 2}}}, d)
check(r["result"]["structuredContent"]["total"] == 3 and not r["result"]["isError"], "tools/call returns structured content")
r = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_program", "arguments": {"bad": 1}}}, d)
check(r["result"]["isError"], "bad arguments are a tool error, not a crash")
check(mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"}, d)["error"]["code"] == -32601, "unknown method -> -32601")
print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED"); sys.exit(1 if fails else 0)
