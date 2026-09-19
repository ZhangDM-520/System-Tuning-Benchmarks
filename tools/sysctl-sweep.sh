#!/bin/bash
# sysctl-sweep.sh -- run a workload under each value of one sysctl, then restore.
#
# Usage: [REPS=2] [PRE="cmd"] sysctl-sweep.sh KEY VALUE [VALUE...] -- WORKLOAD...
set -euo pipefail
cd "$(dirname "$0")"

KEY=$1; shift
VALUES=()
while [ $# -gt 0 ] && [ "$1" != "--" ]; do VALUES+=("$1"); shift; done
[ "${1:-}" = "--" ] && shift
WORK=("$@")
REPS=${REPS:-2}
PRE=${PRE:-}

orig=$(sysctl -n "$KEY")
trap 'sudo -n sysctl -q "$KEY=$orig" || true' EXIT
name=${KEY##*.}

for v in "${VALUES[@]}"; do
  sudo -n sysctl -q "$KEY=$v"
  live=$(sysctl -n "$KEY")
  if [ "$live" != "$v" ]; then
    echo "!! $KEY=$v did not stick (live $live)" >&2
    exit 1
  fi
  echo "### $KEY=$live"
  # shellcheck disable=SC2086
  ./fslat.py run --label "${name}-${v}" --note "$KEY=$v" --repeat "$REPS" \
    ${PRE:+--pre "$PRE"} -- "${WORK[@]}" 2>&1 |
    grep -E '===|PSI|nvme|xfs|PROBE|reclaim'
done
