#!/usr/bin/env python3
"""bbwatch — near-real-time bug-bounty program & scope watcher -> ntfy.

Polls every platform's own public endpoints directly, each on its own thread
and cadence, diffs each program and its scope against the last snapshot, and
pushes one ntfy notification per changed program — naming the exact assets.

  H1         45s   hackerone.com/graphql       all teams + structured scopes
  Bugcrowd   60s   bugcrowd.com/engagements    listing; brief changelog on rotation (~12 min sweep)
  Intigriti  30s   Algolia programs index      lastUpdatedAt -> detail refetch (+ rotation)
  YesWeHack  45s   api.yeswehack.com/programs  last_update_at -> detail refetch (+ rotation)
  Immunefi   30s   immunefi.com/public-api     full feed, ETag-conditional (304s)
  Mirror     5m    arkadiyt/bounty-targets-data — independent scrape used as a
                   cross-check: assets it has that we don't force a direct refetch

Modes:
  python3 watch.py                   one poll of each source, then exit
  python3 watch.py --loop            poll forever
  python3 watch.py --loop --for 5h40m
  python3 watch.py --test            send one test push and exit

Env:
  NTFY_TOPIC        required — ntfy topic(s) to publish to, comma-separated
  NTFY_SERVER       optional — default https://ntfy.sh
  NTFY_TOKEN        optional — bearer token for a protected topic
  STATE_DIR         optional — snapshot directory (default .state)
  STATE_SYNC_CMD    optional — shell command run after state is saved
  HEALTHCHECK_URL   optional — pinged at most every 5 min while polls succeed
  GITHUB_TOKEN      optional — raises the mirror's GitHub API rate limit
  INTERVAL          optional — override every direct source's interval (seconds)
  SOURCES           optional — comma list of h1,bugcrowd,intigriti,ywh,immunefi,mirror
  MIN_PRIORITY      optional — drop pushes below this priority (default 2)
  DRY_RUN=1         optional — print pushes instead of sending them
  BBWATCH_TEST=1    optional — same as --test
"""

import argparse
import base64
import gzip
import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
STATE_DIR = os.environ.get("STATE_DIR", ".state")
STATE_FILE = os.path.join(STATE_DIR, "state.json.gz")
STATUS_FILE = os.path.join(STATE_DIR, "status.json")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
DRY_RUN = bool(os.environ.get("DRY_RUN"))
MIN_PRIORITY = int(os.environ.get("MIN_PRIORITY", "2"))

GONE_AFTER = 3        # consecutive polls a program must be missing before it counts as removed
FAIL_ALERT = 5        # consecutive failed polls before a source-health push
MAX_PUSHES = 8        # individual pushes per poll; the rest collapse into one digest
MAX_LINES = 12        # asset/detail lines per push
SYNC_EVERY = 600      # seconds between state syncs when nothing notifiable happened
DEDUP_FOR = 12 * 3600

STOP = threading.Event()


def log(msg):
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


# ---------- http ----------

def fetch(url, data=None, headers=None, timeout=45, tries=3):
    """GET (or POST when data is given) -> (status, text, headers).

    304 comes back as a status; 5xx/429/network errors are retried; any other
    HTTP error raises immediately.
    """
    hdrs = {"User-Agent": UA, "Accept": "application/json", "Accept-Encoding": "gzip"}
    hdrs.update(headers or {})
    if data is not None and not isinstance(data, bytes):
        data = json.dumps(data).encode()
        hdrs.setdefault("Content-Type", "application/json")
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return r.status, raw.decode("utf-8", "replace"), r.headers
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, "", e.headers
            if (e.code < 500 and e.code != 429) or attempt == tries - 1:
                raise
        except OSError:  # URLError, timeouts, resets
            if attempt == tries - 1:
                raise
        time.sleep(3 * (attempt + 1))


def get_json(url, **kw):
    return json.loads(fetch(url, **kw)[1])


def q(s):
    return urllib.parse.quote(str(s), safe="")


def _at(lst, i):
    return lst[i] if isinstance(i, int) and 0 <= i < len(lst) else str(i)


def money(v, cur="USD"):
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(str(cur or "").upper())
    v = int(v) if float(v).is_integer() else v
    return f"{sym}{v:,}" if sym else f"{v:,} {cur}".strip()


def _num(v):
    try:
        return float(re.sub(r"[^\d.]", "", str(v)) or 0)
    except ValueError:
        return 0.0


# ---------- normalized records ----------
#
# program: {name, url, state, bounty, max, sig, assets, [tags], [ref], [ver]}
#   state   "open" | "paused" | "closed" | platform-specific lowercase
#   sig     listing-level change signal; a change triggers a scope refetch
#   assets  {"<type>|<identifier>": [in_scope, bounty_eligible|None, severity|None]}
#           or None when the scope is not known (never diffed, never erases)

def program(name, url, state, bounty, max_=None, sig=None, assets=None, tags=None, ref=None):
    p = {"name": str(name or "?").strip(), "url": url, "state": state, "bounty": bool(bounty),
         "max": max_, "sig": sig, "assets": assets}
    if tags:
        p["tags"] = tags
    if ref:
        p["ref"] = ref
    return p


def add_asset(assets, ident, typ, in_scope=True, bounty=None, sev=None):
    ident = str(ident or "").strip()
    if not ident:
        return
    k = f"{str(typ or 'other').strip().lower()}|{ident}"
    old = assets.get(k)
    if old:  # the same asset listed twice: the widest entry wins
        in_scope, bounty, sev = old[0] or in_scope, old[1] or bounty, old[2] or sev
    assets[k] = [bool(in_scope), bounty, sev]


# ---------- sources ----------

