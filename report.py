#!/usr/bin/env python3
"""Local companion to watch.py: turn recent program changes into a ranked
candidate shortlist for the hunter pipeline.

Read-only. Fetches the upstream bounty-targets data at HEAD and at a chosen
earlier point (default 48h), diffs scope and metadata, and writes a JSON +
markdown report. The point is the *fresh* surface: a program that just added
assets or just appeared is the least-tested surface available, which is worth
more than a large stale program.

Usage:
    python3 report.py --hours 48 --out /home/axrva/BB/pipeline/changes.json
"""
import argparse
import datetime as dt
import json
import urllib.request

REPO = "arkadiyt/bounty-targets-data"
PLATFORMS = {
    "H1": ("hackerone_data.json", "handle"),
    "Bugcrowd": ("bugcrowd_data.json", "url"),
}
# Programs already handled or refused by the scope gate this cycle.
DENY = {
    "hackerone", "gitlab", "slack", "github", "shopify", "indeed", "security",
    "hackerone_bbp", "internet-bug-bounty",
}
WEB_ASSET_TYPES = {"URL", "WILDCARD", "DOMAIN", "IP_ADDRESS", "CIDR", "OTHER"}
META_FIELDS = ("offers_bounties", "max_payout", "max_bounty", "submission_state", "status", "public")
OPEN_STATES = {"open", "public", "yes"}


def _get(url, timeout=180):
    req = urllib.request.Request(
        url, headers={"User-Agent": "bbwatch-report", "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def commit_at(until=None):
    url = f"https://api.github.com/repos/{REPO}/commits?path=data&per_page=1"
    if until:
        url += f"&until={until}"
    c = json.loads(_get(url))[0]
    return c["sha"], c["commit"]["committer"]["date"]


def fetch(sha, name):
    d = json.loads(_get(f"https://raw.githubusercontent.com/{REPO}/{sha}/data/{name}"))
    if isinstance(d, list):
        return d
    return next((v for v in d.values() if isinstance(v, list)), [])


def canon_target(it):
    if isinstance(it, dict):
        core = {
            k: it.get(k)
            for k in ("asset_identifier", "asset_type", "endpoint", "target", "name", "type", "category")
            if it.get(k) is not None
        }
        return json.dumps(core if core else it, sort_keys=True, default=str)
    return str(it)


def target_meta(it):
    if not isinstance(it, dict):
        return {"name": str(it), "type": "UNKNOWN"}
    return {
        "name": it.get("asset_identifier") or it.get("endpoint") or it.get("target") or it.get("name") or str(it),
        "type": it.get("asset_type") or it.get("type") or it.get("category") or "UNKNOWN",
    }


def scope_map(p):
    t = p.get("targets") or {}
    out = {}
    for it in t.get("in_scope") or []:
        out[canon_target(it)] = target_meta(it)
    return out


def open_for_submission(p):
    for field in ("submission_state", "status"):
        v = p.get(field)
        if v is None:
            continue
        return str(v).lower() in OPEN_STATES
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--out", default="/home/axrva/BB/pipeline/changes.json")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    until = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    head_sha, head_date = commit_at()
    old_sha, old_date = commit_at(until)
    print(f"comparing {old_sha[:9]} ({old_date}) -> {head_sha[:9]} ({head_date})")

    candidates = []
    for plat, (fname, keyfield) in PLATFORMS.items():
        try:
            old_rows = {str(p.get(keyfield)): p for p in fetch(old_sha, fname)}
            new_rows = {str(p.get(keyfield)): p for p in fetch(head_sha, fname)}
        except Exception as exc:  # a platform hiccup must not kill the report
            print(f"! {plat}: {exc}")
            continue

        for key, p in new_rows.items():
            if key.lower() in DENY:
                continue
            if not open_for_submission(p):
                continue
            old = old_rows.get(key)
            fresh = []
            kind = None
            if old is None:
                kind = "NEW"
                fresh = [m for m in scope_map(p).values() if m["type"] in WEB_ASSET_TYPES]
            else:
                new_scope, old_scope = scope_map(p), scope_map(old)
                added = [c for c in new_scope if c not in old_scope]
                fresh = [new_scope[c] for c in added if new_scope[c]["type"] in WEB_ASSET_TYPES]
                if fresh:
                    kind = "SCOPE_GROW"
            meta_changes = []
            if old is not None:
                for field in META_FIELDS:
                    if p.get(field) != old.get(field):
                        meta_changes.append(f"{field}: {old.get(field)} -> {p.get(field)}")
            if kind is None and meta_changes:
                kind = "META"  # scope unchanged; state/bounty churn only
            if kind is None:
                continue

            bounties = bool(p.get("offers_bounties") or p.get("max_payout") or p.get("max_bounty"))
            score = len(fresh) + (10 if kind == "NEW" else 0) + (8 if bounties else 0) + len(meta_changes) * 0.5
            candidates.append(
                {
                    "platform": plat,
                    "key": key,
                    "name": p.get("name") or key,
                    "url": p.get("url"),
                    "kind": kind,
                    "bounties": bounties,
                    "max_payout": p.get("max_payout") or p.get("max_bounty"),
                    "submission_state": p.get("submission_state") or p.get("status"),
                    "new_asset_count": len(fresh),
                    "new_assets": fresh[:40],
                    "meta_changes": meta_changes,
                    "score": round(score, 1),
                }
            )

    candidates.sort(key=lambda c: -c["score"])
    report = {
        "since": old_date,
        "until": head_date,
        "head_sha": head_sha,
        "old_sha": old_sha,
        "candidates": candidates,
    }
    import os

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2)

    print(f"\n{len(candidates)} candidates with changes in the window. Top {args.top}:\n")
    for c in candidates[: args.top]:
        fresh = ", ".join(f"{a['name'][:48]}({a['type']})" for a in c["new_assets"][:3])
        print(
            f"  [{c['score']:>5}] {c['platform']:<9} {c['kind']:<11} {c['name'][:34]:<34} "
            f"+{c['new_asset_count']:<3} bounties={'yes' if c['bounties'] else 'no ':<3} "
            f"state={c['submission_state']}\n          {fresh}"
        )
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
