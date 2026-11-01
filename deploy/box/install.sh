#!/usr/bin/env bash
# Install or update bbwatch on a Linux box (instabox, any VPS) as two systemd services:
#   bbwatch      the watcher (python3 watch.py --loop), restarted if it ever exits
#   bbwatch-web  the dashboard + its data + /healthz on $PORT (default 3000)
# Settings live in ~/bbwatch.env (outside the repo), written once with safe defaults: DRY_RUN=1
# until NTFY_TOPIC is set, so a fresh box never double-pushes next to another watcher.
#
#   curl -fsSL https://raw.githubusercontent.com/atharva80/bbwatch/main/deploy/box/install.sh | bash
set -euo pipefail

repo=${BBWATCH_REPO:-https://github.com/atharva80/bbwatch.git}
dir=$HOME/bbwatch
env=$HOME/bbwatch.env

if [ -d "$dir/.git" ]; then git -C "$dir" pull -q --ff-only; else git clone -q "$repo" "$dir"; fi

# First install: start from the published state, so the new box diffs instead of re-baselining.
mkdir -p "$dir/.state"
if [ ! -f "$dir/.state/state.json.gz" ] && git -C "$dir" fetch -q --depth=1 origin state 2>/dev/null; then
  for f in $(git -C "$dir" ls-tree --name-only FETCH_HEAD); do
    git -C "$dir" show "FETCH_HEAD:$f" > "$dir/.state/$f"
  done
  echo "restored state: $(git -C "$dir" log -1 --format=%s FETCH_HEAD)"
fi

if [ ! -f "$env" ]; then
  cat > "$env" <<ENV
STATE_DIR=$dir/.state
STATE_SYNC_CMD=bash $dir/deploy/box/sync.sh
PORT=3000
# No pushes until this box is the only watcher: set NTFY_TOPIC, then remove DRY_RUN.
DRY_RUN=1
#NTFY_TOPIC=
#HEALTHCHECK_URL=
# Back the state up to GitHub at most this often (s). Only on the one live watcher, with push access: a
# second box would overwrite the shared state branch. The page falls back to it when
# the box is slow to reach, so keep it fresh: ~3 MB a push, 10 min = ~13 GB of the 100 GB monthly egress.
#BACKUP_EVERY=600
ENV
  chmod 600 "$env"
fi

# Prefer IPv4 for outgoing connections. Cloud boxes often share one IPv6 address across many tenants,
# and ntfy.sh's anonymous quota is per address: on instabox the shared IPv6 one was exhausted daily (429)
# while IPv4 had room.
grep -qs '^precedence ::ffff:0:0/96  100' /etc/gai.conf ||
  echo 'precedence ::ffff:0:0/96  100' | sudo tee -a /etc/gai.conf >/dev/null

for unit in bbwatch bbwatch-web; do
  sed -e "s|@USER@|$USER|g" -e "s|@DIR@|$dir|g" -e "s|@ENV@|$env|g" "$dir/deploy/box/$unit.service" |
    sudo tee "/etc/systemd/system/$unit.service" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable -q bbwatch bbwatch-web
sudo systemctl restart bbwatch bbwatch-web
systemctl --no-pager --lines=0 status bbwatch bbwatch-web | grep -E "●|Active:"
