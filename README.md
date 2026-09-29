# bbwatch

Near-real-time bug-bounty program & scope watcher → ntfy push.

It polls each platform's **own** public endpoints (no mirror in between),
each on its own thread and cadence, diffs every program *and its scope*
against the last snapshot, and pushes one notification per changed program —
naming the exact assets that were added.

| Platform  | Every | Endpoint                                | How changes are caught                         |
|-----------|-------|-----------------------------------------|------------------------------------------------|
| HackerOne | 45s   | `hackerone.com/graphql`                 | all teams + scopes; >100-scope teams refetched on count change / rotation |
| Bugcrowd  | 60s   | `bugcrowd.com/engagements.json`         | listing every poll; brief changelog on rotation (~12 min sweep) |
| Intigriti | 30s   | Algolia `programs_prod` index           | `lastUpdatedAt` → detail refetch (+ rotation)  |
| YesWeHack | 45s   | `api.yeswehack.com/programs`            | `last_update_at`/`scopes_count` → detail refetch (+ rotation) |
| Immunefi  | 30s   | `immunefi.com/public-api/bounties.json` | full feed with ETag — a `304` costs nothing    |
| Cantina   | 60s   | `cantina.xyz/api/v0/{bounties,competitions}` | bounties with inline scope + competitions |
| HackenProof | 60s | `hackenproof.com/programs-api/programs` | bounties + audit contests; `updated_at` → scope refetch (+ rotation) |
| Sherlock  | 60s   | `mainnet-contest.sherlock.xyz`          | contests + bug bounties; `last_updated` → scope refetch |
| Code4rena | 2m    | `code4rena.com/api/v1/audits`           | contests (upcoming → live → judging → ended)   |
| CodeHawks | 60s   | `codehawks.cyfrin.io/trpc`              | competitive audits + First Flights             |
| Standoff 365 | 3m | `api.standoff365.com/api/bug-bounty`    | bug bounties in ₽ (scope is prose only)        |
| IssueHunt | 2m    | `api.issuehunt.io/programs`             | bounties + VDPs in ¥, scope inline             |
| Bugrap    | 2m    | `api.bugrap.io/api/v1/companies`        | web3 bounties (no structured scope)            |
| BugBase   | 2m    | `bugbase.ai/api/hacktivity`             | hosted programs + their assets                 |
| huntr     | 10m   | `huntr.com/challenges` (RSC payload)    | AI red-team challenges                         |
| Mirror    | 5m    | `arkadiyt/bounty-targets-data`          | cross-check only (below)                       |

Contests (Code4rena, CodeHawks, Sherlock, Cantina/HackenProof competitions,
huntr) push when announced (🆕, priority 5 with a prize pool) and when they
go live (🟢, 4), with the prize pool and dates in every message.

Not covered: Hats Finance (shut down 2025-12-31), Remedy (Cloudflare JS
challenge), Safevuln (inactive since 2022), Secure3 (unverified).

**Cross-check.** The arkadiyt mirror is an independent scrape of H1, Bugcrowd,
Intigriti and YesWeHack. Whenever it publishes a new commit, every program is
compared against our snapshot; any in-scope asset the mirror has that we
don't forces an immediate direct refetch of that program. So a change our
own change signals missed still surfaces — from the direct source, with full
detail — within ~30 min at worst. The mirror never notifies by itself.

Pushes, by priority:

- 🆕 new program — **5** with bounties (buzzes), 3 VDP
- 🔭 assets added to scope / asset became bounty-eligible — **4** on a paying program, 3 otherwise
- 🟢 program re-opened, 💰 started paying / max bounty up — **4**
- 🚦 paused/closed, ✂️ scope reduced, ⚠️ removed, bounty down — 2
- ⚠️ a source failing 5 polls in a row (and ✅ when it recovers) — 3

More than 8 changes in one poll: the top 7 are pushed individually, the rest
as one digest. A program must be missing from 3 polls in a row before it
counts as removed, and a partial listing (<80% of last time) is treated as a
failed poll, so platform hiccups don't produce fake removals/new programs.

## Where it runs — always on

GitHub Actions; nothing runs on your machine. Layers, outermost last:

1. **Self-queued successor.** Each `watch.yml` run watches ~5h40m. Its *first*
   step queues the next run, which the concurrency group holds until the
   current one ends — finished, crashed, timed out or cancelled — so there is
   always a next run. Handover gap ≈ runner start-up (~30s).
2. **In-process resilience.** A failing platform only affects its own thread
   (health push after 5 failures); a bad merge is logged, never fatal; pushes
   that fail are queued and retried; SIGTERM drains in-flight polls and saves.
3. **keeper.yml**, triggered every 5 min, *and* on every watch completion:
   cancels a watch run that stopped syncing state for 30 min (hung), then
   starts a watcher if none is running or queued. Test pushes go from here.
4. **Keep-alive.** Each watch run re-enables `keeper.yml` through the API, so
   GitHub's 60-days-without-activity rule never disables its schedule.
5. **Your laptop** (when on): the `bbwatch-bridge` systemd timer dispatches
   `keeper.yml` every 10 min.
6. **Dead-man switch**: `HEALTHCHECK_URL` is pinged every ~5 min while polls
   succeed — point its alert at ntfy too, so silence itself notifies you.

A restart after real downtime pushes "🩺 resumed after Xh offline"; nothing
is lost, because the first poll diffs against the last snapshot.

State is the orphan branch `state`: one force-pushed commit holding
`state.json.gz` and a readable `status.json`. `main` gets no bot commits.

Secrets: `NTFY_TOPIC` (required; comma-separate several topics for redundant
delivery), `NTFY_TOKEN` (optional), `HEALTHCHECK_URL` (optional).

```sh
gh workflow run keeper.yml -f test=true         # test push
gh workflow run keeper.yml                      # (re)start the watcher if down
git fetch origin state && git show origin/state:status.json    # per-source health
gh workflow disable keeper.yml && gh workflow disable watch.yml  # stop everything
```

Heads-up: GitHub's terms expect Actions to serve the repo's software project;
an always-on polling job is a grey area. The same script runs unchanged
anywhere with Python 3.9+, e.g. an always-free VM with a systemd unit running
`python3 watch.py --loop` (`NTFY_TOPIC` in an `EnvironmentFile`).

## Run locally

```sh
DRY_RUN=1 python3 watch.py                        # one poll of each source, print pushes
DRY_RUN=1 python3 watch.py --loop --sources h1,immunefi
NTFY_TOPIC=<topic> python3 watch.py --test
```

The first poll of each platform records a baseline silently and sends one
"👁️ armed" push. State goes to `.state/` (`STATE_DIR`). `MIN_PRIORITY=3`
drops the low-priority pushes.

## report.py

Local, read-only: ranks the last N hours of H1/Bugcrowd changes into a
candidate shortlist for the hunter pipeline (from the
arkadiyt/bounty-targets-data history, which suits retrospective windows).

```sh
python3 report.py --hours 48 --out ~/BB/pipeline/changes.json
```

## Not covered yet

- Private / invited programs — needs your platform API credentials
  (H1 `/v1/hackers/programs`, Intigriti researcher API, bbscope).