class Source:
    name = label = ""
    every = 60    # seconds between polls
    rotate = 0    # programs re-checked per poll even without a change signal
    workers = 4
    detail = None  # detail(key, program): fill program["assets"] (and "ver")

    def __init__(self):
        self.lock = threading.Lock()
        self.force = set()  # keys to refetch next poll (set by the mirror cross-check)

    def take_force(self):
        with self.lock:
            keys, self.force = self.force, set()
        return keys

    def listing(self, prev, book):
        raise NotImplementedError

    def poll(self, prev, book):
        progs = self.listing(prev, book)
        if len(prev) > 10 and len(progs) < 0.8 * len(prev):
            raise RuntimeError(f"partial listing: {len(progs)} programs vs {len(prev)} last time")
        if self.detail is None:
            return progs
        want = {k for k in self.take_force() if k in progs}
        for k, p in progs.items():
            o = prev.get(k)
            if o is not None and p["assets"] is None:
                p["assets"] = o.get("assets")
                if "ver" in o:
                    p["ver"] = o["ver"]
            if o is None or o.get("assets") is None or o.get("sig") != p.get("sig"):
                want.add(k)
        keys = sorted(progs)
        if self.rotate and keys:
            i = book.get("rot", 0) % len(keys)
            want.update((keys + keys)[i:i + min(self.rotate, len(keys))])
            book["rot"] = i + self.rotate
        failed = []

        def one(k):
            try:
                self.detail(k, progs[k])
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 404):  # scope not public (e.g. application-only): don't hammer it
                    return
                failed.append(f"{k}: {e}")
                progs[k]["sig"] = (prev.get(k) or {}).get("sig")
            except Exception as e:  # keep the old scope; retry next poll
                failed.append(f"{k}: {e}")
                progs[k]["sig"] = (prev.get(k) or {}).get("sig")

        with ThreadPoolExecutor(self.workers) as ex:
            list(ex.map(one, sorted(want)))
        if failed:
            log(f"{self.label}: {len(failed)}/{len(want)} scope fetches failed (e.g. {failed[0][:160]})")
        return progs


def _b64(n):
    return base64.b64encode(str(n).encode()).decode().rstrip("=")


class HackerOne(Source):
    name, label = "h1", "H1"
    FIELDS = "asset_identifier asset_type eligible_for_submission eligible_for_bounty max_severity"
    # All public teams, open or not, so pauses and re-opens show up as status
    # changes instead of fake removals/new programs. ASC keeps page offsets
    # stable while new programs are appended at the end.
    TEAMS = """query($after: String) {
  teams(first: 100, after: $after, secure_order_by: {started_accepting_at: {_direction: ASC}},
        where: {_and: [{_not: {external_program: {}}}, {state: {_neq: sandboxed}}, {state: {_neq: soft_launched}}]}) {
    total_count
    pageInfo { endCursor hasNextPage }
    nodes {
      handle name offers_bounties submission_state
      structured_scopes(first: 100, archived: false) { total_count pageInfo { hasNextPage } nodes { %s } }
    }
  }
}""" % FIELDS
    SCOPES = """query($handle: String!, $after: String) {
  team(handle: $handle) {
    structured_scopes(first: 1000, after: $after, archived: false) { pageInfo { endCursor hasNextPage } nodes { %s } }
  }
}""" % FIELDS

    every = 45
    BIG_ROTATE = 8  # >100-scope teams fully refetched per poll even when their scope count is unchanged

    def __init__(self):
        super().__init__()
        self.csrf = self.cookie = None
        self.csrf_at = 0

    def session(self):
        if self.csrf and time.time() - self.csrf_at < 1800:
            return
        _, body, h = fetch("https://hackerone.com/directory/programs", headers={"Accept": "text/html"})
        m = re.search(r'name="csrf-token"\s+content="([^"]+)"', body)
        if not m:
            raise RuntimeError("no csrf token on the directory page")
        self.csrf, self.csrf_at = m.group(1), time.time()
        self.cookie = "; ".join(c.split(";", 1)[0] for c in (h.get_all("Set-Cookie") or []))

    def gql(self, query, variables):
        _, body, _ = fetch("https://hackerone.com/graphql", data={"query": query, "variables": variables},
                           headers={"X-Csrf-Token": self.csrf or "", "Cookie": self.cookie or ""}, timeout=60)
        d = json.loads(body)
        if not d.get("data"):
            self.csrf = None  # a stale session is the usual cause: re-handshake next poll
            raise RuntimeError(f"graphql: {str(d.get('errors'))[:200]}")
        return d["data"]

    def pages(self, offsets):
        def one(off):
            return off, self.gql(self.TEAMS, {"after": _b64(off) if off else None})["teams"]
        with ThreadPoolExecutor(8) as ex:
            return dict(ex.map(one, offsets))

    def listing(self, prev, book):
        self.session()
        # Offset cursors (base64 of the offset): fire every page at once, sized by last poll's total.
        pages = self.pages(range(0, (book.get("total") or 0) + 100, 100))
        first = pages[0]
        total = first["total_count"]
        if first["pageInfo"]["hasNextPage"] and first["pageInfo"]["endCursor"] != _b64(len(first["nodes"])):
            raise RuntimeError(f"H1 cursor format changed ({first['pageInfo']['endCursor']!r})")
        pages.update(self.pages([o for o in range(0, total, 100) if o not in pages]))
        nodes = [n for off in sorted(pages) for n in pages[off]["nodes"]]
        book["total"] = total

        # Teams with >100 scopes only carry their first 100 inline. Refetch them in full when the
        # scope count moved, when the mirror says we're behind, or on rotation; else reuse last poll's.
        big = sorted((n for n in nodes if n["structured_scopes"]["pageInfo"]["hasNextPage"]),
                     key=lambda n: n["handle"])
        forced = self.take_force()
        rot = book.get("big_rot", 0) % max(1, len(big))
        pick = {n["handle"] for n in (big + big)[rot:rot + min(self.BIG_ROTATE, len(big))]} | forced
        book["big_rot"] = rot + self.BIG_ROTATE
        refetch = []
        for n in big:
            o = prev.get(n["handle"])
            if (o and o.get("assets") is not None and o.get("sig") == n["structured_scopes"]["total_count"]
                    and n["handle"] not in pick):
                n["reuse"] = o["assets"]
            else:
                refetch.append(n)
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(self.all_scopes, refetch))

        out = {}
        for n in nodes:
            ss = n["structured_scopes"]
            assets = n.get("reuse")
            # H1 sometimes returns timed-out blank scopes: treat the team's scope as unknown
            if assets is None and all(s and s.get("asset_identifier") for s in ss["nodes"]) \
                    and not ss["pageInfo"]["hasNextPage"]:
                assets = {}
                for s in ss["nodes"]:
                    add_asset(assets, s["asset_identifier"], s["asset_type"], s["eligible_for_submission"],
                              s["eligible_for_bounty"], s["max_severity"])
            state = {"disabled": "closed"}.get(n["submission_state"], n["submission_state"])
            out[n["handle"]] = program(n["name"], f"https://hackerone.com/{n['handle']}", state,
                                       n["offers_bounties"], sig=ss["total_count"], assets=assets)
        if len(out) < 0.9 * total:
            raise RuntimeError(f"partial listing: {len(out)}/{total}")
        return out

    def all_scopes(self, n):
        """Refetch a big team's whole scope in pages of 1000 (usually one call); failure = unknown scope."""
        nodes, after = [], None
        try:
            while True:
                ss = self.gql(self.SCOPES, {"handle": n["handle"], "after": after})["team"]["structured_scopes"]
                nodes += ss["nodes"]
                if not ss["pageInfo"]["hasNextPage"]:
                    break
                after = ss["pageInfo"]["endCursor"]
        except Exception as e:
            log(f"H1: full scope fetch failed for {n['handle']}: {e}")
            return
        n["structured_scopes"]["nodes"] = nodes
        n["structured_scopes"]["pageInfo"]["hasNextPage"] = False


