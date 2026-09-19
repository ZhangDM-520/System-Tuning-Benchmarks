#!/bin/bash
# W0c — cosmos retest with a CORRECT verdict, plus proof that --args actually applies.
#
# Why this retest exists: the first characterisation script reported "SURVIVED" for the
# --disable-cpufreq and -c 50 candidates, but its verdict only checked "some scheduler is
# attached and enabled". In both cases /sys/kernel/sched_ext/root/ops still read cake_1.2.1 —
# the switch had timed out at 15.8 s and never happened. So those two results were false
# positives and prove nothing about cosmos.
#
# The verdict here requires ops to ACTUALLY be cosmos. Anything else is reported as "no switch".
# Test 1 exists because Pass B depends on `scxctl switch --args` working at all, and that
# assumption was never verified.

set -u

OPS=/sys/kernel/sched_ext/root/ops
STATE=/sys/kernel/sched_ext/state
OUT="${BENCH_OUT:-$PWD}/scx-w0c-cosmos.txt"

ops_name()  { head -1 "$OPS" 2>/dev/null | tr -d '\n'; }
scx_state() { cat "$STATE" 2>/dev/null; }
is_cake()   { case "$(ops_name)" in cake*) return 0 ;; *) return 1 ;; esac; }
healthy()   { [ "$(scx_state)" = "enabled" ] && is_cake; }

# Wait until the loader has finished whatever it was doing and cake is healthy again. A failed
# scheduler start leaves the loader retrying with backoff, and a switch request issued during that
# window is silently dropped — which is what produced the false positives this script replaces.
recover_cake() {
    local i
    for i in $(seq 1 40); do
        healthy && { sleep 4; healthy && return 0; }
        if [ -n "$(ops_name)" ]; then scxctl switch --sched cake >/dev/null 2>&1
        else scxctl start --sched cake >/dev/null 2>&1; scxctl restore >/dev/null 2>&1; fi
        sleep 1
    done
    sudo systemctl restart scx_loader >/dev/null 2>&1
    sleep 5
    healthy
}

attempt() {
    local label="$1" sched="$2" args="$3" t0 t1 verdict
    echo
    echo "================================================== $label"
    echo "  target: $sched ${args:+[$args]}"
    recover_cake || { echo "  ABORT: machine not healthy"; return 1; }
    echo "  pre   : ops=$(ops_name) state=$(scx_state)"

    t0=$(( $(date +%s%N) / 1000000 ))
    if [ -n "$(ops_name)" ]; then
        if [ -n "$args" ]; then scxctl switch --sched "$sched" --args "$args" 2>&1 | sed 's/^/    scxctl: /'
        else scxctl switch --sched "$sched" 2>&1 | sed 's/^/    scxctl: /'; fi
    else
        if [ -n "$args" ]; then scxctl start --sched "$sched" --args "$args" 2>&1 | sed 's/^/    scxctl: /'
        else scxctl start --sched "$sched" 2>&1 | sed 's/^/    scxctl: /'; fi
    fi

    local got=0 i
    for i in $(seq 1 200); do
        case "$(ops_name)" in "$sched"_*) got=1; break ;; esac
        [ "$(scx_state)" = "disabled" ] && [ $i -gt 30 ] && break
        sleep 0.1
    done
    t1=$(( $(date +%s%N) / 1000000 ))
    echo "  attach: $(( t1 - t0 )) ms   ops='$(ops_name)' state=$(scx_state)"

    sleep 5
    # The verdict: the target must STILL be attached and enabled five seconds later.
    if case "$(ops_name)" in "$sched"_*) true ;; *) false ;; esac && [ "$(scx_state)" = "enabled" ]; then
        verdict="SURVIVED"
    elif [ "$got" = 1 ]; then
        verdict="DIED (attached then torn down)"
    else
        verdict="NO SWITCH (target never attached)"
    fi
    echo "  after 5 s: ops='$(ops_name)' state=$(scx_state)"
    echo "  VERDICT  : $verdict"

    echo "  banner:"
    journalctl -u scx_loader --since "45 seconds ago" --no-pager -o cat 2>/dev/null |
        grep -iE 'options:|Error|Failed|runtime error|init_task|Unregister|primary CPU|performance level|IS ACTIVE|settings|slice' |
        tail -10 | sed 's/^/    /'
    return 0
}

mkdir -p "$(dirname "$OUT")"
exec > >(tee "$OUT") 2>&1

echo "=== W0c cosmos retest — $(date -Is) ==="
echo "start: ops='$(ops_name)' state=$(scx_state)"

# Test 1 proves the --args plumbing works, using a scheduler known to attach. Without this,
# a cosmos "NO SWITCH" could be blamed on --args syntax rather than on cosmos itself.
attempt "TEST 1: --args plumbing (control, bpfland -m all)" bpfland "-m all"
attempt "TEST 2: cosmos, shipped default flags"           cosmos ""
attempt "TEST 3: cosmos, --disable-cpufreq"              cosmos "--disable-cpufreq"

echo
echo "=== W0c summary ==="
grep -E 'VERDICT|---|target:' "$OUT" | sed 's/^/  /'
echo
echo "final: ops=$(ops_name) state=$(scx_state)"
