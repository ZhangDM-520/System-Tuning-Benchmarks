#!/bin/bash
# W0d — (a) prove the --args plumbing with the correct quoting, and
#       (b) test whether scx_cosmos is viable in ANY configuration here.
#
# Two things learned the hard way, both encoded here:
#
# 1. scxctl parses --args with clap, so a value BEGINNING WITH '-' is mistaken for a flag:
#      scxctl switch --sched bpfland --args "-m all"   -> "a value is required for '--args'"
#      scxctl switch --sched cosmos  --args "--disable-cpufreq" -> "unexpected argument found"
#    The working form is the equals form: --args="-m all". Pass B depends entirely on this.
#
# 2. scx_cosmos dies reproducibly at its SHIPPED DEFAULT: it attaches, then
#      runtime error (ops.init_task() failed (-12) for ksoftirqd/0[15])
#    five times over, leaving /sys/kernel/sched_ext/state = disabled. That disabled state then
#    silently poisons every later arm, which is how two earlier "SURVIVED" verdicts were wrong.
#
# So: is cosmos broken specifically at its default, or broken on this machine entirely? That
# distinction decides whether cosmos is comparable at all.

set -u

OPS=/sys/kernel/sched_ext/root/ops
STATE=/sys/kernel/sched_ext/state
OUT="${BENCH_OUT:-$PWD}/scx-w0d-cosmos.txt"

ops_name()  { head -1 "$OPS" 2>/dev/null | tr -d '\n'; }
scx_state() { cat "$STATE" 2>/dev/null; }
is_cake()   { case "$(ops_name)" in cake*) return 0 ;; *) return 1 ;; esac; }
healthy()   { [ "$(scx_state)" = "enabled" ] && is_cake; }

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

# launch(): switches to an arm using whichever scxctl verb is valid from the current state.
# Extra arguments always go through the --args= equals form.
launch() {
    local sched="$1" args="$2" mode="$3"
    if [ -n "$(ops_name)" ]; then
        if [ -n "$args" ]; then scxctl switch --sched "$sched" "--args=$args" 2>&1 | sed 's/^/    scxctl: /'
        elif [ -n "$mode" ]; then scxctl switch --sched "$sched" --mode "$mode" 2>&1 | sed 's/^/    scxctl: /'
        else scxctl switch --sched "$sched" 2>&1 | sed 's/^/    scxctl: /'; fi
    else
        if [ -n "$args" ]; then scxctl start --sched "$sched" "--args=$args" 2>&1 | sed 's/^/    scxctl: /'
        elif [ -n "$mode" ]; then scxctl start --sched "$sched" --mode "$mode" 2>&1 | sed 's/^/    scxctl: /'
        else scxctl start --sched "$sched" 2>&1 | sed 's/^/    scxctl: /'; fi
    fi
}

attempt() {
    local label="$1" sched="$2" args="$3" mode="$4" t0 t1 verdict got i
    echo
    echo "================================================== $label"
    echo "  target: $sched ${args:+--args=$args} ${mode:+--mode=$mode}"
    recover_cake || { echo "  ABORT: machine not healthy"; return 1; }
    echo "  pre   : ops=$(ops_name) state=$(scx_state)"

    t0=$(( $(date +%s%N) / 1000000 ))
    launch "$sched" "$args" "$mode"

    got=0
    for i in $(seq 1 120); do
        case "$(ops_name)" in "$sched"_*) got=1; break ;; esac
        # If sched_ext has gone disabled, the attempt has already failed — stop waiting.
        [ "$(scx_state)" = "disabled" ] && [ $i -gt 25 ] && break
        sleep 0.1
    done
    t1=$(( $(date +%s%N) / 1000000 ))
    echo "  attach: $(( t1 - t0 )) ms   ops='$(ops_name)' state=$(scx_state)"
    sleep 5

    if case "$(ops_name)" in "$sched"_*) true ;; *) false ;; esac && [ "$(scx_state)" = "enabled" ]; then
        verdict="SURVIVED"
    elif [ "$got" = 1 ]; then
        verdict="DIED (attached then torn down)"
    else
        verdict="NO SWITCH (never attached)"
    fi
    echo "  after 5 s: ops='$(ops_name)' state=$(scx_state)"
    echo "  VERDICT  : $verdict"
    echo "  banner:"
    journalctl -u scx_loader --since "50 seconds ago" --no-pager -o cat 2>/dev/null |
        grep -iE 'options:|is active|Error|Failed|runtime error|primary CPU|performance' |
        tail -8 | sed 's/^/    /'
    return 0
}

mkdir -p "$(dirname "$OUT")"
exec > >(tee "$OUT") 2>&1

echo "=== W0d --args plumbing + cosmos viability — $(date -Is) ==="
echo "start: ops='$(ops_name)' state=$(scx_state)"

# A: proves the equals form actually reaches the scheduler. bpfland echoes its options in its
# banner ("scheduler options: scx_bpfland -m all"), so the banner is the proof, not the exit code.
attempt "A: --args equals form (control) — bpfland -m all" bpfland "-m all" ""
# B: cosmos at its shipped default, for a fourth reproduction.
attempt "B: cosmos shipped default" cosmos "" ""
# C: cosmos with its locality engine engaged instead of -c 0.
attempt "C: cosmos -c 50 (locality engine on)" cosmos "-c 50" ""
# D: cosmos via the loader's own configured Gaming mode (-s 700).
attempt "D: cosmos loader Gaming mode (-s 700)" cosmos "" "Gaming"

echo
echo "=== W0d summary ==="
grep -E 'VERDICT|target:' "$OUT" | sed 's/^/  /'
echo
echo "final: ops=$(ops_name) state=$(scx_state)"
