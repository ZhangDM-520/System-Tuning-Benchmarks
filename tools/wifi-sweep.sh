#!/bin/bash
# wifi-sweep.sh -- interleaved measurement of WiFi power save on vs off.
#
# Power save is a wake-up/sleep duty cycle: the radio drops into a low-power
# state between beacons and the first frame after an idle period pays the wake
# cost.  That means a continuous ping *hides* the effect entirely -- the radio
# never gets to sleep.  So each arm is measured two ways:
#
#   burst  back-to-back packets; tests latency and loss under load
#   idle   a packet every IDLE seconds; each one follows a sleep opportunity,
#          which is where power save actually shows up
#
# Energy comes from RAPL because the machine is on AC with a charge cap, so
# battery counters read zero.
#
# Usage: [REPS=3] [COUNT=...] wifi-sweep.sh
set -euo pipefail

IFACE=${IFACE:-wlan0}
REPS=${REPS:-3}
BURST_N=${BURST_N:-200}
BURST_I=${BURST_I:-0.02}
IDLE_N=${IDLE_N:-40}
IDLE_I=${IDLE_I:-0.3}
OUT=${OUT:-${BENCH_OUT:-$PWD}/wifi-sweep.tsv}

GW=$(ip route show default | awk '{print $3; exit}')
[ -n "$GW" ] || { echo "no default gateway" >&2; exit 1; }

rapl() { sudo -n cat /sys/class/powercap/intel-rapl:0/energy_uj; }
psstate() { iw dev "$IFACE" get power_save 2>/dev/null | awk '{print $NF}'; }

ORIG=$(psstate)
restore() { sudo -n iw dev "$IFACE" set power_save "$ORIG" >/dev/null 2>&1 || true; }
trap restore EXIT

printf 'arm\tmode\trep\trtt_min\trtt_avg\trtt_max\tmdev\tloss_pct\tJ\twall_s\tW\n' > "$OUT"
echo "wifi-sweep: gw=$GW iface=$IFACE original=$ORIG reps=$REPS -> $OUT"

one() { # one ARM MODE REP N I
  local arm=$1 mode=$2 rep=$3 n=$4 i=$5
  local e0 t0 t1 e1 line
  e0=$(rapl); t0=$(date +%s.%N)
  line=$(ping -c "$n" -i "$i" -W 2 -q "$GW" 2>&1) || true
  t1=$(date +%s.%N); e1=$(rapl)

  local min avg max mdev loss
  min=$(printf '%s' "$line" | sed -n 's/.*= \([0-9.]*\)\/\([0-9.]*\)\/\([0-9.]*\)\/\([0-9.]*\).*/\1/p')
  avg=$(printf '%s' "$line" | sed -n 's/.*= \([0-9.]*\)\/\([0-9.]*\)\/\([0-9.]*\)\/\([0-9.]*\).*/\2/p')
  max=$(printf '%s' "$line" | sed -n 's/.*= \([0-9.]*\)\/\([0-9.]*\)\/\([0-9.]*\)\/\([0-9.]*\).*/\3/p')
  mdev=$(printf '%s' "$line" | sed -n 's/.*= \([0-9.]*\)\/\([0-9.]*\)\/\([0-9.]*\)\/\([0-9.]*\).*/\4/p')
  loss=$(printf '%s' "$line" | sed -n 's/.*, \([0-9.]*\)% packet loss.*/\1/p')
  min=${min:-NA}; avg=${avg:-NA}; max=${max:-NA}; mdev=${mdev:-NA}; loss=${loss:-100}

  local j w
  j=$(awk -v a="$e1" -v b="$e0" 'BEGIN{printf "%.3f",(a-b)/1e6}')
  w=$(awk -v a="$e1" -v b="$e0" -v t0="$t0" -v t1="$t1" \
      'BEGIN{d=t1-t0; printf "%.2f", (d>0 ? (a-b)/1e6/d : 0)}')
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%.2f\t%s\n' \
    "$arm" "$mode" "$rep" "$min" "$avg" "$max" "$mdev" "$loss" "$j" \
    "$(awk -v a="$t0" -v b="$t1" 'BEGIN{print b-a}')" "$w" | tee -a "$OUT"
}

for ((rep = 1; rep <= REPS; rep++)); do
  for arm in on off; do
    sudo -n iw dev "$IFACE" set power_save "$arm"
    live=$(psstate)
    [ "$live" = "$arm" ] || { echo "!! power_save=$arm did not stick (live '$live')" >&2; exit 1; }
    # Let the firmware finish renegotiating its power mode before measuring;
    # otherwise the first arm of each round catches the transition itself.
    sleep 2
    one "$arm" burst "$rep" "$BURST_N" "$BURST_I"
    one "$arm" idle  "$rep" "$IDLE_N" "$IDLE_I"
  done
done

echo "wifi-sweep: done (original $ORIG restored on exit)"
