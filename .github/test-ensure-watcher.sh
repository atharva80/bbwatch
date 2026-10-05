#!/usr/bin/env bash
# Exercise ensure-watcher.sh against a fake `gh`, covering the scenarios that
# actually broke in production.
set -uo pipefail

PASS=0; FAIL=0
ok()  { echo "  PASS $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL $1: $2"; FAIL=$((FAIL+1)); }

run_case() { # name runs.json exclude expect_live expect_cancel expect_dispatch
  local name="$1" runs="$2" exclude="$3" exp_live="$4" exp_cancel="$5" exp_disp="$6"
  local d; d=$(mktemp -d)
  # CANCEL_FAILS=1 makes `gh run cancel` exit 1, the way GitHub's API really
  # behaves for a run wedged in pending/queued (HTTP 409).
  cat >"$d/gh" <<'EOF'
#!/usr/bin/env bash
case "$1 $2" in
  "run list")     cat "$FAKE_RUNS" ;;
  "run cancel")   [ "${CANCEL_FAILS:-0}" = 1 ] && exit 1
                  echo "CANCELLED $3" >> "$FAKE_LOG"; exit 0 ;;
  "workflow run") echo "DISPATCHED" >> "$FAKE_LOG"; exit 0 ;;
esac
EOF
  chmod +x "$d/gh"; : >"$d/log"
  FAKE_RUNS="$runs" FAKE_LOG="$d/log" CANCEL_FAILS="${CANCEL_FAILS:-0}" PATH="$d:$PATH" \
    bash /tmp/bbw/.github/ensure-watcher.sh "$exclude" >"$d/out" 2>&1
  local live disp ncancel
  live=$(grep -oP 'already running/queued: \K\d+' "$d/out" || echo 0)
  disp=$(grep -c 'DISPATCHED' "$d/log" || true)
  ncancel=$(grep -c 'CANCELLED' "$d/log" || true)

  if [ "$live" = "$exp_live" ] && [ "$disp" = "$exp_disp" ] && [ "$ncancel" = "$exp_cancel" ]; then
    ok "$name (live=$live dispatch=$disp cancels=$ncancel)"
  else
    bad "$name" "got live=$live dispatch=$disp cancels=$ncancel; want live=$exp_live dispatch=$exp_disp cancels=$exp_cancel"
  fi
  sed 's/^/       | /' "$d/out"
  rm -rf "$d"
}

case_json() { python3 -c "
import json,sys,datetime
now=datetime.datetime.now(datetime.timezone.utc)
s=sys.argv[1]
for k,v in {'__NOW__':now.strftime('%Y-%m-%dT%H:%M:%SZ'),
            '__NEW__':(now-datetime.timedelta(seconds=60)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            '__MID__':(now-datetime.timedelta(minutes=40)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            '__OLD__':(now-datetime.timedelta(days=4)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            '__ANCIENT__':(now-datetime.timedelta(days=400)).strftime('%Y-%m-%dT%H:%M:%SZ')}.items():
    s=s.replace(k,v)
json.dump(json.loads(s),open('/tmp/case.json','w'))
print('/tmp/case.json')" "$1"; }

ZOMBIE='[{"databaseId":36801233593,"status":"pending","createdAt":"__OLD__","startedAt":"__OLD__"}]'
BOTH='[{"databaseId":36801233593,"status":"pending","createdAt":"__OLD__","startedAt":"__OLD__"},
      {"databaseId":37281693900,"status":"in_progress","createdAt":"__NOW__","startedAt":"__NOW__"}]'
FRESHQ='[{"databaseId":1001,"status":"queued","createdAt":"__NEW__","startedAt":"__NEW__"}]'
MIDQ='[{"databaseId":1003,"status":"queued","createdAt":"__MID__","startedAt":"__MID__"}]'
DONERUN='[{"databaseId":1002,"status":"completed","createdAt":"__NOW__","startedAt":"__NOW__"}]'
ANCIENT='[{"databaseId":777,"status":"pending","createdAt":"__ANCIENT__","startedAt":"__ANCIENT__"}]'

echo "Scenario 1: the Oct 1 production state — one 4-day-old pending zombie, no live run."
run_case "zombie reaped and replaced" "$(case_json "$ZOMBIE")" 0 0 1 1

echo
echo "Scenario 2: zombie + the run we are inside (in_progress). Keeper path, exclude=0."
run_case "live run suppresses dispatch" "$(case_json "$BOTH")" 0 1 0 0

echo
echo "Scenario 3: the real watch.yml call — exclude our own id, only a 4-day zombie left."
run_case "self-exclude reaps the stale run" "$(case_json "$BOTH")" 37281693900 0 1 1

echo
echo "Scenario 4: freshly queued successor (normal handover) counts as live."
run_case "fresh queued run is live" "$(case_json "$FRESHQ")" 0 1 0 0

echo
echo "Scenario 5: a queued run aged 40min is beyond STALE_SECS -> zombie."
run_case "40min queued run reaped" "$(case_json "$MIDQ")" 0 0 1 1

echo
echo "Scenario 6: nothing at all -> dispatch fresh."
run_case "empty dispatch" "$(case_json '[]')" 0 0 0 1

echo
echo "Scenario 7: only completed runs -> dispatch."
run_case "completed-only dispatch" "$(case_json "$DONERUN")" 0 0 0 1

echo
echo "Scenario 8: an un-cancellable pending run 400 days old is out of the reap window."
run_case "ancient zombie ignored, dispatch anyway" "$(case_json "$ANCIENT")" 0 0 0 1

echo
echo "Scenario 9: un-cancellable zombie (GitHub 409) must still dispatch — the real fix."
CANCEL_FAILS=1 run_case "cancel 409 still dispatches" "$(case_json "$ZOMBIE")" 0 0 0 1

echo
echo "Scenario 10: un-cancellable zombie but a live run exists -> nothing to do."
CANCEL_FAILS=1 run_case "cancel 409 with live run is quiet" "$(case_json "$BOTH")" 0 1 0 0

echo
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]