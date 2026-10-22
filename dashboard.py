#!/usr/bin/env python3
"""Build the data files and the static site behind bbwatch's dashboard.

The page (web/index.html) is a static app. It loads its data at runtime straight
from the `state` branch (raw.githubusercontent.com), so it is as fresh as the
watcher's last sync whatever the keeper's schedule does; the copies deployed with
the page under data/ are only a fallback for when that fetch fails.

    python3 dashboard.py data --state-dir .state      # write catalog.json + latest.json next to the state
    python3 dashboard.py site --state-dir .state --out dist   # the Pages artifact: index.html + data/

`data` runs from sync-state.sh on every state sync; `site` runs in keeper.yml.
Both read only what the watcher already wrote: state.json.gz (every program and
its scope), status.json (source health), changes.json (the change log) and
targets.json (scope additions the watcher saw).
"""
import argparse
import datetime as dt
import gzip
import json
import os
import re
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")

LABELS = {"h1": "HackerOne", "bugcrowd": "Bugcrowd", "intigriti": "Intigriti", "ywh": "YesWeHack",
          "immunefi": "Immunefi", "cantina": "Cantina", "hackenproof": "HackenProof", "sherlock": "Sherlock",
          "code4rena": "Code4rena", "codehawks": "CodeHawks", "standoff": "Standoff 365", "issuehunt": "IssueHunt",
          "bugrap": "Bugrap", "bugbase": "BugBase", "huntr": "huntr"}

# Asset types as each platform names them -> the handful of categories a hunter filters on.
TYPE_CAT = {
    "url": "web", "website": "web", "web": "web", "domain": "web", "websites_and_applications": "web",
    "web-application": "web", "web_application": "web", "web app": "web", "website testing": "web",
    "wildcard": "wildcard", "api": "api", "api testing": "api", "graphql": "api",
    "android": "mobile", "ios": "mobile", "mobile": "mobile", "google_play_app_id": "mobile",
    "apple_store_app_id": "mobile", "other_apk": "mobile", "other_ipa": "mobile", "testflight": "mobile",
    "mobile-application": "mobile", "mobile_application": "mobile", "mobile-application-android": "mobile",
    "mobile-application-ios": "mobile", "android application": "mobile", "ios application": "mobile",
    "smart_contract": "contract", "smart contract": "contract", "contract": "contract", "blockchain": "contract",
    "blockchain_dlt": "contract", "protocol": "contract", "smart_contracts": "contract",
    "source_code": "code", "source code": "code", "github": "code", "repository": "code",
    "cidr": "network", "iprange": "network", "ip_address": "network", "ip": "network", "network": "network",
    "executable": "desktop", "downloadable_executables": "desktop", "windows_app_store_app_id": "desktop",
    "application": "desktop", "desktop": "desktop",
    "hardware": "hardware", "device": "hardware", "iot": "hardware",
    "ai_model": "ai", "ai": "ai", "llm": "ai",
}

# Rough USD rates, only to rank rewards across currencies. Display always keeps the original.
FX = {"$": 1, "USD": 1, "USDC": 1, "USDT": 1, "DAI": 1, "€": 1.08, "EUR": 1.08, "£": 1.27, "GBP": 1.27,
      "¥": 0.0066, "JPY": 0.0066, "₽": 0.011, "RUB": 0.011, "CHF": 1.12, "AUD": 0.65, "CAD": 0.73,
      "INR": 0.012, "SGD": 0.74}

LIVE = {"open", "active", "live", "upcoming", "judging"}
DEAD = {"ended", "closed", "suspended", "archived", "finished"}
LATEST_DAYS = 90       # Latest targets looks back this far
LATEST_KEEP = 8000
SCOPE_SAMPLE = 8       # identifiers per live program kept for search and the card preview


# ---------- parsing helpers ----------

def to_dt(v):
    """ISO string, unix seconds/millis, "17 Nov 2025" or a JS Date.toString() -> aware datetime, else None."""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            return dt.datetime.fromtimestamp(v / 1000 if v > 1e12 else v, dt.timezone.utc)
        s = str(v).strip().split("|", 1)[0]           # "<date>|<extra>" sigs
        if s.isdigit():
            n = int(s)
            return dt.datetime.fromtimestamp(n / 1000 if n > 1e12 else n, dt.timezone.utc)
        if "GMT" in s:                                 # "Mon Mar 11 2024 01:36:34 GMT+0000 (…)"
            d = dt.datetime.strptime(s.split(" GMT", 1)[0], "%a %b %d %Y %H:%M:%S")
        else:
            try:
                d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                d = dt.datetime.strptime(s, "%d %b %Y")
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


_MIN = dt.datetime(2012, 1, 1, tzinfo=dt.timezone.utc)


