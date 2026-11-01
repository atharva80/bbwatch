#!/usr/bin/env python3
"""Offline tests for the watcher's diff logic — no network, a fake clock.

    python3 tests/test_watch.py

Each case is a false positive (or a regression) seen in production: status flapping, pagination
gaps, renames, fixture programs, target flaps, VDP noise, the IssueHunt shrink wedge.
"""
import json, os, sys, tempfile
os.environ["STATE_DIR"] = tempfile.mkdtemp(prefix="bbwatch-test-"); os.environ["DRY_RUN"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import watch as w
CLOCK = [1_800_000_000.0]
w.time.time = lambda: CLOCK[0]
def tick(sec): CLOCK[0] += sec

class Fake(w.Source):
    name, label = "fake", "Fake"
    def __init__(self): super().__init__(); self.data = {}
    def listing(self, prev, book): return {k: dict(v, assets=dict(v["assets"])) for k, v in self.data.items()}

def P(name, idents, state="open", bounty=True):
    a = {}
    for i in idents: w.add_asset(a, i, "url", True, None)
    return w.program(name, f"https://x/{name}", state, bounty, "$1,000" if bounty else None, assets=a)

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)
def fresh(n=12):
    src = Fake(); st = {"sources": {"fake": {"programs": {}, "book": {}}}}
    src.data = {f"p{i}": P(f"p{i}", [f"p{i}.example.com".replace("example", "ex4mple")]) for i in range(n)}
    run(src, st); return src, st
def run(src, st, sec=60):
    tick(sec); slot = st["sources"]["fake"]
    return w.process(src, src.poll(slot["programs"], slot["book"]), None, 0.1, st, {})
kinds = lambda evs: [(e["kind"], e["prio"]) for e in evs]

# 1 status flap that reverts within the hold: silent both ways
src, st = fresh()
src.data["p1"] = dict(src.data["p1"], state="paused"); e1 = run(src, st)
src.data["p1"] = dict(src.data["p1"], state="open"); e2 = run(src, st, 3600 * 4)
check(e1 == [] and e2 == [], f"pause reverted within 12h is silent ({kinds(e1)}, {kinds(e2)})")

# 2 a pause that holds is announced once, the reopen right away
src.data["p1"] = dict(src.data["p1"], state="paused"); e1 = run(src, st)
e2 = run(src, st, w.STATE_HOLD + 60)
e3 = run(src, st)
src.data["p1"] = dict(src.data["p1"], state="open"); e4 = run(src, st)
check(e1 == [] and kinds(e2) == [("STATE", 2)] and e3 == [] and kinds(e4) == [("OPEN", 4)],
      f"held pause announced once, reopen immediate ({kinds(e1)}, {kinds(e2)}, {kinds(e3)}, {kinds(e4)})")

# 3 pagination flap: missing for 3 quick polls is not gone, and coming back is not new
gone = src.data.pop("p2"); evs = [run(src, st) for _ in range(3)]
src.data["p2"] = gone; back = run(src, st)
check(all(e == [] for e in evs) and back == [], f"3-minute disappearance is silent ({[kinds(e) for e in evs]}, {kinds(back)})")

# 4 a real delisting is announced (dashboard only), and the return is not "new"
first = st["sources"]["fake"]["programs"]["p3"]["first_seen"] if "first_seen" in st["sources"]["fake"]["programs"]["p3"] else None
gone = src.data.pop("p3"); evs = [run(src, st, 20 * 60) for _ in range(4)]
check([kinds(e) for e in evs] == [[], [], [], [("GONE", 2)]],
      f"delisting needs 3 polls AND 45 min, then one GONE prio 2 ({[kinds(e) for e in evs]})")
src.data["p3"] = gone; back = run(src, st, 86400)
check(not any(k == "NEW" for k, _ in kinds(back)), f"relisted program is not NEW ({kinds(back)})")
check(st["sources"]["fake"]["programs"]["p3"].get("first_seen") == first, "relisted program keeps its first-seen date")

# 5 rename: old key vanishes, new key with the same targets appears
old = src.data.pop("p4")
src.data["p4-renamed"] = P("p4 (ex-p4)", list(k.partition("|")[2] for k in old["assets"]))
e = run(src, st)
check([k for k, _ in kinds(e)] == ["META"] and "renamed from p4" in e[0]["title"], f"rename -> one META 'renamed' ({[x['title'] for x in e]})")
check("p4" not in st["sources"]["fake"]["programs"], "old key dropped silently after a rename")

