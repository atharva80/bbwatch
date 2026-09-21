# bbwatch

24/7 bug-bounty program & scope change watcher → ntfy push.

Runs entirely in GitHub Actions (free, public repo = unlimited minutes).
Every ~10 minutes it polls [arkadiyt/bounty-targets-data](https://github.com/arkadiyt/bounty-targets-data)
(HackerOne / Bugcrowd / Intigriti / YesWeHack public data, refreshed by its
own action every ~30 min), diffs the structured data against the last
snapshot, and pushes typed events to an ntfy topic:

- 🆕 new program (priority 5 — buzzes)
- 🚦 status change (paused / closed / re-opened) and 💰 bounty changes (4)
- 🔭 scope changes: +N / -M assets (3)
- ✏️ minor metadata updates (2, noise-filtered)

Nothing runs on your own machine. Your laptop can be off for weeks; the
alerts keep coming. Downtime never loses events: the source is a commit
history, so a late run replays everything it missed.

## Setup (already done)

- Repo secret `NTFY_TOPIC` — the ntfy topic to publish to
- Optional repo secret `HEALTHCHECK_URL` — dead-man switch; the job pings it
  each run, and *silence* should alert you
- Schedule: `.github/workflows/watch.yml` (edit the `cron:` line to change
  cadence; GitHub's minimum is 5 minutes)

## Run locally

```sh
GITHUB_TOKEN=$(gh auth token) NTFY_TOPIC=<topic> DRY_RUN=1 python3 watch.py
```

`DRY_RUN=1` prints the payload instead of publishing.

## State

`state.json` holds the last processed data commit. The workflow commits it
back after every run — this is also the activity that stops GitHub from
disabling the schedule after 60 idle days.

## Later lanes

- Private programs: bbscope (`poll --db` → `db changes`) on the local rig,
  same ntfy path.
- Program scoring (crowding / bounty / fit) and hunter auto-pack generation
  subscribe to the same event stream.
