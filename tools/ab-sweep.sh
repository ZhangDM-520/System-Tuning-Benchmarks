#!/bin/bash
# ab-sweep.sh -- interleaved A/B sweep over the values of one knob.
#
# Arms are cycled inside every repetition round, so slow drift in the machine
# (thermals, page cache, background work) lands on every arm equally.  A sweep
# that ran all controls first and all treatments afterwards would manufacture
# differences out of drift, and the acceptance rule compares against the
# control's own spread -- so that mistake would be invisible.
#
# Usage:
#   ab-sweep.sh --label NAME --reps N \
#               --get 'cat /sys/...' \
#               --set 'echo %V% | sudo -n tee /sys/... > /dev/null' \
#               [--drop] [--pre 'CMD'] [--sleep SECS] \
#               --values V1 V2 V3 -- WORKLOAD...
#
# --get is used both to read the original value (restored on exit) and to
# verify that each value actually stuck.  A value that silently fails to apply
# is a hard error: an unattributable result row is worse than no row.
set -euo pipefail
cd "$(dirname "$0")"

LABEL=''; REPS=3; GET=''; SET=''; DROP=''; PRE=''; SLEEP=''
VALUES=(); WORK=()

while [ $# -gt 0 ]; do
  case "$1" in
    --label)  LABEL=$2; shift 2 ;;
    --reps)   REPS=$2; shift 2 ;;
    --get)    GET=$2; shift 2 ;;
    --set)    SET=$2; shift 2 ;;
    --drop)   DROP=--drop; shift ;;
    --pre)    PRE=$2; shift 2 ;;
    --sleep)  SLEEP=$2; shift 2 ;;
    --values) shift; while [ $# -gt 0 ] && [ "$1" != "--" ]; do VALUES+=("$1"); shift; done ;;
    --)       shift; WORK=("$@"); break ;;
    *) echo "ab-sweep: bad argument '$1'" >&2; exit 2 ;;
  esac
done

[ -n "$LABEL" ] && [ -n "$GET" ] && [ -n "$SET" ] \
  && [ ${#VALUES[@]} -gt 0 ] && [ ${#WORK[@]} -gt 0 ] \
  || { echo "ab-sweep: need --label --get --set --values -- WORK" >&2; exit 2; }

ORIG=$(eval "$GET")
restore() {
  local cur; cur=$(eval "$GET")
  [ "$cur" = "$ORIG" ] || eval "${SET//%V%/$ORIG}" || true
}
trap restore EXIT

echo "ab-sweep: $LABEL  original=$(echo "$ORIG" | tr -d '\n')  reps=$REPS  arms=${VALUES[*]}"

for ((rep = 1; rep <= REPS; rep++)); do
  for v in "${VALUES[@]}"; do
    eval "${SET//%V%/$v}"
    live=$(eval "$GET")
    if [ "$live" != "$v" ]; then
      echo "!! ${LABEL}=${v} did not stick (live '${live}')" >&2
      exit 1
    fi
    args=(run --label "${LABEL}-${v}" --note "$(basename "$LABEL")=$v" --repeat 1)
    [ -n "$DROP" ] && args+=("$DROP")
    [ -n "$PRE" ] && args+=(--pre "$PRE")
    [ -n "$SLEEP" ] && args+=(--sleep "$SLEEP")
    ./fslat.py "${args[@]}" -- "${WORK[@]}" 2>&1 |
      grep -E '===|PSI|nvme|xfs|probe|reclaim' | sed "s/^/  r${rep} /"
  done
done

echo "ab-sweep: $LABEL done"
