#!/usr/bin/env bash
# Make sure a watch.yml run exists besides run $1 (default: none excluded).
#
# watch.yml calls this first thing with its own run id, so its successor is
# already queued (held back by the concurrency group) and starts the moment
# the current run ends — however it ends: finished, crashed, timed out or
# cancelled. keeper.yml calls it with no argument as the backstop.
set -euo pipefail

exclude="${1:-0}"
for i in 1 2 3 4 5; do
  if active=$(gh run list --workflow watch.yml --limit 30 --json databaseId,status \
      --jq "[.[] | select(.status != \"completed\" and .databaseId != $exclude)] | length"); then
    if [ "$active" != "0" ]; then
      echo "watch runs already running/queued: $active"
      exit 0
    fi
    if gh workflow run watch.yml --ref "${GITHUB_REF_NAME:-main}"; then
      echo "dispatched a watch run"
      exit 0
    fi
  fi
  sleep $((i * 10))
done
echo "could not ensure a watch run" >&2
exit 1
