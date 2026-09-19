#!/bin/bash
# mtu-sweep.sh -- is a larger wlan0 MTU actually worth anything here?
#
# The mt7925 advertises maxmtu 2304, which is the 802.11 standard maximum MSDU.
# A larger MTU means IP payloads that used to fragment now fit in one frame, and
# TCP MSS grows with it, so bulk transfers need fewer packets per megabyte.
#
# Both effects are measurable locally without any external endpoint:
#   -s 2200  exceeds 1500, so at MTU 1500 it is fragmented and at 2304 it is not
#   -s 1400  fits at both MTUs and acts as the control: it must NOT move
#
# Interleaved so drift lands on both arms.  Energy via RAPL (root-only).
#
# Usage: [REPS=4] mtu-sweep.sh
set -euo pipefail

IFACE=${IFACE:-wlan0}
REPS=${REPS:-4}
N=${N:-200}
I=${I:-0.02}
GW=$(ip route show default | awk '{print $3; exit}')
OUT=${OUT:-${BENCH_OUT:-$PWD}/mtu-sweep.tsv}

orig_mtu=$(cat "/sys/class/net/$IFACE/mtu")
max_mtu=${MAX_MTU:-$(ip -d link show "$IFACE" | sed -n 's/.*maxmtu \([0-9]*\).*/\1/p')}
: "${max_mtu:=2304}"
restore() { sudo -n ip link set "$IFACE" mtu "$orig_mtu" >/dev/null 2>&1 || true; }
trap restore EXIT

rapl() { sudo -n cat /sys/class/powercap/intel-rapl:0/energy_uj; }

printf 'mtu\tsize\trep\trtt_min\trtt_avg\trtt_max\tmdev\tloss_pct\tJ\n' > "$OUT"
echo "mtu-sweep: iface=$IFACE gw=$GW orig_mtu=$orig_mtu max_mtu=$max_mtu reps=$REPS"

one() { # one MTU SIZE REP
  local mtu=$1 size=$2 rep=$3 e0 e1 line
  e0=$(rapl)
  line=$(ping -c "$N" -i "$I" -W 2 -q -s "$size" -M do "$GW" 2>&1) || true
  e1=$(rapl)
  local min avg max mdev loss
  min=$(printf '%s' "$line" | sed -n 's/.*= \([0-9.]*\)\/\([0-9.]*\)\/\([0-9.]*\)\/\([0-9.]*\).*/\1/p')
  avg=$(printf '%s' "$line" | sed -n 's/.*= \([0-9.]*\)\/\([0-9.]*\)\/\([0-9.]*\)\/\([0-9.]*\).*/\2/p')
  max=$(printf '%s' "$line" | sed -n 's/.*= \([0-9.]*\)\/\([0-9.]*\)\/\([0-9.]*\)\/\([0-9.]*\).*/\3/p')
  mdev=$(printf '%s' "$line" | sed -n 's/.*= \([0-9.]*\)\/\([0-9.]*\)\/\([0-9.]*\)\/\([0-9.]*\).*/\4/p')
  loss=$(printf '%s' "$line" | sed -n 's/.*, \([0-9.]*\)% packet loss.*/\1/p')
  if [ -z "$min" ]; then
    printf '%s\t%s\t%s\tNA\tNA\tNA\tNA\t%s\tNA\n' "$mtu" "$size" "$rep" "${loss:-100}" | tee -a "$OUT"
    return
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$mtu" "$size" "$rep" \
    "$min" "$avg" "$max" "$mdev" "${loss:-?}" \
    "$(awk -v a="$e1" -v b="$e0" 'BEGIN{printf "%.3f",(a-b)/1e6}')" | tee -a "$OUT"
}

for ((rep = 1; rep <= REPS; rep++)); do
  for mtu in 1500 "$max_mtu"; do
    sudo -n ip link set "$IFACE" mtu "$mtu"
    live=$(cat "/sys/class/net/$IFACE/mtu")
    [ "$live" = "$mtu" ] || { echo "!! mtu $mtu did not stick (live $live)" >&2; exit 1; }
    sleep 1
    one "$mtu" 2200 "$rep"   # fragments at 1500, single frame at 2304
    one "$mtu" 1400 "$rep"   # control: fits either way, must not move
  done
done

echo "mtu-sweep: done (mtu $orig_mtu restored on exit)"
