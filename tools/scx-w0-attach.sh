#!/bin/bash
# W0 — attach feasibility gate for the scx scheduler campaign.
#
# Verifies that each arm in the comparison can actually be brought up with DEFAULT flags,
# records the scheduler's own startup banner (the only place a resolved primary-domain mask is
# ever printed), and restores the original scheduler no matter how this script exits.
#
# The comparison is meaningless if an arm silently fails to attach, so this runs BEFORE any
# measurement and refuses to substitute an arm that does not come up.
#
# Two scxctl behaviours discovered while building this and designed around:
#   * `scxctl start` REFUSES while a scheduler is already attached — changing arms requires
#     `switch`, and `start` is only valid from the detached state.
#   * scxctl exit codes are unreliable (it printed "error: ..." and still returned rc=0),
#     so every state assertion here reads /sys/kernel/sched_ext/root/ops instead of trusting rc.

set -u

WS="${BENCH_OUT:-$PWD}"
OPS=/sys/kernel/sched_ext/root/ops
STATE=/sys/kernel/sched_ext/state
REJ=/sys/kernel/sched_ext/nr_rejected
OUT="$WS/scx-w0-attach.txt"

ORIG_SCHED=""

ops_name()  { head -1 "$OPS" 2>/dev/null | tr -d '\n'; }
scx_state() { cat "$STATE" 2>/dev/null; }
rejected()  { cat "$REJ" 2>/dev/null; }
now_ms()    { echo $(( $(date +%s%N) / 1000000 )); }

# set_arm(): the single place that knows how to move between arms. "bore" means detach entirely
# so tasks fall back to the kernel's BORE-patched fair class.
set_arm() {
    local tgt="$1" extra="${2:-}"
    if [ "$tgt" = "bore" ]; then
        scxctl stop >/dev/null 2>&1
    elif [ -n "$(ops_name)" ]; then
        if [ -n "$extra" ]; then
            scxctl switch --sched "$tgt" --args "$extra" >/dev/null 2>&1
        else
            scxctl switch --sched "$tgt" >/dev/null 2>&1
        fi
    else
        if [ -n "$extra" ]; then
            scxctl start --sched "$tgt" --args "$extra" >/dev/null 2>&1
        else
            scxctl start --sched "$tgt" >/dev/null 2>&1
        fi
    fi
}

# wait_ops(): block until /sys/kernel/sched_ext/root/ops reflects what we asked for.
# Attach latency is measured to THIS moment, not to scxctl returning — the gap between the two
# is where a slow or partial attach would otherwise hide.
wait_ops() {
    local want="$1" i
    for i in $(seq 1 150); do
        case "$(ops_name)" in
            "$want"*) return 0 ;;
        esac
        if [ "$want" = "none" ] && [ -z "$(ops_name)" ]; then return 0; fi
        sleep 0.1
    done
    return 1
}

# banner(): the scheduler's stdout is captured by scx_loader into the journal. That banner is
# the authoritative record of what the arm actually resolved — e.g. bpfland prints the primary
# CPU domain mask it derived from the power profile, which is otherwise unobservable.
banner() {
    journalctl -u scx_loader --since "30 seconds ago" --no-pager -o cat 2>/dev/null |
        sed 's/^/      /'
}

restore() {
    local rc=$?
    echo
    echo "=== restoring original scheduler (${ORIG_SCHED:-cake}) ==="
    set_arm "${ORIG_SCHED:-cake}"
    wait_ops "${ORIG_SCHED:-cake}" || true
    sleep 1
    echo "    ops now : $(ops_name)"
    echo "    state   : $(scx_state)   rejected: $(rejected)"
    if [ "$(ops_name)" = "cake_1.2.1_x86_64_unknown_linux_gnu" ]; then
        echo "    RESTORE OK"
    else
        echo "    RESTORE UNEXPECTED — investigate"
    fi
    return $rc
}
trap restore EXIT

mkdir -p "$WS"
exec > >(tee "$OUT") 2>&1

echo "=== W0 attach feasibility — $(date -Is) ==="
echo "kernel      : $(uname -r)"
echo "ops at start: $(ops_name)"
echo "state       : $(scx_state)   rejected: $(rejected)"

start_ops="$(ops_name)"
[ -n "$start_ops" ] && ORIG_SCHED="${start_ops%%_*}"
echo "original    : ${ORIG_SCHED:-<none>}"

probe_scx() {
    local sched="$1" t0 t1 name later
    echo
    echo "------------------------------ arm: scx_$sched  (default flags)"
    t0="$(now_ms)"
    set_arm "$sched"
    if wait_ops "$sched"; then
        t1="$(now_ms)"
        name="$(ops_name)"
        echo "    attach  : $(( t1 - t0 )) ms"
        echo "    ops     : $name"
    else
        echo "    attach  : FAILED to attach within 15 s"
        echo "    ops     : '$(ops_name)'"
        name=""
    fi
    echo "    state   : $(scx_state)   rejected: $(rejected)"
    echo "    banner:"
    banner
    # Settle, then confirm it is STILL attached. A scheduler that attaches and is then
    # watchdog-rejected is not a viable arm — exactly the failure this gate exists to catch.
    sleep 3
    later="$(ops_name)"
    if [ -n "$later" ] && [ "$later" = "$name" ]; then
        echo "    still attached after 3 s: yes"
    else
        echo "    still attached after 3 s: NO — was '$name', now '$later'"
    fi
}

for s in bpfland cosmos pandemonium cake; do
    probe_scx "$s"
done

echo
echo "------------------------------ arm: BORE (kernel stock, scx detached)"
t0="$(now_ms)"
set_arm bore
wait_ops none || true
t1="$(now_ms)"
echo "    detach  : $(( t1 - t0 )) ms"
echo "    ops     : '$(ops_name)'   (empty = kernel fair class = BORE)"
echo "    state   : $(scx_state)   rejected: $(rejected)"
echo "    sched_bore = $(cat /proc/sys/kernel/sched_bore 2>/dev/null)"
# A real pinned busy-loop proves the fallback path is live, rather than merely "no scheduler".
echo "    running a pinned task on cpu0 to prove the kernel path functions:"
taskset -c 0 sh -c 'end=$(( $(date +%s%N) + 400000000 )); while [ $(date +%s%N) -lt $end ]; do :; done' &&
    echo "      pinned busy-loop completed OK"
echo "    ops during/after that run: '$(ops_name)'"

echo
echo "=== W0 complete — restore follows from the trap ==="