class Bugcrowd(Source):
    name, label = "bugcrowd", "Bugcrowd"
    rotate = 25
    LIST = "https://bugcrowd.com/engagements.json?category=bug_bounty&sort_by=starts&sort_direction=desc&page={}"

    def listing(self, prev, book):
        first = get_json(self.LIST.format(1))
        meta = first.get("paginationMeta") or {}
        pages = -(-int(meta.get("totalCount") or 0) // max(1, int(meta.get("limit") or 24)))
        rows = list(first.get("engagements") or [])
        with ThreadPoolExecutor(4) as ex:
            for d in ex.map(lambda n: get_json(self.LIST.format(n)), range(2, pages + 1)):
                rows.extend(d.get("engagements") or [])
        out = {}
        for e in rows:
            if e.get("isDemo") or not e.get("briefUrl"):
                continue
            path = e["briefUrl"].rstrip("/")
            mx = (e.get("rewardSummary") or {}).get("maxReward")
            out[path.rsplit("/", 1)[-1]] = program(
                e.get("name"), "https://bugcrowd.com" + path, str(e.get("accessStatus") or "open").lower(),
                "$" in str(mx or ""), mx, sig=f"{mx}|{e.get('accessStatus')}", ref=path)
        return out

    def detail(self, key, p):
        base = "https://bugcrowd.com" + p["ref"]
        logs = get_json(base + "/changelog.json").get("changelogs") or []
        latest = next((c for c in logs if c.get("changelogState") == "Latest"), logs[0] if logs else None)
        if latest is None or (latest["id"] == p.get("ver") and p.get("assets") is not None):
            return
        brief = get_json(f"{base}/changelog/{latest['id']}.json")["data"]
        assets = {}
        for g in brief.get("scope") or []:
            in_scope = bool(g.get("inScope"))
            paid = in_scope and any((v or {}).get("max") for v in (g.get("rewardRangeData") or {}).values())
            for t in g.get("targets") or []:
                add_asset(assets, t.get("uri") or t.get("name") or t.get("ipAddress"), t.get("category"),
                          in_scope, paid)
        p["assets"], p["ver"] = assets, latest["id"]


class Intigriti(Source):
    name, label = "intigriti", "Intigriti"
    every = 30
    rotate = 8
    STATUS = ["_", "wizard", "draft", "open", "suspended", "closing", "closed", "archived"]
    CONF = ["_", "invite-only", "application", "registered", "public"]
    TYPES = ["_", "url", "android", "ios", "iprange", "device", "other", "wildcard"]
    ALGOLIA = ("https://aazuksyar4-dsn.algolia.net/1/indexes/*/queries"
               "?x-algolia-api-key=70d8a3400477311f27ce002ec953aeb0&x-algolia-application-id=AAZUKSYAR4")

    def listing(self, prev, book):
        d = get_json(self.ALGOLIA, headers={"Referer": "https://www.intigriti.com/"},
                     data={"requests": [{"indexName": "programs_prod", "hitsPerPage": 1000, "page": 0, "query": ""}]})
        out = {}
        for h in d["results"][0]["hits"]:
            mx = h.get("maxBounty") or {}
            val = mx.get("value") or 0
            conf = _at(self.CONF, h.get("confidentialityLevel"))
            tags = [t for t, on in ((conf, conf != "public"), ("T&C", h.get("tacRequired")),
                                    ("2FA", h.get("twoFactorRequired"))) if on]
            ch, hd = h["companyHandle"], h["handle"]
            out[h["programId"]] = program(
                h.get("name"), f"https://www.intigriti.com/programs/{q(ch)}/{q(hd)}/detail",
                _at(self.STATUS, h.get("status")), val > 0,
                money(val, mx.get("currency")) if val else None,
                sig=h.get("lastUpdatedAt"), tags=tags, ref=[ch, hd])
        return out

    def detail(self, key, p):
        ch, hd = p["ref"]
        d = get_json(f"https://app.intigriti.com/api/core/public/programs/{q(ch)}/{q(hd)}")
        coll = max(d.get("assetsCollection") or [], key=lambda c: c.get("createdAt") or 0, default=None)
        assets = {}
        for t in ((coll or {}).get("content") or {}).get("assetsAndGroups") or []:
            for a in (t["assets"] if "assets" in t else [t]):
                tier = a.get("bountyTierId")  # 1 no bounty, 2-4 tier 3..1, 5 out of scope
                add_asset(assets, a.get("name"), _at(self.TYPES, a.get("typeId")), tier != 5, tier in (2, 3, 4))
        p["assets"] = assets


class YesWeHack(Source):
    name, label = "ywh", "YesWeHack"
    every = 45
    rotate = 5

    def listing(self, prev, book):
        rows, page, pages = [], 1, 1
        while page <= pages:
            d = get_json(f"https://api.yeswehack.com/programs?page={page}&resultsPerPage=100")
            rows += d.get("items") or []
            pages = int((d.get("pagination") or {}).get("nb_pages") or 1)
            page += 1
        out = {}
        for r in rows:
            if r.get("demo"):
                continue
            if r.get("disabled") or r.get("archived"):
                state = "closed"
            else:
                state = "open" if r.get("status") == "V" else str(r.get("status")).lower()
            mx = r.get("bounty_reward_max") or 0
            out[r["slug"]] = program(
                r.get("title"), f"https://yeswehack.com/programs/{r['slug']}", state, r.get("bounty"),
                money(mx, r.get("currency") or "EUR") if mx else None,
                sig=f"{r.get('last_update_at')}|{r.get('scopes_count')}",
                tags=["VDP"] if r.get("vdp") else None)
        return out

    def detail(self, key, p):
        d = get_json(f"https://api.yeswehack.com/programs/{q(key)}")
        assets = {}
        for s in d.get("scopes") or []:
            add_asset(assets, s.get("scope"), s.get("scope_type"), True, p["bounty"], s.get("asset_value"))
        oos = d.get("out_of_scope")
        for s in oos if isinstance(oos, list) else []:
            if isinstance(s, dict):
                add_asset(assets, s.get("scope"), s.get("scope_type"), False)
            else:
                add_asset(assets, s, "other", False)
        p["assets"] = assets


class Immunefi(Source):
    name, label = "immunefi", "Immunefi"
    every = 30
    URL = "https://immunefi.com/public-api/bounties.json"

    def listing(self, prev, book):
        hdrs = {"If-None-Match": book["etag"]} if prev and book.get("etag") else {}
        status, body, h = fetch(self.URL, headers=hdrs, timeout=90)
        if status == 304:
            return {k: dict(v) for k, v in prev.items()}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        out = {}
        for b in json.loads(body):
            slug = b.get("slug") or str(b.get("id"))
            state = "paused" if b.get("isPaused") else "open"
            if b.get("endDate") and b["endDate"] < now:
                state = "ended"
            mx = b.get("maxBounty") or 0
            assets = {}
            for a in b.get("assets") or []:
                add_asset(assets, a.get("url"), a.get("type"), True, True)
            tags = [t for t, on in (("invite-only", b.get("inviteOnly")), ("KYC", b.get("kyc")),
                                    ("boost", b.get("endDate"))) if on]
            out[slug] = program(b.get("project") or slug, f"https://immunefi.com/bug-bounty/{slug}/", state,
                                mx > 0, money(mx) if mx else None, sig=b.get("updatedDate"), assets=assets,
                                tags=tags)
        book["etag"] = h.get("ETag")
        return out


class Mirror(Source):
    """arkadiyt/bounty-targets-data: an independent scrape of the same platforms (every ~30 min).

    Never notifies by itself. It is a cross-check: any in-scope asset the mirror has that our
    snapshot lacks forces a direct refetch of that program, so a scope change our change
    signals missed still surfaces (from the direct source, with full detail).
    """
    name, label = "mirror", "Mirror"
    every = 300
    REPO = "arkadiyt/bounty-targets-data"
    FILES = {  # our source -> (mirror file, key field, in-scope identifier field)
        "h1": ("hackerone_data.json", "handle", "asset_identifier"),
        "bugcrowd": ("bugcrowd_data.json", "url", "target"),
        "intigriti": ("intigriti_data.json", "id", "endpoint"),
        "ywh": ("yeswehack_data.json", "id", "target"),
    }

    def __init__(self, names):
        super().__init__()
        self.names = [n for n in self.FILES if n in names]

    def poll(self, prev, book):
        hdrs = {"Accept": "application/vnd.github+json"}
        if os.environ.get("GITHUB_TOKEN"):
            hdrs["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
        sha = get_json(f"https://api.github.com/repos/{self.REPO}/commits?path=data&per_page=1", headers=hdrs)[0]["sha"]
        if sha == book.get("sha"):
            return None
        out = {}
        for name in self.names:
            fname, keyf, identf = self.FILES[name]
            rows = json.loads(fetch(f"https://raw.githubusercontent.com/{self.REPO}/{sha}/data/{fname}", timeout=180)[1])
            progs = {}
            for r in rows:
                k = str(r.get(keyf) or "")
                if name == "bugcrowd":
                    k = k.rstrip("/").rsplit("/", 1)[-1]
                progs[k] = {str(t.get(identf) or "").strip().lower()
                            for t in (r.get("targets") or {}).get("in_scope") or [] if isinstance(t, dict)} - {""}
            out[name] = progs
        book["sha"] = sha
        return out


def reconcile(state, mirror, by_name):
    """Mirror result -> force refetches of programs where the mirror knows assets we don't."""
    for name, theirs in mirror.items():
        src = by_name.get(name)
        ours = ((state.get("sources") or {}).get(name) or {}).get("programs") or {}
        if src is None or not ours:
            continue
        behind = set()
        for k, idents in theirs.items():
            p = ours.get(k)
            if p is None or p.get("assets") is None:
                continue
            if idents - {a.split("|", 1)[1].strip().lower() for a in p["assets"]}:
                behind.add(k)
        with src.lock:
            src.force |= behind
        log(f"Mirror: {src.label}: {len(theirs)} programs cross-checked, {len(behind)} behind the mirror"
            + (f" -> refetching {', '.join(sorted(behind)[:5])}{' …' if len(behind) > 5 else ''}" if behind else ""))


SOURCES = {s.name: s for s in (HackerOne, Bugcrowd, Intigriti, YesWeHack, Immunefi, Mirror)}


# ---------- messages ----------
#
# Phone-first: the title says who and what; the first body lines are the change itself
# (what shows in a collapsed notification); the program summary comes last.
#
#   🔭 1win · H1 — 2 new targets
#   ➕ 1w.cash — Web · bounty · critical
#   ➕ Deposit/Withdraw — Other · bounty · critical
#   ➖ old.1win.com — removed from scope
#
#   Open · pays bounties

LIVE = {"open", "active", "live", "upcoming"}  # states where a hunter can (soon) submit

TYPE_NAMES = {
    "Web": ("url", "website", "web-application", "websites_and_applications", "web", "application"),
    "Wildcard": ("wildcard",),
    "API": ("api",),
    "Android app": ("android", "google_play_app_id", "mobile-application-android", "other_apk"),
    "iOS app": ("ios", "apple_store_app_id", "mobile-application-ios", "testflight", "other_ipa"),
    "Mobile app": ("mobile-application", "mobile"),
    "Desktop app": ("downloadable_executables", "windows_app_store_app_id", "executable", "desktop"),
    "Source code": ("source_code", "github", "repository", "repo"),
    "Smart contract": ("smart_contract", "smart-contract", "contract"),
    "Blockchain": ("blockchain_dlt", "blockchain", "protocol"),
    "IP": ("ip_address", "ip"),
    "IP range": ("cidr", "iprange", "ip_range"),
    "Network": ("network",),
    "Hardware/IoT": ("hardware", "iot", "device"),
    "AI model": ("ai_model", "ai-model"),
}
TYPE_NAME = {raw: nice for nice, raws in TYPE_NAMES.items() for raw in raws}
STATE_NAMES = {"open": "Open", "paused": "Paused", "closed": "Closed", "ended": "Ended", "suspended": "Suspended",
               "closing": "Closing", "archived": "Archived", "upcoming": "Upcoming", "active": "Live",
               "live": "Live", "judging": "Judging"}
STATE_MEANING = {"open": "accepting reports again", "active": "live now", "live": "live now",
                 "paused": "not accepting reports", "suspended": "not accepting reports",
                 "closed": "closed", "ended": "ended", "judging": "submissions closed, judging",
                 "upcoming": "starting soon"}


def paid(a, p):
    return a[1] is True or (a[1] is None and p["bounty"])


def nice_state(s):
    return STATE_NAMES.get(s, str(s).replace("_", " ").capitalize())


def asset_line(mark, k, a, p, note=""):
    typ, ident = k.split("|", 1)
    ident = " ".join(ident.split())
    if len(ident) > 64:
        ident = ident[:63] + "…"
    bits = [TYPE_NAME.get(typ, "Other" if typ.isdigit() else typ.replace("_", " ").capitalize())]
    if p["bounty"]:
        bits.append("bounty" if paid(a, p) else "no bounty")
    if a[2] and str(a[2]).lower() not in ("none", "null"):
        bits.append(str(a[2]).lower().replace("_", " "))
    return f"{mark} {ident} — {note or ' · '.join(bits)}"


def summary(p):
    bits = [nice_state(p["state"])]
    if p["bounty"]:
        bits.append(f"pays up to {p['max']}" if p.get("max") else "pays bounties")
    else:
        bits.append("no bounty (VDP)")
    bits += p.get("tags") or []
    return " · ".join(bits)


def event(src, p, kind, prio, what, lines, emoji):
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES - 1] + [f"… and {len(lines) - MAX_LINES + 1} more"]
    return {"kind": kind, "prio": prio, "url": p.get("url"), "tag": src.name,
            "title": f"{emoji} {p['name']} · {src.label} — {what}",
            "body": "\n".join(lines + ["", summary(p)])}


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def new_event(src, key, p):
    live = p["state"] in LIVE
    prio = (5 if p["bounty"] else 3) if live else 2
    a = p.get("assets")
    if a is None:
        lines = ["Scope not published yet — open the program page."]
    else:
        ins = sorted((k for k in a if a[k][0]), key=lambda k: (not paid(a[k], p), k))
        lines = [f"{plural(len(ins), 'target')} in scope:"] + [asset_line("•", k, a[k], p) for k in ins]
    return event(src, p, "NEW", prio, f"new {'contest' if p.get('contest') else 'program'}", lines, "🆕")


def diff_program(src, key, o, p):
    kinds, lines = [], []
    live = p["state"] in LIVE
    oa, pa = o.get("assets"), p.get("assets")
    fresh = []
    if oa is not None and pa is not None and oa != pa:
        fresh = sorted((k for k in pa if pa[k][0] and not (k in oa and oa[k][0])),
                       key=lambda k: (not paid(pa[k], p), k))
        now_paid = sorted(k for k in pa if k in oa and pa[k][0] and oa[k][0] and paid(pa[k], p) and not paid(oa[k], o))
        sev = sorted(k for k in pa if k in oa and pa[k][0] and oa[k][0] and pa[k][2] != oa[k][2])
        gone = sorted(k for k in oa if oa[k][0] and not (k in pa and pa[k][0]))
        if fresh or now_paid:
            hot = any(paid(pa[k], p) for k in fresh + now_paid)
            what = plural(len(fresh), "new target") if fresh else plural(len(now_paid), "target") + " now paid"
            kinds.append(((4 if hot else 3) if live else 2, "SCOPE", what, "🔭"))
        elif gone or sev:
            kinds.append((2, "SCOPE-", "scope changed" if sev else plural(len(gone), "target") + " removed", "✂️"))
        lines += [asset_line("➕", k, pa[k], p) for k in fresh]
        lines += [asset_line("💵", k, pa[k], p, "now bounty-eligible") for k in now_paid]
        lines += [asset_line("⚖️", k, pa[k], p, f"severity {oa[k][2] or '?'} → {pa[k][2] or '?'}".lower()) for k in sev]
        lines += [asset_line("➖", k, oa[k], o, "removed from scope") for k in gone]
    if o["state"] != p["state"]:
        meaning = STATE_MEANING.get(p["state"], "")
        lines.append(f"🚦 {nice_state(o['state'])} → {nice_state(p['state'])}" + (f" ({meaning})" if meaning else ""))
        if live:
            kinds.append((4 if p["bounty"] else 3, "OPEN", "reopened" if p["state"] == "open" else nice_state(p["state"]).lower(), "🟢"))
        else:
            kinds.append((2, "STATE", nice_state(p["state"]).lower(), "🚦"))
    if o["bounty"] != p["bounty"]:
        lines.append("💰 Now pays bounties" if p["bounty"] else "💰 Stopped paying bounties (now VDP)")
        kinds.append((4, "BOUNTY", "now pays bounties", "💰") if p["bounty"] else (2, "BOUNTY", "bounties dropped", "💰"))
    elif o.get("max") and p.get("max") and _num(o["max"]) != _num(p["max"]):
        up = _num(p["max"]) > _num(o["max"])
        lines.append(f"💰 Max bounty {'raised' if up else 'lowered'}: {o['max']} → {p['max']}")
        kinds.append((4 if up else 2, "BOUNTY", f"max bounty {'up' if up else 'down'}", "💰"))
    if not kinds:
        return None
    prio, kind, what, emoji = max(kinds, key=lambda k: k[0])
    return event(src, p, kind, prio, what, lines, emoji)


def gone_event(src, p):
    return event(src, p, "GONE", 2, "no longer listed",
                 ["⚠️ Gone from the public directory (closed, made private, or deleted)."], "⚠️")


# ---------- diffing ----------

def diff_source(src, prev, new, book):
    """-> (state to keep, events). Absent programs are carried for GONE_AFTER polls."""
    events, merged = [], dict(new)
    missing = book.setdefault("missing", {})
    for k in prev.keys() - new.keys():
        missing[k] = missing.get(k, 0) + 1
        if missing[k] >= GONE_AFTER:
            del missing[k]
            events.append(gone_event(src, prev[k]))
        else:
            merged[k] = prev[k]
    for k, p in new.items():
        missing.pop(k, None)
        o = prev.get(k)
        if o is None:
            events.append(new_event(src, k, p))
            continue
        if p.get("assets") is None and o.get("assets") is not None:  # unknown scope never erases a known one
            p["assets"] = o["assets"]
        e = diff_program(src, k, o, p)
        if e:
            events.append(e)
    return merged, events


# ---------- merging one source's poll ----------

def health(title, body, prio, tag):
    return {"kind": "HEALTH", "prio": prio, "url": None, "tag": tag, "title": title, "body": body}


def process(src, progs, err, dt, state, by_name):
    """Merge one finished poll into state -> events. Runs on the main thread only."""
    slot = state["sources"][src.name]
    book = slot["book"]
    events = []
    if err is not None:
        book["fails"] = book.get("fails", 0) + 1
        book["err"] = f"{type(err).__name__}: {err}"[:300]
        log(f"{src.label}: FAILED ({dt:.1f}s, {book['fails']}x in a row): {book['err']}")
        if book["fails"] == FAIL_ALERT:
            events.append(health(f"⚠️ bbwatch: {src.label} source failing",
                                 f"{FAIL_ALERT} polls in a row failed.\n{book['err']}", 3, "warning"))
        return events
    if book.get("fails", 0) >= FAIL_ALERT:
        events.append(health(f"✅ bbwatch: {src.label} source recovered",
                             f"back after {book['fails']} failed polls", 2, "white_check_mark"))
    book["fails"], book["ok_at"] = 0, int(time.time())
    book.pop("err", None)
    if isinstance(src, Mirror):
        if progs is None:
            log(f"Mirror: no new mirror commit ({dt:.1f}s)")
        else:
            reconcile(state, progs, by_name)
        return events
    if not book.get("baselined"):
        slot["programs"], book["baselined"] = progs, True
        log(f"{src.label}: baseline {len(progs)} programs ({dt:.1f}s)")
        return events
    slot["programs"], evs = diff_source(src, slot["programs"], progs, book)
    log(f"{src.label}: {len(progs)} programs, {len(evs)} events ({dt:.1f}s)")
    return events + evs


def worker(src, slot, results, loop, every):
    """One thread per source, each on its own cadence: a slow platform never delays the others.

    The thread hands each result to the main thread and waits for it to be merged before
    polling again, so it never reads its slot while the main thread is writing it.
    """
    while not STOP.is_set():
        t = time.time()
        try:
            progs, err = src.poll(slot["programs"], slot["book"]), None
        except Exception as e:
            progs, err = None, e
        ack = threading.Event()
        results.put((src, progs, err, time.time() - t, ack))
        ack.wait()
        if not loop:
            return
        STOP.wait(max(1.0, every - (time.time() - t)))


# ---------- delivery ----------

def topics():
    return [t.strip() for t in os.environ.get("NTFY_TOPIC", "").split(",") if t.strip()]


def publish(msg):
    if DRY_RUN:
        print("DRY_RUN push:\n" + json.dumps(msg, indent=2, ensure_ascii=False), flush=True)
        return
    tok = os.environ.get("NTFY_TOKEN")
    fetch(NTFY_SERVER, data=msg, headers={"Authorization": f"Bearer {tok}"} if tok else None, timeout=20, tries=2)
    time.sleep(0.4)


def to_push(e):
    m = {"title": e["title"], "message": e["body"], "priority": e["prio"], "tags": [e["tag"]]}
    if e.get("url"):
        m["click"] = e["url"]
        m["actions"] = [{"action": "view", "label": "Open program", "url": e["url"]}]
    return m


def deliver(state, events):
    now = int(time.time())
    sent = {k: v for k, v in (state.get("sent") or {}).items() if now - v < DEDUP_FOR}
    fresh = []
    for e in sorted((e for e in events if e["prio"] >= MIN_PRIORITY), key=lambda e: -e["prio"]):
        eid = hashlib.sha1(f"{e['title']}\n{e['body']}".encode()).hexdigest()[:16]
        if eid not in sent:  # a replayed poll (restored older state) must not re-push
            sent[eid] = now
            fresh.append(e)
    state["sent"] = sent
    if len(fresh) > MAX_PUSHES:
        head, rest = fresh[:MAX_PUSHES - 1], fresh[MAX_PUSHES - 1:]
        fresh = head + [{"prio": min(3, rest[0]["prio"]), "url": None, "tag": "satellite",
                         "title": f"📡 bbwatch: +{len(rest)} more changes",
                         "body": "\n".join(e["title"] for e in rest[:25]) + ("\n…" if len(rest) > 25 else "")}]
    outbox = (state.get("outbox") or []) + [dict(to_push(e), topic=t) for e in fresh for t in topics() or [""]]
    keep = []
    for m in outbox:
        if not keep:
            try:
                publish(m)
                continue
            except Exception as e:  # ntfy down: queue this and everything after it, retry next merge
                log(f"ntfy publish failed: {e}")
        keep.append(m)
    state["outbox"] = keep[-100:]
    return len(fresh)


# ---------- state ----------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"v": 2}
    with gzip.open(STATE_FILE, "rt") as f:
        return json.load(f)


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    state["saved_at"] = int(time.time())
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True).encode()
    with open(STATE_FILE + ".tmp", "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as g:
        g.write(raw)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)
    status = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "sources": {
        name: {"programs": len(s["programs"]), "fails": s["book"].get("fails", 0), "error": s["book"].get("err"),
               "ok_at": s["book"].get("ok_at")}
        for name, s in (state.get("sources") or {}).items()}, "outbox": len(state.get("outbox") or [])}
    with open(STATUS_FILE + ".tmp", "w") as f:
        json.dump(status, f, indent=2)
        f.write("\n")
    os.replace(STATUS_FILE + ".tmp", STATUS_FILE)


