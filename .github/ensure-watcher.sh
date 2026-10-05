#!/usr/bin/env bash
# Make sure a watch.yml run exists besides run $1 (default: none excluded).
#
# watch.yml calls this first thing with its own run id, so its successor is
# already queued (held back by the concurrency group) and starts the moment the
# current run ends — however it ends: finished, crashed, timed out or
# cancelled. keeper.yml calls it with no argument as the backstop.
#
# A run only counts as a live watcher if it is in_progress, or queued AND
# recently touched by GitHub. A queued run that has sat untouched for
# STALE_SECS is a zombie: it will never start (it is wedged in the queue, not
# waiting on our concurrency group), it holds the concurrency group so nothing
# can ever start, and because it is "not completed" every naive liveness check
# sees it as a healthy watcher. Those get cancelled and replaced.
set -euo pipefail

exclude="${1:-0}"
STALE_SECS="${STALE_SECS:-1800}"

for i in 1 2 3 4 5; do
  runs=$(gh run list --workflow watch.yml --limit 30 \
      --json databaseId,status,createdAt,startedAt 2>/dev/null || echo "[]")

  if live=$(jq --argjson ex "$exclude" --argjson stale "$STALE_SECS" '[
        .[]
        | select(.databaseId != $ex)
        | select(.status == "in_progress"
                 or (.status != "completed"
                     and (((now - (.createdAt | fromdateiso8601)) | floor) < $stale)))
      ] | length' <<<"$runs" 2>/dev/null); then

    if [ "$live" != "0" ]; then
      echo "watch runs already running/queued: $live"
      exit 0
    fi

    # Nothing live. Reap wedged non-completed runs before dispatching, or they
    # keep the concurrency group locked and the fresh run queues forever too.
    for id in $(jq -r '.[] | select(.status != "completed" and .status != "in_progress")
                     | .databaseId' <<<"$runs" 2>/dev/null); do
      echo "run $id is $(
        jq -r --argjson i "$id" '.[] | select(.databaseId == $i) | .status' <<<"$runs"
      ) and stale — cancelling the zombie"
      gh run cancel "$id" || true
    done

    if gh workflow run watch.yml --ref "${GITHUB_REF_NAME:-main}"; then
      echo "dispatched a watch run"
      exit 0
    fi
  fi
  sleep $((i * 10))
done
echo "could not ensure a watch run" >&2
exit 1