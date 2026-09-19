#!/bin/bash
# W0b — characterise the scx_cosmos attach failure in isolation.
#
# W0 showed scx_cosmos attaching and then being torn down with
#   runtime error (ops.init_task() failed (-12) for ksoftirqd/0[15]) on CPU 4
# after 5 attempts, leaving /sys/kernel/sched_ext/state = disabled. That disabled state then made
# the LATER arm probes (pandemonium, cake) report false failures, because they were running against
# a dead sched_ext rather than against their own scheduler.
#
# This script answers two questions:
#   1. Is the cosmos failure reproducible, or a one-off?
#   2. Is it the default configuration specifically? cosmos enables cpufreq control by default and
#      its -c 0 disables the locality engine, so both are plausible culprits.
#
# It recovers to cake between every attempt and refuses to leave the machine detached.
# /sys/kernel/sched_ext/state is read-only, so recovery must go through scx_loader.

set -u

OPS=/sys/kernel/sched_ext/root/ops
STATE=/sys/kernel/sched_ext/state
OUT="${BENCH_OUT:-$PWD}/scx-w0b-cosmos.txt"

ops_name()  { head -1 "$OPS" 2>/dev/null | tr -d '\n'; }
scx_state() { cat "$STATE" 2>/dev/null; }
is_cake()   { case "$(ops_name)" in cake*) return 0 ;; *) return 1 ;; esac; }
healthy()   { [ "$(scx_state)" = "enabled" ] && is_cake; }

recover_cake() {
    local i
    for i in $(seq 1 30); do
        healthy && return 0
        if [ -n "$(ops_name)" ]; then
            scxctl switch --sched cake >/dev/null 2>&1
        else
            scxctl start --sched cake >/dev/null 2>&1
            scxctl restore >/dev/null 2>&1
        fi
        sleep 1
    done
    # Last resort: the loader re-reads its config and restores the default scheduler.
    sudo systemctl restart scx_loader >/dev/null 2>&1
    sleep 4
    healthy
}

trap 'echo; echo "trap recovery:"; recover_cake; echo "  ops=$(ops_name) state=$(scx_state)"' EXIT

mkdir -p "$(dirname "$OUT")"
exec > >(tee "$OUT") 2>&1

echo "=== W0b cosmos characterisation — $(date -Is) ==="
echo "start: ops='$(ops_name)' state=$(scx_state)"

attempt() {
    local desc="$1" args="$2" t0 t1 outcome
    echo
    echo "-------------------------------------------------- $desc"
    echo "  args: ${args:-<default flags>}"
    recover_cake || { echo "  could not recover to cake — aborting"; return 1; }
    echo "  pre : ops=$(ops_name) state=$(scx_state)"

    t0=$(( $(date +%s%N) / 1000000 ))
    if [ -n "$(ops_name)" ]; then
        if [ -n "$args" ]; then scxctl switch --sched cosmos --args "$args" >/dev/null 2>&1
        else scxctl switch --sched cosmos >/dev/null 2>&1; fi
    else
        if [ -n "$args" ]; then scxctl start --sched cosmos --args "$args" >/dev/null 2>&1
        else scxctl start --sched cosmos >/dev/null 2>&1; fi
    fi

    # Wait for attach, then watch for a long enough window that a teardown cannot hide inside it.
    local ok=0 i
    for i in $(seq 1 150); do
        case "$(ops_name)" in cosmos_*) ok=1; break ;; esac
        [ "$(scx_state)" = "disabled" ] && break
        sleep 0.1
    done
    t1=$(( $(date +%s%N) / 1000000 ))
    echo "  attach: $(( t1 - t0 )) ms   ops=$(ops_name) state=$(scx_state)"

    sleep 5
    if [ -n "$(ops_name)" ] && [ "$(scx_state)" = "enabled" ]; then
        outcome="SURVIVED"
    else
        outcome="DIED"
    fi
    echo "  after 5 s: ops='$(ops_name)' state=$(scx_state)  -> $outcome"

    echo "  journal:"
    journalctl -u scx_loader --since "40 seconds ago" --no-pager -o cat 2>/dev/null |
        grep -iE 'error|fail|exit|runtime|init_task|Unregister|setting up|options:|performance|EXIT' |
        tail -12 | sed 's/^/    /'
    return 0
}

attempt "A: shipped default flags"          ""
attempt "B: default + --disable-cpufreq"    "--disable-cpufreq"
attempt "C: locality engine engaged (-c 50)" "-c 50"

echo
echo "=== W0b summary ==="
grep -E '^\s+after 5 s|---|^\s+args:' "$OUT" | sed 's/^/  /'
echo
echo "final: ops=$(ops_name) state=$(scx_state)"