def ts(v):
    """Unix seconds for a believable date (2012 … a year ahead), else 0 — rejects counts/prices posing as dates."""
    d = to_dt(v)
    if d and _MIN <= d <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=400):
        return int(d.timestamp())
    return 0


def usd(m):
    """Approximate USD value of a reward string ("$55,000 USDC (plus …)", "€1,000", "₽60,000,000"), 0 if unknown.
    Takes the first number, so a parenthetical second amount does not concatenate into it."""
    if not m:
        return 0
    s = str(m)
    mo = re.search(r"([0-9][0-9,]*)(\.[0-9]+)?\s*(k|m|million|mln)?\b", s, re.I)
    if not mo:
        return 0
    val = float(mo.group(1).replace(",", "") + (mo.group(2) or ""))
    suf = (mo.group(3) or "").lower()
    val *= 1_000 if suf == "k" else 1_000_000 if suf in ("m", "million", "mln") else 1
    rate = next((FX[c] for c in ("$", "€", "£", "¥", "₽") if c in s), None)
    if rate is None:
        code = re.search(r"\b([A-Z]{3,4})\b", s)
        rate = FX.get(code.group(1)) if code else None
    return int(val * rate) if rate else 0


def type_cat(typ, ident=""):
    t = str(typ or "").strip().lower()
    if t in TYPE_CAT:
        cat = TYPE_CAT[t]
    elif "contract" in t or "blockchain" in t:
        cat = "contract"
    elif "android" in t or "ios" in t or "mobile" in t or "apk" in t:
        cat = "mobile"
    elif "api" in t:
        cat = "api"
    elif "source" in t or "code" in t or "github" in t:
        cat = "code"
    elif "web" in t or "url" in t or "domain" in t or "site" in t:
        cat = "web"
    else:
        cat = "other"
    if cat in ("web", "other") and str(ident).startswith("*."):
        return "wildcard"
    if cat == "other" and str(ident).startswith("0x"):
        return "contract"
    return cat


# ---------- loading ----------

def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as exc:
        print(f"! {os.path.basename(path)}: {'not present' if not os.path.exists(path) else exc}")
        return None


def load(state_dir):
    """(status, changes, state, targets) — any of them may be None; a gap means a lighter page, never a failure."""
    state = None
    gz = os.path.join(state_dir, "state.json.gz")
    try:
        with gzip.open(gz, "rt") as f:
            state = json.load(f)
    except Exception as exc:
        print(f"! state.json.gz: {'not present' if not os.path.exists(gz) else exc}")
    return (read_json(os.path.join(state_dir, "status.json")), read_json(os.path.join(state_dir, "changes.json")),
            state, read_json(os.path.join(state_dir, "targets.json")))


# ---------- the catalog ----------

def program_tags(r):
    tags = list(r.get("tags") or [])
    meta = r.get("meta") or {}
    if "rep" in meta:
        tags.append("no rep needed" if not meta["rep"] else f"rep ≥ {meta['rep']}")
    if meta.get("fee"):
        tags.append(f"fee {meta['fee']}")
    return tags


def build_catalog(state):
    out = []
    for plat, src in sorted(((state or {}).get("sources") or {}).items()):
        if plat == "mirror" or not isinstance(src, dict):
            continue
        for key, r in (src.get("programs") or {}).items():
            if not isinstance(r, dict):
                continue
            assets = r.get("assets") or {}
            live_scope = [(k, v) for k, v in assets.items() if v and v[0]]
            cats, idents = set(), []
            paid_n = 0
            for k, v in sorted(live_scope, key=lambda kv: (not (kv[1][1] is True or (kv[1][1] is None and r.get("bounty"))), kv[0])):
                typ, _, ident = k.partition("|")
                cats.add(type_cat(typ, ident))
                idents.append(ident)
                if v[1] is True or (v[1] is None and r.get("bounty")):
                    paid_n += 1
            st = r.get("state") or ""
            first = ts(r.get("first_seen"))
            listed_p = ts(r.get("listed")) or ts(r.get("date"))   # `date` = the field's name before `listed`
            updated = max(ts(r.get("updated")), ts(r.get("sig")), ts(r.get("scope_changed")))
            meta = r.get("meta") or {}
            out.append({
                "id": f"{plat}:{key}",
                "p": plat,
                "n": r.get("name") or "",
                "u": r.get("url") or "",
                "s": st,
                "b": 1 if r.get("bounty") else 0,
                "c": 1 if r.get("contest") else 0,
                "w": r.get("when") or "",
                "mx": r.get("max") or "",
                "mn": r.get("min") or "",
                "usd": usd(r.get("max")),
                "t": program_tags(r),
                "m": {k: meta[k] for k in ("rep", "reports", "resp") if k in meta},
                "fee": 1 if meta.get("fee") else 0,
                "ty": sorted(cats),
                "na": len(live_scope),
                "np": paid_n,
                "sc": [] if st in DEAD else idents[:SCOPE_SAMPLE],
                "L": listed_p or first,           # listing date: the platform's, else when bbwatch first saw it
                "Lw": 0 if listed_p else (1 if first else 0),
                "U": max(updated, first),          # last activity: platform update, scope growth, discovery
                "S": ts(r.get("scope_changed")),
                "F": first,
            })
    out.sort(key=lambda r: (-r["L"], r["n"].lower()))
    return out


