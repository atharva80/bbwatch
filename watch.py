#!/usr/bin/env python3
"""bbwatch — 24/7 bug-bounty program & scope change watcher.

Polls arkadiyt/bounty-targets-data (HackerOne / Bugcrowd / Intigriti /
YesWeHack public data, refreshed by its own GitHub Action every ~30 min),
diffs the structured data against the last-seen snapshot, and pushes
typed events to ntfy.

Designed to run inside GitHub Actions on a schedule (see
.github/workflows/watch.yml), but runs anywhere Python 3.9+ is available.

State lives in state.json (committed back by the workflow; that commit is
also what keeps GitHub from disabling the schedule after 60 idle days).

Env:
  NTFY_TOPIC        required — ntfy topic to publish to (e.g. "hunter")
  GITHUB_TOKEN      optional — GitHub API token (Actions provides one)
  HEALTHCHECK_URL   optional — pinged after every successful run
  DRY_RUN=1         optional — print payloads instead of publishing
  MAX_LINES         optional — max event lines per push (default 14)
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO = "arkadiyt/bounty-targets-data"
FILES = [
    "hackerone_data.json",
    "bugcrowd_data.json",
    "intigriti_data.json",
    "yeswehack_data.json",
]
PLATFORM = {
    "hackerone_data.json": "H1",
    "bugcrowd_data.json": "Bugcrowd",
    "intigriti_data.json": "Intigriti",
    "yeswehack_data.json": "YesWeHack",
}
# Fields that churn on their own and must never fire a notification.
NOISE = {
    "hackerone_data.json": {
        "response_efficiency_percentage",
        "average_time_to_bounty_awarded",
        "average_time_to_first_program_response",
        "average_time_to_report_resolved",
        "id",
    },
    "bugcrowd_data.json": {"id"},
    "intigriti_data.json": {"id", "twoFactorRequired", "tacRequired"},
    "yeswehack_data.json": {"id"},
}
KEYFIELD = {
    "hackerone_data.json": "handle",
    "bugcrowd_data.json": "url",
    "intigriti_data.json": "id",
    "yeswehack_data.json": "id",
}
# Field -> event kind when it changes.
FIELD_KIND = [
    ("offers_bounties", "BOUNTY"),
    ("max_payout", "BOUNTY"),
    ("max_bounty", "BOUNTY"),
    ("min_bounty", "BOUNTY"),
    ("submission_state", "STATE"),
    ("status", "STATE"),
    ("disabled", "STATE"),
    ("safe_harbor", "STATE"),
    ("allows_disclosure", "STATE"),
    ("public", "STATE"),
    ("confidentiality_level", "STATE"),
]
PRIO = {"NEW": 5, "GONE": 4, "STATE": 4, "BOUNTY": 4, "SCOPE": 3, "UPDATED": 2, "ARMED": 3}
EMOJI = {"NEW": "🆕", "GONE": "⚠️", "SCOPE": "🔭", "BOUNTY": "💰", "STATE": "🚦", "UPDATED": "✏️"}
STATE_FILE = "state.json"


# ---------- fetching ----------

def _get(url, token=None, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "bbwatch", "Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def latest_commit(token=None):
    data = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits?path=data&per_page=1", token))
    c = data[0]
    return c["sha"], c["commit"]["committer"]["date"]


def fetch_data(sha, name, token=None):
    raw = _get(f"https://raw.githubusercontent.com/{REPO}/{sha}/data/{name}", token)
    d = json.loads(raw)
    if isinstance(d, list):
        return d
    for v in d.values():
        if isinstance(v, list):
            return v
    return []


# ---------- normalizing ----------

def key_of(fname, p):
    return str(p.get(KEYFIELD[fname]) or p.get("name") or "?")


def display(fname, p):
    return str(p.get("name") or key_of(fname, p))


def canon_str(v):
    return json.dumps(v, sort_keys=True, default=str)


def target_sets(fname, p):
    t = p.get("targets") or {}
    def items(lst):
        out = []
        for it in lst:
            if isinstance(it, dict):
                core = {k: it.get(k) for k in
                        ("asset_identifier", "asset_type", "endpoint", "target", "name", "type", "category")
                        if it.get(k) is not None}
                out.append(canon_str(core if core else it))
            else:
                out.append(str(it))
        return sorted(set(out))
    return items(t.get("in_scope") or []), items(t.get("out_of_scope") or [])


def new_program_brief(fname, p):
    parts = []
    if "offers_bounties" in p:
        parts.append("bounties: " + ("yes" if p.get("offers_bounties") else "no"))
    if p.get("submission_state"):
        parts.append(f"state: {p['submission_state']}")
    if p.get("min_bounty") is not None or p.get("max_bounty") is not None:
        parts.append(f"bounty: {p.get('min_bounty', '?')}–{p.get('max_bounty', '?')}")
    if p.get("max_payout") is not None:
        parts.append(f"max payout: {p['max_payout']}")
    ins, outs = target_sets(fname, p)
    parts.append(f"scope: {len(ins)} in / {len(outs)} out")
    return " · ".join(parts)


def diff_program(fname, old, new):
    ev = []
    o_ins, o_outs = target_sets(fname, old)
    n_ins, n_outs = target_sets(fname, new)
    if (o_ins, o_outs) != (n_ins, n_outs):
        added = len(set(n_ins) - set(o_ins)) + len(set(n_outs) - set(o_outs))
        removed = len(set(o_ins) - set(n_ins)) + len(set(o_outs) - set(n_outs))
        ev.append(("SCOPE", f"scope: +{added} / -{removed}"))
    for fld, kind in FIELD_KIND:
        if fld in old or fld in new:
            if canon_str(old.get(fld)) != canon_str(new.get(fld)):
                ev.append((kind, f"{fld}: {canon_str(old.get(fld))} → {canon_str(new.get(fld))}"))
    reported = {"targets"} | {f for f, _ in FIELD_KIND}
    o_c = {k: v for k, v in old.items() if k not in NOISE[fname] and k not in reported}
    n_c = {k: v for k, v in new.items() if k not in NOISE[fname] and k not in reported}
    diffs = sorted(k for k in set(o_c) | set(n_c) if canon_str(o_c.get(k)) != canon_str(n_c.get(k)))
    if diffs:
        ev.append(("UPDATED", "changed: " + ", ".join(diffs[:4])))
    return ev


# ---------- events -> message ----------

def assemble(events, checked_at):
    max_lines = int(os.environ.get("MAX_LINES", "14"))
    prio = max(PRIO[e[0]] for e in events)
    lines = []
    click = None
    for kind, plat, label, detail, url in events:
        tag = EMOJI[kind]
        if kind == "GONE":
            lines.append(f"{tag} {plat} · {label} — removed from dataset")
        else:
            lines.append(f"{tag} {plat} · {label} — {detail}")
        if click is None and url and kind in ("NEW", "SCOPE", "BOUNTY", "STATE"):
            click = url
    extra = 0
    if len(lines) > max_lines:
        extra = len(lines) - max_lines
        lines = lines[:max_lines]
    body = "\n".join(lines)
    if extra:
        body += f"\n… +{extra} more"
    body += f"\nchecked {checked_at}"
    if len(events) == 1:
        kind, plat, label, detail, url = events[0]
        title = {
            "NEW": f"🆕 New program: {label} ({plat})",
            "GONE": f"⚠️ Program removed: {label} ({plat})",
            "SCOPE": f"🔭 Scope update: {label} ({plat})",
            "BOUNTY": f"💰 Bounty change: {label} ({plat})",
            "STATE": f"🚦 Status change: {label} ({plat})",
            "UPDATED": f"✏️ Update: {label} ({plat})",
        }[kind]
    else:
        title = f"📡 bbwatch: {len(events)} events ({len(set(e[2] for e in events))} programs)"
    return title, body, prio, click


def publish(topic, title, message, priority, click=None):
    payload = {"topic": topic, "title": title, "message": message, "priority": priority}
    if click:
        payload["click"] = click
    if os.environ.get("DRY_RUN"):
        print("DRY_RUN payload:\n" + json.dumps(payload, indent=2, ensure_ascii=False))
        return
    req = urllib.request.Request(
        "https://ntfy.sh", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "bbwatch"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"ntfy publish: HTTP {r.status}")


# ---------- main ----------

def main():
    token = os.environ.get("GITHUB_TOKEN")
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        sys.exit("NTFY_TOPIC is required")
    hc = os.environ.get("HEALTHCHECK_URL")

    new_sha, committed_at = latest_commit(token)
    print(f"latest data commit: {new_sha[:8]} at {committed_at}")

    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
    last = state.get("sha")

    if last == new_sha:
        print("no new data commit since last run — nothing to do")
        ping(hc)
        return

    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    events = []
    if last:
        for fname in FILES:
            try:
                old = fetch_data(last, fname, token)
                new = fetch_data(new_sha, fname, token)
            except urllib.error.HTTPError as e:
                print(f"{fname}: fetch failed ({e}); skipping")
                continue
            om = {key_of(fname, p): p for p in old}
            nm = {key_of(fname, p): p for p in new}
            plat = PLATFORM[fname]
            for k in sorted(set(nm) - set(om)):
                events.append(("NEW", plat, display(fname, nm[k]), new_program_brief(fname, nm[k]), nm[k].get("url")))
            for k in sorted(set(om) - set(nm)):
                events.append(("GONE", plat, display(fname, om[k]), "", om[k].get("url")))
            for k in sorted(set(om) & set(nm)):
                for kind, detail in diff_program(fname, om[k], nm[k]):
                    events.append((kind, plat, display(fname, nm[k]), detail, nm[k].get("url")))
        if events:
            title, body, prio, click = assemble(events, checked_at)
            print(f"{len(events)} events -> {title}")
            publish(topic, title, body, prio, click)
        else:
            print("data commit advanced but no notifiable changes (noise-only)")
    else:
        # First run: baseline only — remember where we are, tell the user we're live.
        print("first run: recording baseline")
        publish(topic, "👁️ bbwatch armed", f"Watching public bug-bounty programs 24/7.\nBaseline: {new_sha[:8]}\nNext runs notify on new programs / scope / bounty / status changes.", PRIO["ARMED"])

    state = {"sha": new_sha, "committed_at": committed_at, "checked_at": checked_at,
             "events_last_run": len(events)}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    ping(hc)


def ping(hc):
    if not hc:
        return
    try:
        urllib.request.urlopen(hc, timeout=15).read()
        print("healthcheck pinged")
    except Exception as e:
        print(f"healthcheck ping failed: {e}")


if __name__ == "__main__":
    main()
