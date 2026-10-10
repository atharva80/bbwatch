#!/usr/bin/env python3
"""Build bbwatch's dashboard as one self-contained HTML file.

Reads the two files the watcher publishes to the `state` branch — status.json
(source health, last sync) and changes.json (the rolling change log) — and
renders them into a single page with no external requests, so it works from
GitHub Pages, from a local file, or from a phone.

    python3 dashboard.py --state-dir .state --out dist/index.html
    python3 dashboard.py --fetch --out dist/index.html     # pull from state branch

The page carries a filter box and kind/priority controls that run in the
browser; nothing on it talks to a server, so there is nothing to keep alive
beyond the watcher that produces the JSON.
"""
import argparse
import datetime as dt
import json
import os
import urllib.request

STATE_RAW = "https://raw.githubusercontent.com/atharva80/bbwatch/state"

KIND_LABEL = {
    "NEW": "new program",
    "SCOPE": "scope change",
    "SCOPE_GROW": "scope grew",
    "META": "program update",
    "HEALTH": "watcher health",
    "ARMED": "watcher armed",
}
KIND_CLASS = {"NEW": "new", "SCOPE": "scope", "SCOPE_GROW": "scope", "META": "meta",
              "HEALTH": "health", "ARMED": "health"}
PRIO_CLASS = {5: "p5", 4: "p4", 3: "p3", 2: "p2", 1: "p1"}


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "bbwatch-dashboard"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def load(state_dir, do_fetch):
    status, changes = None, None
    if do_fetch:
        for name, url in (("status.json", f"{STATE_RAW}/status.json"),
                          ("changes.json", f"{STATE_RAW}/changes.json")):
            try:
                data = fetch_json(url)
                if name == "status.json":
                    status = data
                else:
                    changes = data
            except Exception as exc:
                print(f"! {name}: {exc}")
    else:
        for name, target in (("status.json", "status"), ("changes.json", "changes")):
            path = os.path.join(state_dir, name)
            if not os.path.exists(path):
                continue
            with open(path) as f:
                data = json.load(f)
            if target == "status":
                status = data
            else:
                changes = data
    return status, changes


