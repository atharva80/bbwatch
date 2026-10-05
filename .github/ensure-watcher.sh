#!/usr/bin/env bash
# Make sure a watch.yml run exists besides run $1 (default: none excluded).
#
# watch.yml calls this first thing with its own run id, so its successor is
# already queued (held back by the concurrency group) and starts the moment the
# current run ends — however it ends: finished, crashed, timed out or
# cancelled. keeper.yml calls it with no argument as the backstop.
#
# A run only counts as a live watcher if it is in_progress, or queued AND
# recently created. A queued run that has sat untouched for STALE_SECS is a
# zombie: GitHub will not start it (it is wedged in the queue, not waiting on
# our concurrency group), it holds the bbwatch concurrency group, and because
# it is "not completed" every naive liveness check sees it as a healthy
# watcher. Those are reported, cancelled, and then replaced.
#
# Cancelling a wedged run is best-effort: the API answers 409 "not in progress"
# for runs stuck in pending/queued, and neither disabling nor re-enabling the
# workflow clears them. Dispatching still succeeds around one, so a zombie is
# logged and tolerated rather than being a reason to do nothing.
set -euo pipefail

exclude="${1:-0}"
STALE_SECS="${STALE_SECS:-1800}"
# Only runs from this window can prove liveness or be reaped. Old wedged runs
# cannot be cleared, so without this they accumulate and --limit 30 eventually
# pushes the real runs out of the query. It must stay comfortably wider than
# STALE_SECS and than the longest expected outage, or a genuinely wedged
# watcher falls out of the window and stops being reported at all.
WINDOW_SECS="${WINDOW_SECS:-604800}"

for i in 1 2 3 4 5; do
  runs=$(gh run list --workflow watch.yml --limit 30 \
      --json databaseId,status,createdAt,startedAt 2>/dev/null || echo "[]")

  if live=$(jq --argjson ex "$exclude" --argjson stale "$STALE_SECS" --argjson win "$WINDOW_SECS" '[
        .[]
        | select(.databaseId != $ex)
        | select(
            ((now - (.createdAt | fromdateiso8601)) | floor) < $win
            and (.status == "in_progress"
                 or (.status != "completed"
                     and (((now - (.createdAt | fromdateiso8601)) | floor) < $stale)))
          )
      ] | length' <<<"$runs" 2>/dev/null); then

    if [ "$live" != "0" ]; then
      echo "watch runs already running/queued: $live"
      exit 0
    fi

    # Nothing live. Report and try to clear wedged non-completed runs before
    # dispatching. gh run cancel can fail here; that is expected and does not
    # block the dispatch below.
    for id in $(jq -r --argjson win "$WINDOW_SECS" '[
          .[]
          | select(.status != "completed" and .status != "in_progress")
          | select(((now - (.createdAt | fromdateiso8601)) | floor) < $win)
        ] | .[].databaseId' <<<"$runs" 2>/dev/null); do
      echo "run $id is $(jq -r --argjson i "$id" '.[] | select(.databaseId == $i) | .status' <<<"$runs") \
and wedged — cancelling (best effort)" >&2
      gh run cancel "$id" >&2 || echo "run $id could not be cancelled (GitHub refuses pending/queued runs); continuing" >&2
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