#!/bin/bash
# dw-sweep.sh -- sweep dirty-writeback shaping, measured by responsiveness.
#
# The metric is the thing the user actually feels: how long a real browser
# launch takes while a large write is in flight, plus PSI io "full" (micro-
# seconds during which *every* task was stalled on I/O).
#
# Config argument format:  LABEL:BG_BYTES:DIRTY_BYTES:EXPIRE_CENTISECS
#   LABEL=base restores the shipped ratio-based behaviour.
set -euo pipefail
cd "$(dirname "$0")"

declare -A ORIG
for k in vm.dirty_background_bytes vm.dirty_background_ratio vm.dirty_bytes \
         vm.dirty_ratio vm.dirty_expire_centisecs vm.dirty_writeback_centisecs; do
  ORIG[$k]=$(sysctl -n "$k")
done

apply() { # apply LABEL BG DIRTY EXPIRE
  local label=$1 bg=$2 dirty=$3 expire=$4
  if [ "$label" = base ]; then
    # Writing 0 to a *_bytes knob is rejected with EINVAL.  The supported way
    # back to ratio mode is to write the *_ratio knob, which implicitly zeroes
    # the corresponding *_bytes knob.
    sudo -n sysctl -q \
      vm.dirty_background_ratio="${ORIG[vm.dirty_background_ratio]}" \
      vm.dirty_ratio="${ORIG[vm.dirty_ratio]}" \
      vm.dirty_expire_centisecs="$expire"
  else
    sudo -n sysctl -q vm.dirty_background_bytes="$bg" vm.dirty_bytes="$dirty" \
                        vm.dirty_expire_centisecs="$expire"
  fi
  printf '### %-14s bg=%s dirty=%s expire=%s (live: bg=%s/%s dirty=%s/%s expire=%s)\n' \
    "$label" "$bg" "$dirty" "$expire" \
    "$(sysctl -n vm.dirty_background_bytes)" "$(sysctl -n vm.dirty_background_ratio)" \
    "$(sysctl -n vm.dirty_bytes)" "$(sysctl -n vm.dirty_ratio)" \
    "$(sysctl -n vm.dirty_expire_centisecs)"
}

restore() {
  sudo -n sysctl -q \
    vm.dirty_background_ratio="${ORIG[vm.dirty_background_ratio]}" \
    vm.dirty_ratio="${ORIG[vm.dirty_ratio]}" \
    vm.dirty_expire_centisecs="${ORIG[vm.dirty_expire_centisecs]}" \
    vm.dirty_writeback_centisecs="${ORIG[vm.dirty_writeback_centisecs]}"
}
trap restore EXIT

for cfg in "$@"; do
  IFS=: read -r label bg dirty expire <<< "$cfg"
  apply "$label" "${bg:-0}" "${dirty:-0}" "${expire:-3000}"
  ./fslat.py run --label "dw-${label}" --note "$cfg" --repeat "${REPS:-2}" \
    --pre "rm -f $PWD/big.bin" -- ./wl.sh launchunderload "${LOAD_MIB:-8192}" 2 2>&1 |
    grep -E '===|PSI|PROBE'
done
