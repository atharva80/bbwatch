#!/usr/bin/env python3
"""Build bbwatch's dashboard as one self-contained HTML file.

Two views, both rendered from the files the watcher publishes to the `state`
branch, embedded inline so the page makes no external request (works from
GitHub Pages, a local file, or a phone):

  * Bounties — the full browsable catalog of every program the watcher tracks,
    flattened from state.json.gz, with filters (search, platform, state,
    paying-only, web3/smart-contract, sort by max bounty). This is the default.
  * Changes — the rolling change feed (changes.json): the watcher's own events,
    newest first.

Source health (status.json) heads both.

    python3 dashboard.py --state-dir .state --out dist/index.html
    python3 dashboard.py --fetch --out dist/index.html     # pull from state branch
"""
import argparse
import datetime as dt
import gzip
import json
import os
import re
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

# Which asset types mark a program as "web3 / smart-contract" surface.
WEB3_TYPES = {"smart_contract", "source_code", "blockchain", "contract"}
# States whose scope strings are dropped from the catalog to keep the page
# small — these programs are over, so searching their repos is rarely useful.
DEAD_STATES = {"ended", "closed", "suspended"}


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "bbwatch-dashboard"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "bbwatch-dashboard"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def load(state_dir, do_fetch):
    """Return (status, changes, state) — any of which may be None if missing or
    corrupt. A gap means a lighter page, never a failed build."""
    status = changes = state = None
    if do_fetch:
        try:
            status = fetch_json(f"{STATE_RAW}/status.json")
        except Exception as exc:
            print(f"! status.json: {exc}")
        try:
            changes = fetch_json(f"{STATE_RAW}/changes.json")
        except Exception as exc:
            print(f"! changes.json: {exc}")
        try:
            state = json.loads(gzip.decompress(fetch_bytes(f"{STATE_RAW}/state.json.gz")))
        except Exception as exc:
            print(f"! state.json.gz: {exc}")
    else:
        for name in ("status.json", "changes.json"):
            path = os.path.join(state_dir, name)
            if not os.path.exists(path):
                print(f"! {name}: not present, skipping")
                continue
            try:
                with open(path) as f:
                    data = json.load(f)
            except Exception as exc:
                print(f"! {name}: unusable ({exc}), skipping")
                continue
            if name == "status.json":
                status = data
            else:
                changes = data
        gz = os.path.join(state_dir, "state.json.gz")
        if os.path.exists(gz):
            try:
                with open(gz, "rb") as f:
                    state = json.loads(gzip.decompress(f.read()))
            except Exception as exc:
                print(f"! state.json.gz: unusable ({exc}), skipping")
        else:
            print("! state.json.gz: not present, skipping")
    return status, changes, state


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


def money(m):
    """Best-effort numeric value of a bounty string for sorting ($, €, ¥, ₽…).

    Takes the *first* number in the string (so "$55,000 USDC (plus $20,000)"
    reads as 55000, not the two concatenated) and applies a k/m/million suffix.
    """
    if not m:
        return 0
    s = str(m)
    mo = re.search(r"([0-9][0-9,]*)(\.[0-9]+)?\s*(k|m|million|mln)?", s, re.I)
    if not mo:
        return 0
    try:
        val = float(mo.group(1).replace(",", "") + (mo.group(2) or ""))
    except ValueError:
        return 0
    suffix = (mo.group(3) or "").lower()
    if suffix == "k":
        val *= 1_000
    elif suffix in ("m", "million", "mln"):
        val *= 1_000_000
    return int(val)


def source_rows(status):
    rows = []
    for name, s in sorted(((status or {}).get("sources") or {}).items()):
        err = s.get("error")
        rows.append({
            "name": name,
            "programs": s.get("programs"),
            "ok": not err,
            "error": err,
        })
    return rows