# 6 fixture programs never enter the state
src.data["lab"] = P("Org B Victim Lab", ["pb.lab", "https://r2-p1.invalid", "localhost"])
e = run(src, st)
check(e == [] and "lab" not in st["sources"]["fake"]["programs"], f"placeholder-scope program dropped ({kinds(e)})")
check(not w.placeholder_scope(P("real", ["api.example-corp.com", "app.lab.io"])["assets"]), "real hosts are not placeholders")

# 7 target dropped and re-added within 6h: no new-scope push
src.data["p5"] = P("p5", ["p5.ex4mple.com", "api.p5.io"]); e0 = run(src, st)
src.data["p5"] = P("p5", ["p5.ex4mple.com"]); e1 = run(src, st)
src.data["p5"] = P("p5", ["p5.ex4mple.com", "api.p5.io"]); e2 = run(src, st, 3600)
check(kinds(e0) == [("SCOPE", 4)] and kinds(e1) == [("SCOPE-", 2)] and e2 == [],
      f"target flap: real add pushes, re-add within 6h is silent ({kinds(e0)}, {kinds(e1)}, {kinds(e2)})")

# 8 VDPs stay off the phone
src.data["vdp"] = P("vdp", ["vdp.io"], bounty=False); e1 = run(src, st)
src.data["vdp"] = P("vdp", ["vdp.io", "new.vdp.io"], bounty=False); e2 = run(src, st)
check(kinds(e1) == [("NEW", 2)] and kinds(e2) == [("SCOPE", 2)] and w.MIN_PRIORITY == 3,
      f"VDP new/scope are priority 2, below the push threshold 3 ({kinds(e1)}, {kinds(e2)})")

# 9 a brand-new paying program still pushes instantly
src.data["hot"] = P("hot", ["hot.io"]); e = run(src, st)
check(kinds(e) == [("NEW", 5)], f"new paying program pushes at once ({kinds(e)})")

# 10 regressions from earlier rounds
src, st = fresh()
p0 = st["sources"]["fake"]["programs"]["p0"]
p0["max"] = None  # as H1 records were before reward ranges existed
src.data["p0"] = P("p0", [k.partition("|")[2] for k in p0["assets"]])
src.data["p0"].update(listed="2026-10-01T00:00:00Z", min="$50", meta={"rep": 0}, max="$2,000")
check(run(src, st) == [], "presentation-only fields and max None->value are silent")
check(st["sources"]["fake"]["programs"]["p0"].get("listed"), "listed stored")
src.data["p0"] = P("p0", [k.partition("|")[2] for k in p0["assets"]]); run(src, st)
check(st["sources"]["fake"]["programs"]["p0"].get("listed") == "2026-10-01T00:00:00Z", "listed carried across polls")
src.data["n1"] = P("n1", ["a.n1.io", "b.n1.io"]); run(src, st); w.record_targets()
n1 = lambda: [r for r in json.load(open(w.TARGETS_FILE))["entries"] if r["key"] == "n1"]
check(sorted((r["asset"], r["why"]) for r in n1()) == [("a.n1.io", "new"), ("b.n1.io", "new")], "new program's targets logged")
src.data["n1"] = P("n1", ["a.n1.io", "b.n1.io", "c.n1.io"]); run(src, st); w.record_targets()
check(n1()[0]["asset"] == "c.n1.io" and n1()[0]["why"] == "added", "scope addition logged newest-first")
run(src, st); w.record_targets()
check(len(n1()) == 3, "no duplicate target rows on replay")
keep = dict(list(src.data.items())[:4]); src.data = keep
try:
    run(src, st); check(False, "short listing should raise")
except RuntimeError as e:
    check("partial listing" in str(e), "short listing rejected at first")
st["sources"]["fake"]["book"]["short_since"] = CLOCK[0] - w.SHORT_ACCEPT - 1
e = run(src, st)
check(len(e) == 1 and "shrank" in e[0]["title"] and len(st["sources"]["fake"]["programs"]) == 4,
      f"sustained shrink accepted with one notice, vanished programs dropped ({[x['title'] for x in e]})")
check(run(src, st) == [], "quiet after the rebase")
print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)
