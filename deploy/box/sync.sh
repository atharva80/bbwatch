#!/usr/bin/env bash
# The box's STATE_SYNC_CMD, run by the watcher after it saves state:
#   - rebuild the dashboard's data files (catalog, latest targets, scope shards) every time;
#   - back the state up to GitHub's `state` branch at most every BACKUP_EVERY seconds, when set.
# Unlike the GitHub-hosted watcher this box serves the data itself, so the 10 MB force-push is a
# backup, not the delivery path, and stays rare (the free plan's egress is 100 GiB a month).
set -uo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
dir=${STATE_DIR:-$root/.state}

python3 "$root/dashboard.py" data --state-dir "$dir" >/dev/null || echo "dashboard data build failed" >&2

stamp="$dir/.last-backup"   # a dotfile: sync-state.sh publishes "$dir"/*, which skips it
if [ -n "${BACKUP_EVERY:-}" ]; then
  last=$(stat -c %Y "$stamp" 2>/dev/null || echo 0)
  if [ $(( $(date +%s) - last )) -ge "$BACKUP_EVERY" ]; then
    (cd "$root" && STATE_DIR="$dir" bash .github/sync-state.sh) && touch "$stamp" || echo "state backup failed" >&2
  fi
fi
exit 0
