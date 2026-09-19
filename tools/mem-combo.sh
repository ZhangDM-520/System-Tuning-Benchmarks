#!/bin/bash
# mem-combo.sh -- validate a *combination* of memory knobs as one arm.
#
# ab-sweep.sh varies one knob at a time, but min_free_kbytes and
# watermark_scale_factor both set the same watermarks: min_free sets the floor
# and scale_factor widens the band above it.  Adopting both on the strength of
# two independent single-knob sweeps would assume they do not interact, which
# is exactly the assumption worth testing rather than assuming.
#
# Each arm is "MINFREE:WATERMARK"; arms are interleaved inside every round.
#
# Usage: [REPS=6] mem-combo.sh "524288:150" "262144:10" -- WORKLOAD...
set -euo pipefail
cd "$(dirname "$0")"

REPS=${REPS:-6}
ARMS=()
while [ $# -gt 0 ] && [ "$1" != "--" ]; do ARMS+=("$1"); shift; done
[ "${1:-}" = "--" ] && shift
WORK=("$@")
[ ${#ARMS[@]} -gt 0 ] && [ ${#WORK[@]} -gt 0 ] || { echo "need arms and -- work" >&2; exit 2; }

get_pair() { printf '%s:%s' "$(cat /proc/sys/vm/min_free_kbytes)" \
                              "$(cat /proc/sys/vm/watermark_scale_factor)"; }
set_pair() {
  printf '%s' "${1%%:*}"  | sudo -n tee /proc/sys/vm/min_free_kbytes        > /dev/null
  printf '%s' "${1##*:}" | sudo -n tee /proc/sys/vm/watermark_scale_factor > /dev/null
}

ORIG=$(get_pair)
restore() { set_pair "$ORIG" || true; }
trap restore EXIT
echo "mem-combo: original=$ORIG reps=$REPS arms=${ARMS[*]}"

for ((rep = 1; rep <= REPS; rep++)); do
  for arm in "${ARMS[@]}"; do
    set_pair "$arm"
    live=$(get_pair)
    [ "$live" = "$arm" ] || { echo "!! $arm did not stick (live $live)" >&2; exit 1; }
    ./fslat.py run --label "memcombo-${arm}" --note "minfree:scale=$arm" \
      --repeat 1 --drop -- "${WORK[@]}" 2>&1 |
      grep -E '===|PSI|reclaim' | sed "s/^/  r${rep} /"
  done
done
echo "mem-combo: done (restored $ORIG)"