def sync_state():
    cmd = os.environ.get("STATE_SYNC_CMD")
    if not cmd:
        return
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        if r.returncode:
            log(f"state sync failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
    except subprocess.TimeoutExpired:
        log("state sync timed out")


def ping(url):
    try:
        urllib.request.urlopen(url, timeout=15).read()
    except Exception as e:
        log(f"healthcheck ping failed: {e}")


def samples():
    """One of every notification kind, from fake programs, through the real builders."""
    h1, imm, bc, inti = (SOURCES[n] for n in ("h1", "immunefi", "bugcrowd", "intigriti"))
    url = "https://hackerone.com/directory/programs"
    p = program("Example Corp", url, "open", True, "$10,000", assets={
        "wildcard|*.example.com": [True, True, "critical"], "api|api.example.com": [True, True, "high"],
        "ios|com.example.app": [True, False, "medium"], "url|legacy.example.com": [True, True, "critical"]})
    new = dict(p, assets={k: v for k, v in p["assets"].items() if k != "url|legacy.example.com"})
    new["assets"].update({"url|pay.example.com": [True, True, "critical"], "ios|com.example.app": [True, True, "high"],
                          "api|graphql.example.com": [True, True, "critical"]})
    vdp = program("Example Health VDP", url, "open", False, assets={"url|portal.examplehealth.org": [True, None, None]})
    web3 = program("Example Protocol", "https://immunefi.com/bug-bounty/", "open", True, "$250,000",
                   tags=["KYC"], assets={"smart_contract|0x1f98…F984": [True, True, None]})
    ev = [
        new_event(h1, "x", p),
        diff_program(h1, "x", p, new),
        diff_program(imm, "y", dict(web3, state="paused"), web3),
        diff_program(bc, "z", dict(p, max="$5,000"), p),
        diff_program(inti, "w", vdp, dict(vdp, bounty=True, max="€2,500")),
        new_event(bc, "v", vdp),
        diff_program(h1, "x", p, dict(p, state="paused")),
        diff_program(h1, "x", p, dict(p, assets={k: v for k, v in p["assets"].items() if k != "url|legacy.example.com"})),
        gone_event(imm, web3),
        health("⚠️ bbwatch: Bugcrowd source failing", f"{FAIL_ALERT} polls in a row failed.\nHTTPError: HTTP Error 403: Forbidden",
               3, "warning"),
        health("✅ bbwatch: Bugcrowd source recovered", "back after 7 failed polls", 2, "white_check_mark"),
        health("🩺 bbwatch resumed after 1h12m offline", "The watcher was not running. Everything that changed "
               "meanwhile is diffed against the last snapshot on this first poll, so nothing is lost.", 2, "stethoscope"),
        {"prio": 3, "url": None, "tag": "eyes", "title": "👁️ bbwatch armed",
         "body": "Baseline recorded — changes from here on are pushed.\nH1: 751 programs\nBugcrowd: 287 programs"},
        {"prio": 3, "url": None, "tag": "satellite", "title": "📡 bbwatch: +9 more changes",
         "body": "🔭 Example Corp · H1 — 2 new targets\n🟢 Example Protocol · Immunefi — reopened\n…"},
    ]
    return [e for e in ev if e]


# ---------- main ----------

def parse_duration(s):
    total = sum(int(n) * {"h": 3600, "m": 60, "s": 1, "": 1}[u] for n, u in re.findall(r"(\d+)([hms]?)", s))
    return total or None


def fmt_gap(sec):
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m"


def main():
    ap = argparse.ArgumentParser(description="bug-bounty program & scope watcher -> ntfy")
    ap.add_argument("--loop", action="store_true", help="keep polling, each source on its own interval")
    ap.add_argument("--for", dest="duration", default="", help="with --loop: stop after e.g. 5h40m")
    ap.add_argument("--interval", type=int, default=int(os.environ["INTERVAL"]) if os.environ.get("INTERVAL") else None,
                    help="override every source's poll interval (seconds; the mirror keeps its own)")
    ap.add_argument("--sources", default=os.environ.get("SOURCES") or ",".join(SOURCES))
    ap.add_argument("--test", action="store_true", help="send one test push and exit")
    args = ap.parse_args()

    if not topics() and not DRY_RUN:
        sys.exit("NTFY_TOPIC is required")
    if args.test or os.environ.get("BBWATCH_TEST", "").lower() in ("1", "true", "yes"):
        evs = samples()
        intro = health(f"🧪 bbwatch test — {len(evs)} sample notifications follow",
                       "One of every kind bbwatch sends, built by the real message code.\n"
                       "\"Example …\" programs are fake. Priorities are real: 5 buzzes, 4 high, 3 normal, 2 quiet.",
                       3, "test_tube")
        for e in [intro] + evs:
            for t in topics() or [""]:
                publish(dict(to_push(e), topic=t))
            time.sleep(1.5)  # keep them in order on the phone
        return
    names = [s for s in args.sources.split(",") if s]
    unknown = [s for s in names if s not in SOURCES]
    if unknown:
        sys.exit(f"unknown sources: {', '.join(unknown)} (known: {', '.join(SOURCES)})")
    sources = [SOURCES[s](names) if s == "mirror" else SOURCES[s]() for s in names]
    by_name = {s.name: s for s in sources}
    direct = [s for s in sources if not isinstance(s, Mirror)]

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    hc = os.environ.get("HEALTHCHECK_URL")
    duration = parse_duration(args.duration) if args.duration else None
    deadline = time.time() + duration if duration else float("inf")
    state = load_state()
    slots = state.setdefault("sources", {})
    for s in sources:
        slots.setdefault(s.name, {"programs": {}, "book": {}})
    log("bbwatch: " + ", ".join(
        f"{s.label}/{s.every if isinstance(s, Mirror) or not args.interval else args.interval}s" for s in sources)
        + ((f" for {args.duration}" if duration else " forever") if args.loop else " (one poll each)"))

    events = []
    gap = int(time.time()) - state["saved_at"] if state.get("saved_at") else 0
    if gap > 20 * 60:
        events.append(health(f"🩺 bbwatch resumed after {fmt_gap(gap)} offline",
                             "The watcher was not running. Everything that changed meanwhile is diffed "
                             "against the last snapshot on this first poll, so nothing is lost.", 2, "stethoscope"))
    armed_pending = not all(slots[s.name]["book"].get("baselined") for s in direct)

    results = queue.Queue()
    threads = []
    for s in sources:
        every = s.every if isinstance(s, Mirror) or not args.interval else args.interval
        t = threading.Thread(target=worker, args=(s, slots[s.name], results, args.loop, every),
                             name=s.name, daemon=True)
        t.start()
        threads.append(t)

    last_sync = last_ping = last_ok = 0.0

    def handle(item):
        nonlocal last_sync, last_ok, armed_pending
        src, progs, err, dt, ack = item
        try:
            evs = process(src, progs, err, dt, state, by_name)
            if err is None:
                last_ok = time.time()
            if armed_pending and all(slots[s.name]["book"].get("baselined") for s in direct):
                armed_pending = False
                evs.append({"kind": "ARMED", "prio": 3, "url": None, "tag": "eyes", "title": "👁️ bbwatch armed",
                            "body": "Baseline recorded — changes from here on are pushed.\n" + "\n".join(
                                f"{s.label}: {len(slots[s.name]['programs'])} programs" for s in direct)})
            pushed = deliver(state, events + evs)
            events.clear()
            save_state(state)
            if pushed or time.time() - last_sync >= SYNC_EVERY:
                sync_state()
                last_sync = time.time()
        except Exception as e:  # never let one bad merge kill the watcher
            log(f"merge of {src.label} failed: {type(e).__name__}: {e}")
        finally:
            ack.set()

    while not STOP.is_set() and time.time() < deadline:
        if not args.loop and not any(t.is_alive() for t in threads) and results.empty():
            break
        try:
            handle(results.get(timeout=1))
        except queue.Empty:
            pass
        if hc and last_ok and time.time() - last_ok < 300 and time.time() - last_ping >= 300:
            ping(hc)
            last_ping = time.time()

    # Stop: let in-flight polls finish and merge them, so the saved state is as fresh as possible.
    STOP.set()
    until = time.time() + 90
    while (any(t.is_alive() for t in threads) or not results.empty()) and time.time() < until:
        try:
            handle(results.get(timeout=1))
        except queue.Empty:
            pass
    if events:
        deliver(state, events)
    save_state(state)
    sync_state()
    log("bbwatch: stopped")


if __name__ == "__main__":
    main()