def build_catalog(state):
    """Flatten every source's programs into one trimmed, searchable list."""
    cat = []
    for plat, s in sorted(((state or {}).get("sources") or {}).items()):
        if plat == "mirror" or not isinstance(s, dict):
            continue
        for _pid, r in (s.get("programs") or {}).items():
            if not isinstance(r, dict):
                continue
            assets = r.get("assets") or {}
            types, scopes = set(), []
            for key in assets:
                head, _, ident = str(key).partition("|")
                types.add(head)
                if ident:
                    scopes.append(ident)
            st = r.get("state") or ""
            is_web3 = bool(types & WEB3_TYPES) or any(
                t.lower() in ("smart contract", "web3", "blockchain", "defi")
                for t in (r.get("tags") or []))
            cat.append({
                "p": plat,
                "n": r.get("name") or "",
                "u": r.get("url") or "",
                "m": r.get("max") or "",
                "mv": money(r.get("max")),
                "b": 1 if r.get("bounty") else 0,
                "s": st,
                "t": r.get("tags") or [],
                "c": 1 if r.get("contest") else 0,
                "w": r.get("when") or "",
                "na": len(assets),
                "ty": sorted(types),
                "w3": 1 if is_web3 else 0,
                # scope idents kept only for live programs, capped, to bound size
                "sc": [] if st in DEAD_STATES else scopes[:8],
            })
    # paying + live first, then by bounty value, then name
    live_rank = {"open": 0, "active": 0, "judging": 1, "paused": 2}
    cat.sort(key=lambda r: (live_rank.get(r["s"], 3), -r["mv"], r["n"].lower()))
    return cat


def render(status, changes, catalog, generated):
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
        "catalog": catalog,
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return _TEMPLATE.replace("__DATA__", data)


