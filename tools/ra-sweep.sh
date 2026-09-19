#!/bin/bash
# ra-sweep.sh -- sweep block read-ahead and measure its effect on a workload.
#
# Rationale: the cold metadata walk runs at queue depth ~1 and ~95 us per
# round trip with the device only ~55% busy.  Fewer, larger round trips is
# therefore the lever -- and it should also *reduce* device wakeups.
#
# Usage: ra-sweep.sh [--workload W] [--reps N] KB...
set -euo pipefail
cd "$(dirname "$0")"

DEV=nvme0n1
KNOB=/sys/block/$DEV/queue/read_ahead_kb
WORKLOAD=${WORKLOAD:-}
REPS=${REPS:-2}
VALUES=()

while [ $# -gt 0 ]; do
  case "$1" in
    --workload) WORKLOAD=$2; shift 2 ;;
    --reps)     REPS=$2; shift 2 ;;
    *)          VALUES+=("$1"); shift ;;
  esac
done

: "${WORKLOAD:=$HOME/Workspace/Gentoo-Style-Arch}"
orig=$(cat "$KNOB")
restore() { echo "$orig" | sudo -n tee "$KNOB" > /dev/null; }
trap restore EXIT

echo "== sweep read_ahead_kb over: ${VALUES[*]}  (restoring $orig afterwards)"
for kb in "${VALUES[@]}"; do
  echo "$kb" | sudo -n tee "$KNOB" > /dev/null
  live=$(cat "$KNOB")
  [ "$live" = "$kb" ] || { echo "!! failed to set $kb (live $live)" >&2; exit 1; }
  echo "### read_ahead_kb=$live"
  ./fslat.py run --label "ra${kb}" --note "read_ahead_kb=$kb" --drop --repeat "$REPS" \
    -- ./wl.sh coldmeta "$WORKLOAD" 2>&1 | grep -E '===|PSI|nvme'
done
