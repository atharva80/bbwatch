#!/usr/bin/env python3
"""bbwatch's query engine: every program, every target, every change, with exact filters.

One engine behind every way an agent or a script asks: the REST API and the remote MCP endpoint on the
box (deploy/box/serve.py) and the local MCP server (mcp_server.py). It reads the watcher's complete
state — not the trimmed dashboard files — so nothing the watcher knows is left out: out-of-scope
targets, tiers, reward tables, per-target added/updated dates, the change log and the targets log.

    from query import Data
    d = Data.from_dir(".state")            # or Data(state, changes, targets, status)
    d.search_programs(platform=["h1"], bounty_only=True, listed_within_days=7)
    d.get_program("h1:inspectorio")
    d.search_targets(q="github.com", category=["code", "contract"], bounty_only=True)

Every list result is {"total", "offset", "limit", "results"}; dates are ISO 8601 UTC.
"""
import datetime as dt
import gzip
import json
import os

import dashboard as db

LIVE = {"open", "active", "live"}          # accepting reports now
SOON = {"upcoming"}
OVER = {"ended", "closed", "suspended", "archived", "finished"}
CATEGORIES = sorted(set(db.TYPE_CAT.values()) | {"wildcard", "other"})
SORTS = ("listed", "updated", "reward", "targets", "name")
MAX_LIMIT = 500


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def _list(v):
    """Accept "a,b", ["a","b"] or None -> lowercase set (empty = no filter)."""
    if not v:
        return set()
    items = v if isinstance(v, (list, tuple, set)) else str(v).split(",")
    return {str(x).strip().lower() for x in items if str(x).strip()}


def _page(rows, offset, limit):
    offset = max(0, int(offset or 0))
    limit = max(1, min(MAX_LIMIT, int(limit or 50)))
    return {"total": len(rows), "offset": offset, "limit": limit, "results": rows[offset:offset + limit]}


def _words(q):
    return [w for w in str(q or "").lower().split() if w]


def _paid(a, r):
    return a[1] is True or (a[1] is None and bool(r.get("bounty")))