_TEMPLATE = r"""<!doctype html>
<html lang="en" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>bbwatch — bug-bounty surface</title>
<style>
  :root{
    --bg:#0e1116; --panel:#151a21; --line:#232b36; --text:#e6edf3; --dim:#9aa7b4;
    --accent:#4c8dff; --p5:#3fb950; --p4:#d29922; --p3:#8b949e; --p2:#6e7681;
    --chip:#1b222c;
  }
  @media (prefers-color-scheme: light){
    :root{ --bg:#f6f8fa; --panel:#ffffff; --line:#d8dee4; --text:#1f2328; --dim:#59636e; --chip:#eef1f4; }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Ubuntu,"Helvetica Neue",sans-serif}
  header{padding:22px 16px 12px;border-bottom:1px solid var(--line)}
  .wrap{max-width:1100px;margin:0 auto}
  h1{margin:0;font-size:20px;letter-spacing:-.01em}
  h1 span{color:var(--dim);font-weight:400}
  .meta{margin-top:6px;color:var(--dim);font-size:13px}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--p5);margin-right:6px;vertical-align:1px}
  .dot.stale{background:var(--p4)}
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
  .chip{border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:12px;color:var(--dim);background:var(--panel)}
  .chip b{color:var(--text);font-weight:600}
  .chip.bad{border-color:#7d2d2d;color:#ff9c9c}
  .tabs{display:flex;gap:4px;margin-top:14px}
  .tab{border:1px solid var(--line);border-bottom:none;background:transparent;color:var(--dim);
    border-radius:8px 8px 0 0;padding:8px 16px;font-size:14px;cursor:pointer}
  .tab.on{background:var(--panel);color:var(--text);font-weight:600}
  section{padding:16px}
  .controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 14px}
  input[type=search],select{background:var(--panel);border:1px solid var(--line);color:var(--text);
    border-radius:8px;padding:8px 10px;font-size:14px}
  input[type=search]{flex:1 1 240px;min-width:0}
  label.tg{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--dim);
    border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:7px 10px;cursor:pointer}
  label.tg input{accent-color:var(--accent)}
  .count{color:var(--dim);font-size:13px;margin-left:auto;white-space:nowrap}

  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:10px}
  .b{border:1px solid var(--line);background:var(--panel);border-radius:10px;padding:12px 14px;
    display:flex;flex-direction:column;gap:7px}
  .b.dead{opacity:.6}
  .b .top{display:flex;gap:8px;align-items:baseline}
  .b .nm{font-weight:600;line-height:1.3}
  .b .nm a{color:var(--text)} .b .nm a:hover{color:var(--accent)}
  .b .pay{margin-left:auto;font-weight:700;color:var(--p5);white-space:nowrap;font-size:14px}
  .b .pay.none{color:var(--dim);font-weight:400}
  .tagrow{display:flex;flex-wrap:wrap;gap:5px}
  .t{font-size:11px;border-radius:5px;padding:2px 7px;border:1px solid var(--line);color:var(--dim);background:var(--chip)}
  .t.plat{color:var(--accent);border-color:var(--accent)}
  .t.w3{color:#d2a8ff;border-color:#4a3766}
  .t.st-open,.t.st-active{color:#7ee787;border-color:#2b5a34}
  .t.st-judging{color:#79c0ff;border-color:#264a72}
  .t.st-paused,.t.st-suspended{color:#d29922;border-color:#6b5420}
  .t.st-ended,.t.st-closed{color:var(--dim)}
  .b .sc{color:var(--dim);font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

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
  footer{padding:10px 16px 30px;color:var(--dim);font-size:12px}
  .hide{display:none}
</style>
</head>
<body>
<header><div class="wrap">
  <h1>bbwatch <span>· bug-bounty surface</span></h1>
  <div class="meta"><span id="liveness" class="dot"></span><span id="liveness-text"></span>
    · source data <span id="updated"></span> · built <span id="generated"></span></div>
  <div class="chips" id="sources"></div>
  <div class="tabs">
    <button class="tab on" id="tab-cat" data-view="cat">Bounties</button>
    <button class="tab" id="tab-feed" data-view="feed">Changes</button>
  </div>
</div></header>

<section class="wrap" id="view-cat">
  <div class="controls">
    <input type="search" id="cq" placeholder="search name, url, scope repo, tag…">
    <select id="cplat"><option value="">all platforms</option></select>
    <select id="cstate">
      <option value="live">live (open · active · judging)</option>
      <option value="open">open only</option>
      <option value="">any state</option>
      <option value="ended">ended / past</option>
    </select>
    <select id="csort">
      <option value="mv">sort: max bounty</option>
      <option value="n">sort: name</option>
      <option value="p">sort: platform</option>
    </select>
    <label class="tg"><input type="checkbox" id="cpay">paying only</label>
    <label class="tg"><input type="checkbox" id="cw3">web3 / smart&nbsp;contract</label>
    <span class="count" id="ccount"></span>
  </div>
  <div class="grid" id="cat"></div>
</section>

<section class="wrap hide" id="view-feed">
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

<footer class="wrap">Catalog is every program the watcher tracks, newest snapshot.
Changes are its own events, newest first. Nothing is filtered for eligibility:
a program you cannot submit to is still listed. Max-bounty values mix currencies
and are approximate — they are what the platform advertises.</footer>

<script>
const DATA = __DATA__;

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
  div.innerHTML = '<b>' + esc(s.name) + '</b> · ' + (s.programs ?? '?')
    + (s.ok ? '' : ' · ' + esc(s.error || 'error'));
  sources.appendChild(div);
});

function esc(t){ const d = document.createElement('div'); d.textContent = t == null ? '' : t; return d.innerHTML; }

/* ---- tabs ---- */
document.querySelectorAll('.tab').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
  const v = btn.dataset.view;
  document.getElementById('view-cat').classList.toggle('hide', v !== 'cat');
  document.getElementById('view-feed').classList.toggle('hide', v !== 'feed');
}));

/* ---- catalog ---- */
const CAT = DATA.catalog || [];
const cq = document.getElementById('cq'), cplat = document.getElementById('cplat'),
      cstate = document.getElementById('cstate'), csort = document.getElementById('csort'),
      cpay = document.getElementById('cpay'), cw3 = document.getElementById('cw3'),
      catEl = document.getElementById('cat'), ccount = document.getElementById('ccount');

const platCounts = {};
CAT.forEach(r => { platCounts[r.p] = (platCounts[r.p] || 0) + 1; });
Object.keys(platCounts).sort().forEach(p => {
  const o = document.createElement('option');
  o.value = p; o.textContent = p + ' (' + platCounts[p] + ')';
  cplat.appendChild(o);
});

const LIVE = {open:1, active:1, judging:1};
function renderCat(){
  const needle = (cq.value || '').toLowerCase().trim();
  const words = needle ? needle.split(/\s+/) : [];
  const plat = cplat.value, st = cstate.value, sort = csort.value;
  const payOnly = cpay.checked, w3Only = cw3.checked;
  let rows = CAT.filter(r => {
    if (plat && r.p !== plat) return false;
    if (st === 'live' && !LIVE[r.s]) return false;
    else if (st === 'open' && r.s !== 'open') return false;
    else if (st === 'ended' && LIVE[r.s]) return false;
    if (payOnly && !r.b) return false;
    if (w3Only && !r.w3) return false;
    if (words.length){
      const hay = (r.n + ' ' + r.u + ' ' + r.p + ' ' + (r.t||[]).join(' ')
        + ' ' + (r.sc||[]).join(' ')).toLowerCase();
      if (!words.every(w => hay.includes(w))) return false;
    }
    return true;
  });
  if (sort === 'n') rows.sort((a,b) => a.n.toLowerCase().localeCompare(b.n.toLowerCase()));
  else if (sort === 'p') rows.sort((a,b) => (a.p+a.n).localeCompare(b.p+b.n));
  else rows.sort((a,b) => b.mv - a.mv || a.n.toLowerCase().localeCompare(b.n.toLowerCase()));

  ccount.textContent = rows.length.toLocaleString() + ' of ' + CAT.length.toLocaleString() + ' programs';
  catEl.innerHTML = '';
  if (!rows.length){
    catEl.innerHTML = '<div class="empty">' + (CAT.length
      ? 'no program matches these filters'
      : 'catalog not available — the watcher snapshot has not been published yet') + '</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  const dead = {ended:1, closed:1, suspended:1};
  for (const r of rows.slice(0, 600)){
    const card = document.createElement('div');
    card.className = 'b' + (dead[r.s] ? ' dead' : '');
    const tags = [];
    tags.push('<span class="t plat">' + esc(r.p) + '</span>');
    if (r.s) tags.push('<span class="t st-' + esc(r.s) + '">' + esc(r.s) + '</span>');
    if (r.c) tags.push('<span class="t">contest</span>');
    if (r.w3) tags.push('<span class="t w3">web3</span>');
    (r.t || []).slice(0,3).forEach(t => tags.push('<span class="t">' + esc(t) + '</span>'));
    const scope = (r.na ? r.na + ' asset' + (r.na>1?'s':'') : '')
      + (r.sc && r.sc.length ? ' · ' + esc(r.sc.slice(0,2).join(', ')) : '')
      + (r.w ? ' · ' + esc(r.w) : '');
    card.innerHTML =
      '<div class="top"><span class="nm">' + (r.u
          ? '<a href="' + encodeURI(r.u) + '" rel="noopener" target="_blank">' + esc(r.n) + '</a>'
          : esc(r.n)) + '</span>'
        + '<span class="pay' + (r.b && r.mv ? '' : ' none') + '">'
          + (r.m ? esc(r.m) : (r.b ? 'paid' : 'VDP')) + '</span></div>'
      + '<div class="tagrow">' + tags.join('') + '</div>'
      + (scope ? '<div class="sc">' + scope + '</div>' : '');
    frag.appendChild(card);
  }
  catEl.appendChild(frag);
  if (rows.length > 600){
    const more = document.createElement('div');
    more.className = 'empty';
    more.textContent = 'showing the first 600 — narrow the filters to see the rest';
    catEl.appendChild(more);
  }
}
[cq, cplat, cstate, csort, cpay, cw3].forEach(el =>
  el.addEventListener(el.type === 'checkbox' || el.tagName === 'SELECT' ? 'change' : 'input', renderCat));
renderCat();

/* ---- change feed ---- */
const feed = document.getElementById('feed'), q = document.getElementById('q'),
      kind = document.getElementById('kind'), prio = document.getElementById('prio'),
      count = document.getElementById('count');
function renderFeed(){
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
      + (e.url ? '<div style="margin-top:8px"><a href="' + encodeURI(e.url) + '" rel="noopener" target="_blank">open program →</a></div>' : '');
    feed.appendChild(card);
  }
}
q.addEventListener('input', renderFeed);
kind.addEventListener('change', renderFeed);
prio.addEventListener('change', renderFeed);
renderFeed();
</script>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default=".state")
    ap.add_argument("--out", default="dist/index.html")
    ap.add_argument("--fetch", action="store_true", help="read state from the state branch")
    args = ap.parse_args()
    status, changes, state = load(args.state_dir, args.fetch)
    catalog = build_catalog(state)
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    html = render(status, changes, catalog, generated)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        f.write(html)
    print(f"wrote {args.out}: {len(html)} bytes, "
          f"{len(catalog)} program(s), "
          f"{len((changes or {}).get('entries') or [])} change(s), "
          f"{len((status or {}).get('sources') or {})} source(s)")


if __name__ == "__main__":
    main()