# ---------- latest targets ----------

def build_latest(state, targets, catalog):
    """Recently added in-scope targets: the platform's own per-asset dates (H1, Immunefi) merged with the
    additions the watcher observed (every platform), one row per (program, asset), newest first."""
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    since = now - LATEST_DAYS * 86400
    by_id = {r["id"]: r for r in catalog}
    rows = {}

    def put(row):
        k = (row["id"], row["a"])
        old = rows.get(k)
        if old is None:
            rows[k] = row
        else:  # keep the platform's date when there is one (it is exact), the watcher's reason otherwise
            if row.get("pd") and not old.get("pd"):
                row["w"] = old["w"] if old["w"] != "added" else row["w"]
                rows[k] = row
            elif old.get("w") == "added" and row.get("w") != "added":
                old["w"] = row["w"]

    for plat, src in ((state or {}).get("sources") or {}).items():
        for key, r in ((src or {}).get("programs") or {}).items():
            added = r.get("added") or {}
            if not added:
                continue
            assets = r.get("assets") or {}
            pid = f"{plat}:{key}"
            prog = by_id.get(pid) or {}
            launched = prog.get("L") or 0
            for k, when in added.items():
                t = ts(when)
                a = assets.get(k)
                if t < since or not a or not a[0]:
                    continue
                typ, _, ident = k.partition("|")
                put({"t": t, "id": pid, "p": plat, "n": r.get("name") or "", "u": r.get("url") or "",
                     "a": ident, "y": typ, "c": type_cat(typ, ident),
                     "$": 1 if (a[1] is True or (a[1] is None and r.get("bounty"))) else 0,
                     "w": "new" if launched and t <= launched + 3 * 86400 and launched >= since else "added",
                     "mx": r.get("max") or "", "pd": 1})

    for e in (targets or {}).get("entries") or []:
        t = ts(e.get("ts"))
        if t < since:
            continue
        pid = f"{e.get('src')}:{e.get('key')}"
        ident = e.get("asset") or ""
        put({"t": t, "id": pid, "p": e.get("src") or "", "n": e.get("name") or "", "u": e.get("url") or "",
             "a": ident, "y": e.get("type") or "", "c": type_cat(e.get("type"), ident),
             "$": 1 if e.get("paid") else 0, "w": e.get("why") or "added", "mx": e.get("max") or ""})

    out = sorted(rows.values(), key=lambda r: (-r["t"], r["n"].lower(), r["a"]))[:LATEST_KEEP]
    return out


# ---------- writing ----------

def write_json(path, doc):
    with open(path + ".tmp", "w") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(path + ".tmp", path)


def build_data(state_dir, out_dir):
    status, changes, state, targets = load(state_dir)
    os.makedirs(out_dir, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = (status or {}).get("updated") or stamp
    catalog = build_catalog(state)
    latest = build_latest(state, targets, catalog)
    if state is not None:  # never replace a good published catalog with an empty one
        write_json(os.path.join(out_dir, "catalog.json"),
                   {"generated": stamp, "updated": updated, "labels": LABELS, "programs": catalog})
        write_json(os.path.join(out_dir, "latest.json"), {"generated": stamp, "updated": updated, "entries": latest})
    print(f"data: {len(catalog)} programs, {len(latest)} latest targets -> {out_dir}")
    return status, changes


def build_site(state_dir, out):
    data = os.path.join(out, "data")
    status, changes = build_data(state_dir, data)
    for name, doc in (("status.json", status), ("changes.json", changes)):
        if doc is not None:
            write_json(os.path.join(data, name), doc)
    for f in os.listdir(WEB):
        src = os.path.join(WEB, f)
        if os.path.isfile(src):
            shutil.copy(src, os.path.join(out, f))
    print(f"site -> {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["data", "site"])
    ap.add_argument("--state-dir", default=".state")
    ap.add_argument("--out", default="dist", help="site output directory (site only)")
    args = ap.parse_args()
    if args.cmd == "data":
        build_data(args.state_dir, args.state_dir)
    else:
        build_site(args.state_dir, args.out)


if __name__ == "__main__":
    main()
