#!/usr/bin/env bash
# Publish $STATE_DIR (default .state/) as the single commit of the orphan
# branch `state`. Force-pushed every time, so the branch never accumulates
# history and main stays free of bot commits. Uses plumbing only: the
# working tree and the checked-out branch are never touched.
set -euo pipefail

dir="${STATE_DIR:-.state}"
[ -f "$dir/state.json.gz" ] || exit 0

git config user.name >/dev/null || git config user.name "bbwatch-bot"
git config user.email >/dev/null || git config user.email "bbwatch-bot@users.noreply.github.com"

tree=$(
  for f in "$dir"/*; do
    [ -f "$f" ] || continue
    case "$f" in *.tmp) continue ;; esac
    printf '100644 blob %s\t%s\n' "$(git hash-object -w "$f")" "$(basename "$f")"
  done | git mktree
)
commit=$(git commit-tree "$tree" -m "state $(date -u +%FT%TZ)")
git push -q -f origin "$commit:refs/heads/state"