class Data:
    def __init__(self, state, changes=None, targets=None, status=None):
        self.state, self.status = state or {}, status or {}
        self.changes = (changes or {}).get("entries") or []
        self.targets = (targets or {}).get("entries") or []
        self.now = dt.datetime.now(dt.timezone.utc).timestamp()
        self.catalog = {r["id"]: r for r in db.build_catalog(self.state)}
        self.raw = {f"{p}:{k}": (p, k, r)
                    for p, s in (self.state.get("sources") or {}).items() if p != "mirror" and isinstance(s, dict)
                    for k, r in (s.get("programs") or {}).items() if isinstance(r, dict)}
        self.seen = {}  # (platform, key, asset) -> when the watcher saw it added
        for e in self.targets:
            t = db.ts(e.get("ts"))
            k = (e.get("src"), e.get("key"), e.get("asset"))
            if t and e.get("why") in ("new", "added", "paid", "readded") and t > self.seen.get(k, 0):
                self.seen[k] = t

    @classmethod
    def from_dir(cls, state_dir):
        status, changes, state, targets = db.load(state_dir)
        return cls(state, changes, targets, status)

    @classmethod
    def from_bytes(cls, state_gz, changes=None, targets=None, status=None):
        return cls(json.loads(gzip.decompress(state_gz)), changes, targets, status)

    # ---------- shapes ----------

    def program_summary(self, c):
        m = c.get("m") or {}
        plat, key, r = self.raw[c["id"]]
        meta = r.get("meta") or {}
        assets = r.get("assets") or {}
        return {
            "id": c["id"], "platform": c["p"], "platform_name": db.LABELS.get(c["p"], c["p"]), "name": c["n"],
            "url": c["u"], "status": c["s"], "accepting_reports": c["s"] in LIVE, "bounty": bool(c["b"]),
            "contest": bool(c["c"]), "contest_window": c["w"] or None,
            "reward": {"min": c["mn"] or None, "max": c["mx"] or None, "max_usd_approx": c["usd"] or None},
            "listed_at": iso(c["L"]), "listed_at_source": "first seen by bbwatch" if c["Lw"] else "platform",
            "updated_at": iso(c["U"]), "first_seen_at": iso(c["F"]), "scope_grew_at": iso(c["S"]),
            "scope_categories": c["ty"], "tags": c["t"],
            "targets": {"in_scope": c["na"], "bounty_eligible": c["np"],
                        "out_of_scope": sum(1 for v in assets.values() if v and not v[0])},
            "hunter_facts": {k: v for k, v in (("reputation_required", meta.get("rep")),
                                               ("submission_fee", meta.get("fee")),
                                               ("resolved_reports", m.get("reports") or meta.get("reports")),
                                               ("response_efficiency_pct", m.get("resp"))) if v is not None},
        }

    def _rows(self, pid):
        """Every asset of one program, as dashboard.build_scope computes it (tier, freshest date).
        Built for all programs at once on first use (~0.5 s), then a lookup."""
        if not hasattr(self, "_scope"):
            self._scope = {}
            for shard in db.build_scope(self.state, {"entries": self.targets}):
                self._scope.update(shard)
        return self._scope.get(pid) or {"a": []}

    # ---------- programs ----------

    def search_programs(self, q=None, platform=None, status="live", bounty_only=False, contests="exclude",
                        category=None, min_reward_usd=0, listed_within_days=None, updated_within_days=None,
                        scope_grew_within_days=None, max_reputation=None, no_submission_fee=False,
                        sort="listed", offset=0, limit=50):
        plats, cats, ws = _list(platform), _list(category), _words(q)
        status = (status or "any").lower()
        cut = lambda days: self.now - float(days) * 86400 if days not in (None, "", 0, "0") else None
        lc, uc, sc = cut(listed_within_days), cut(updated_within_days), cut(scope_grew_within_days)
        rows = []
        for c in self.catalog.values():
            if plats and c["p"] not in plats:
                continue
            if contests == "exclude" and c["c"] or contests == "only" and not c["c"]:
                continue
            if status == "live" and c["s"] not in LIVE and not (c["c"] and c["s"] in SOON):
                continue
            if status == "closed" and c["s"] not in OVER:
                continue
            if status not in ("live", "closed", "any") and c["s"] != status:
                continue
            if bounty_only and not c["b"]:
                continue
            if cats and not cats & set(c["ty"]):
                continue
            if min_reward_usd and c["usd"] < float(min_reward_usd):
                continue
            if lc and not c["L"] >= lc or uc and not c["U"] >= uc or sc and not c["S"] >= sc:
                continue
            rep = (c.get("m") or {}).get("rep")
            if max_reputation not in (None, "") and rep is not None and rep > int(max_reputation):
                continue
            if no_submission_fee and c["fee"]:
                continue
            if ws:
                plat, key, r = self.raw[c["id"]]
                hay = " ".join([c["n"], c["p"], db.LABELS.get(c["p"], ""), c["u"], " ".join(c["t"])]
                               + [k.partition("|")[2] for k in (r.get("assets") or {})]).lower()
                if not all(w in hay for w in ws):
                    continue
            rows.append(c)
        key = {"listed": lambda c: (-c["L"], -c["usd"]), "updated": lambda c: (-c["U"], -c["L"]),
               "reward": lambda c: (-c["usd"], -c["L"]), "targets": lambda c: (-c["na"], -c["usd"]),
               "name": lambda c: c["n"].lower()}.get(sort, lambda c: (-c["L"], -c["usd"]))
        rows.sort(key=key)
        page = _page(rows, offset, limit)
        page["results"] = [self.program_summary(c) for c in page["results"]]
        return page

    def get_program(self, id, include_out_of_scope=True, history_limit=50):
        if id not in self.catalog:
            matches = [pid for pid, c in self.catalog.items() if id.lower() in (pid.lower(), c["n"].lower())]
            if len(matches) != 1:
                return {"error": "not_found" if not matches else "ambiguous", "id": id,
                        "candidates": matches[:20]}
            id = matches[0]
        c = self.catalog[id]
        plat, key, r = self.raw[id]
        entry = self._rows(id)
        kinds = {"u": "platform updated", "a": "platform added", "s": "seen added by bbwatch", "": None}
        scope = [{"target": x[0], "category": x[1], "type": x[2], "in_scope": bool(x[3]),
                  "bounty_eligible": bool(x[4]), "severity": x[5] or None, "tier": x[6] or None,
                  "date": iso(x[7]), "date_kind": kinds[x[8]]}
                 for x in entry["a"] if include_out_of_scope or x[3]]
        history = [{"at": e.get("ts"), "kind": e.get("kind"), "what": e.get("what") or e.get("title"),
                    "detail": e.get("body")} for e in self.changes if e.get("url") and e.get("url") == r.get("url")]
        target_events = [{"at": e.get("ts"), "event": e.get("why"), "target": e.get("asset"), "type": e.get("type"),
                          "bounty_eligible": e.get("paid")}
                         for e in self.targets if e.get("src") == plat and e.get("key") == key]
        out = self.program_summary(c)
        out.update({
            "tier_kind": entry.get("tk"),
            "rewards_table": [dict(zip(("tier", "critical", "high", "medium", "low"), row)) for row in entry.get("rt") or []],
            "scope": scope,
            "history": history[:history_limit],
            "target_events": target_events[:history_limit],
        })
        return out

    # ---------- targets ----------

    def search_targets(self, q=None, platform=None, category=None, bounty_only=False, in_scope=True,
                       program_status="live", min_reward_usd=0, added_within_days=None, tier=None,
                       sort="date", offset=0, limit=100):
        plats, cats, ws, tiers = _list(platform), _list(category), _words(q), _list(tier)
        cut = self.now - float(added_within_days) * 86400 if added_within_days not in (None, "", 0, "0") else None
        rows = []
        for pid, c in self.catalog.items():
            if plats and c["p"] not in plats:
                continue
            if program_status == "live" and c["s"] not in LIVE:
                continue
            if min_reward_usd and c["usd"] < float(min_reward_usd):
                continue
            plat, key, r = self.raw[pid]
            assets = r.get("assets") or {}
            if not assets:
                continue
            detail = {x[0]: x for x in self._rows(pid)["a"]}
            for k, a in assets.items():
                typ, _, ident = k.partition("|")
                if in_scope is not None and bool(a[0]) != bool(in_scope):
                    continue
                cat = db.type_cat(typ, ident)
                if cats and cat not in cats:
                    continue
                paid = _paid(a, r)
                if bounty_only and not paid:
                    continue
                if ws and not all(w in ident.lower() for w in ws):
                    continue
                x = detail.get(ident)
                t, date = (x[6] if x else ""), (x[7] if x else 0)
                if tiers and str(t).lower() not in tiers:
                    continue
                if cut and not date >= cut:
                    continue
                rows.append({"target": ident, "category": cat, "type": typ, "in_scope": bool(a[0]),
                             "bounty_eligible": paid, "severity": a[2] or None, "tier": t or None,
                             "date": iso(date), "program_id": pid, "program": c["n"], "platform": c["p"],
                             "program_status": c["s"], "program_reward_max": c["mx"] or None, "url": c["u"]})
        if sort == "date":
            rows.sort(key=lambda x: (x["date"] or "", x["program_reward_max"] or ""), reverse=True)
        elif sort == "reward":
            rows.sort(key=lambda x: -self.catalog[x["program_id"]]["usd"])
        else:
            rows.sort(key=lambda x: (x["program"].lower(), x["target"]))
        return _page(rows, offset, limit)

    def latest_targets(self, within_days=7, platform=None, category=None, bounty_only=True, why=None,
                       offset=0, limit=100):
        plats, cats, whys = _list(platform), _list(category), _list(why)
        since = self.now - float(within_days or 7) * 86400
        rows = [e for e in db.build_latest(self.state, {"entries": self.targets}, list(self.catalog.values()))
                if e["t"] >= since and (not plats or e["p"] in plats) and (not cats or e["c"] in cats)
                and (not bounty_only or e["$"]) and (not whys or e["w"] in whys)]
        page = _page(rows, offset, limit)
        page["results"] = [{"at": iso(e["t"]), "event": e["w"], "target": e["a"], "category": e["c"], "type": e["y"],
                            "bounty_eligible": bool(e["$"]), "program_id": e["id"], "program": e["n"],
                            "platform": e["p"], "program_reward_max": e["mx"] or None, "url": e["u"],
                            "date_source": "platform" if e.get("pd") else "bbwatch"} for e in page["results"]]
        return page

    # ---------- changes and health ----------

    def recent_changes(self, within_hours=24, kind=None, platform=None, q=None, offset=0, limit=100):
        kinds, plats, ws = {k.upper() for k in _list(kind)}, _list(platform), _words(q)
        since = self.now - float(within_hours or 24) * 3600
        rows = []
        for e in self.changes:
            t = db.ts(e.get("ts"))
            if t < since or (kinds and str(e.get("kind")).upper() not in kinds) or (plats and e.get("tag") not in plats):
                continue
            if ws and not all(w in f"{e.get('title')} {e.get('body')}".lower() for w in ws):
                continue
            rows.append({"at": e.get("ts"), "kind": e.get("kind"), "platform": e.get("tag"), "program": e.get("name"),
                         "what": e.get("what"), "title": e.get("title"), "detail": e.get("body"), "url": e.get("url"),
                         "priority": e.get("prio")})
        return _page(rows, offset, limit)

    def stats(self):
        per = {}
        for c in self.catalog.values():
            p = per.setdefault(c["p"], {"platform_name": db.LABELS.get(c["p"], c["p"]), "programs": 0,
                                        "accepting_reports": 0, "bounty_programs_live": 0, "targets_in_scope": 0})
            p["programs"] += 1
            p["accepting_reports"] += c["s"] in LIVE
            p["bounty_programs_live"] += bool(c["b"] and c["s"] in LIVE and not c["c"])
            p["targets_in_scope"] += c["na"]
        for name, s in (self.status.get("sources") or {}).items():
            if name in per:
                per[name]["healthy"] = not s.get("error")
                per[name]["last_good_poll"] = iso(s.get("ok_at"))
                if s.get("error"):
                    per[name]["error"] = s["error"]
        return {"data_updated_at": self.status.get("updated"), "programs": len(self.catalog),
                "targets_in_scope": sum(p["targets_in_scope"] for p in per.values()),
                "categories": CATEGORIES, "sorts": list(SORTS), "platforms": per}