def ago(stamp):
    """Human age of an ISO timestamp, blank when unparseable."""
    try:
        when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        delta = dt.datetime.now(dt.timezone.utc) - when
    except Exception:
        return ""
    secs = int(delta.total_seconds())
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs // 60} min ago"
    if secs < 172800:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def source_rows(status):
    rows = []
    for name, s in sorted(((status or {}).get("sources") or {}).items()):
        err = s.get("error")
        rows.append({
            "name": name,
            "programs": s.get("programs"),
            "ok": not err,
            "error": err,
            "age": ago(dt.datetime.fromtimestamp(s["ok_at"], dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            if s.get("ok_at") else "",
        })
    return rows


def render(status, changes, generated):
    entries = (changes or {}).get("entries") or []
    feed = []
    for e in entries:
        prio = e.get("prio") or 0
        feed.append({
            "ts": e.get("ts"),
            "ago": ago(e.get("ts") or ""),
            "kind": e.get("kind") or "",
            "kind_label": KIND_LABEL.get(e.get("kind") or "", (e.get("kind") or "").lower()),
            "kind_class": KIND_CLASS.get(e.get("kind") or "", "meta"),
            "prio": prio,
            "prio_class": PRIO_CLASS.get(prio, "p1"),
            "tag": e.get("tag") or "",
            "url": e.get("url") or "",
            "title": e.get("title") or "",
            "body": e.get("body") or "",
        })
    updated = (status or {}).get("updated") or ""
    payload = {
        "generated": generated,
        "updated": updated,
        "updated_ago": ago(updated),
        "sources": source_rows(status),
        "outbox": (status or {}).get("outbox"),
        "feed": feed,
    }
    data = json.dumps(payload, ensure_ascii=False)
    return """<!doctype html>
<html lang="en" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>bbwatch — fresh bug-bounty surface</title>
<style>
  :root{
    --bg:#0e1116; --panel:#151a21; --line:#232b36; --text:#e6edf3; --dim:#9aa7b4;
    --accent:#4c8dff; --p5:#3fb950; --p4:#d29922; --p3:#8b949e; --p2:#6e7681;
  }
  @media (prefers-color-scheme: light){
    :root{ --bg:#f6f8fa; --panel:#ffffff; --line:#d8dee4; --text:#1f2328; --dim:#59636e; }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Ubuntu,"Helvetica Neue",sans-serif}
  header{padding:26px 22px 14px;border-bottom:1px solid var(--line)}
  .wrap{max-width:1000px;margin:0 auto}
  h1{margin:0;font-size:20px;letter-spacing:-.01em}
  h1 span{color:var(--dim);font-weight:400}
  .meta{margin-top:6px;color:var(--dim);font-size:13px}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--p5);margin-right:6px;vertical-align:1px}
  .dot.stale{background:var(--p4)}
  section{padding:18px 22px}
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
  .chip{border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:12px;color:var(--dim);background:var(--panel)}
  .chip b{color:var(--text);font-weight:600}
  .chip.bad{border-color:#7d2d2d;color:#ff9c9c}
  .controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 14px}
  input[type=search],select{background:var(--panel);border:1px solid var(--line);color:var(--text);
    border-radius:8px;padding:7px 10px;font-size:14px;min-width:220px}
  select{min-width:0}
  .card{border:1px solid var(--line);background:var(--panel);border-radius:10px;padding:12px 14px;margin:10px 0}
  .card.health{opacity:.75}
  .row{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
  .badge{font-size:11px;text-transform:uppercase;letter-spacing:.04em;border-radius:5px;padding:2px 7px;border:1px solid var(--line);color:var(--dim)}
  .badge.new{color:#7ee787;border-color:#2b5a34}
  .badge.scope{color:#79c0ff;border-color:#264a72}
  .badge.health{color:#d2a8ff;border-color:#4a3766}
  .prio5{border-left:3px solid var(--p5)} .prio4{border-left:3px solid var(--p4)}
  .prio3{border-left:3px solid var(--p3)} .prio2{border-left:3px solid var(--p2)}
  .title{font-weight:600}
  .time{color:var(--dim);font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;margin-left:auto}
  pre{margin:8px 0 0;white-space:pre-wrap;color:var(--dim);font-size:13px;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}
  .empty{color:var(--dim);border:1px dashed var(--line);border-radius:10px;padding:18px;text-align:center}
  footer{padding:8px 22px 30px;color:var(--dim);font-size:12px}
  .count{color:var(--dim);font-size:13px;margin-left:auto}
</style>
</head>
<body>
<header><div class="wrap">
  <h1>bbwatch <span>· fresh bug-bounty surface</span></h1>
  <div class="meta"><span id="liveness" class="dot"></span><span id="liveness-text"></span>
    · source data <span id="updated"></span> · page built <span id="generated"></span></div>
  <div class="chips" id="sources"></div>
</div></header>

<section class="wrap">
  <div class="controls">
    <input type="search" id="q" placeholder="filter changes: program, kind, domain…">
    <select id="kind">
      <option value="">all kinds</option>
      <option value="NEW">new programs</option>
      <option value="SCOPE">scope changes</option>
      <option value="META">program updates</option>
      <option value="HEALTH">watcher health</option>
    </select>
    <select id="prio">
      <option value="">any priority</option>
      <option value="5">priority 5 — live + bounty</option>
      <option value="4">priority 4+</option>
      <option value="3">priority 3+</option>
    </select>
    <span class="count" id="count"></span>
  </div>
  <div id="feed"></div>
</section>
<footer class="wrap">Changes are the watcher's own events, newest first. Click a card's
program link to open it on the platform. Nothing here is filtered for you:
a scope change on a program you cannot submit to is still a scope change.</footer>

<script>
const DATA = __DATA__;
const feed = document.getElementById('feed');
const q = document.getElementById('q');
const kind = document.getElementById('kind');
const prio = document.getElementById('prio');
const count = document.getElementById('count');

document.getElementById('updated').textContent = DATA.updated || 'unknown';
document.getElementById('generated').textContent = (DATA.generated || '').replace('T',' ').replace('Z',' UTC');
const fresh = DATA.updated ? (Date.now() - Date.parse(DATA.updated)) : Infinity;
const live = fresh < 30*60*1000;
const dot = document.getElementById('liveness');
if (!live) dot.classList.add('stale');
document.getElementById('liveness-text').textContent = live
  ? 'watcher live' : 'watcher last synced ' + (DATA.updated_ago || 'unknown');
if (typeof DATA.outbox === 'number' && DATA.outbox > 0)
  document.getElementById('liveness-text').textContent += ' · ' + DATA.outbox + ' queued push(es)';

const sources = document.getElementById('sources');
(DATA.sources || []).forEach(s => {
  const div = document.createElement('span');
  div.className = 'chip' + (s.ok ? '' : ' bad');
  div.innerHTML = '<b>' + s.name + '</b> · ' + (s.programs ?? '?') + ' programs'
    + (s.ok ? '' : ' · ' + (s.error || 'error'));
  sources.appendChild(div);
});

function esc(t){ const d = document.createElement('div'); d.textContent = t || ''; return d.innerHTML; }

function render(){
  const needle = (q.value || '').toLowerCase().trim();
  const k = kind.value, p = parseInt(prio.value || '0', 10);
  const rows = (DATA.feed || []).filter(e => {
    if (k && e.kind_class !== (k === 'SCOPE' ? 'scope' : k.toLowerCase())) return false;
    if (p && (e.prio || 0) < p) return false;
    if (!needle) return true;
    return (e.title + ' ' + e.body + ' ' + e.tag + ' ' + e.url).toLowerCase().includes(needle);
  });
  count.textContent = rows.length + ' of ' + (DATA.feed || []).length + ' change(s)';
  feed.innerHTML = '';
  if (!rows.length){
    feed.innerHTML = '<div class="empty">' + ((DATA.feed || []).length
      ? 'no change matches this filter'
      : 'no changes recorded yet — the watcher logs them as they arrive') + '</div>';
    return;
  }
  for (const e of rows){
    const card = document.createElement('div');
    card.className = 'card prio' + (e.prio || 1) + (e.kind_class === 'health' ? ' health' : '');
    card.innerHTML =
      '<div class="row"><span class="badge ' + e.kind_class + '">' + esc(e.kind_label) + '</span>'
      + '<span class="badge">' + esc(e.tag) + '</span>'
      + '<span class="title">' + esc(e.title) + '</span>'
      + '<span class="time" title="' + esc(e.ts) + '">' + esc(e.ago || e.ts) + '</span></div>'
      + (e.body ? '<pre>' + esc(e.body) + '</pre>' : '')
      + (e.url ? '<div style="margin-top:8px"><a href="' + e.url + '" rel="noopener">open program →</a></div>' : '');
    feed.appendChild(card);
  }
}
q.addEventListener('input', render);
kind.addEventListener('change', render);
prio.addEventListener('change', render);
render();
</script>
</body></html>
""".replace("__DATA__", data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default=".state")
    ap.add_argument("--out", default="dist/index.html")
    ap.add_argument("--fetch", action="store_true", help="read status/changes from the state branch")
    args = ap.parse_args()
    status, changes = load(args.state_dir, args.fetch)
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    html = render(status, changes, generated)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        f.write(html)
    print(f"wrote {args.out}: {len(html)} bytes, "
          f"{len((changes or {}).get('entries') or [])} change(s), "
          f"{len((status or {}).get('sources') or {})} source(s)")


if __name__ == "__main__":
    main()
