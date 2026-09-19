#!/bin/bash
# Direct probe: does pandemonium's adaptive tuner actually change anything?
#
# The phase sweep answered this statistically (adaptive == --no-adaptive across six classes, n=10),
# but that is inference from outcomes. This probe asks the mechanism directly: run pandemonium at
# verbose, drive it through a deliberately VARIED load so its tuner has every reason to move, and
# capture everything it says. Pandemonium at default verbosity logs only a startup banner and a
# shutdown line -- 104 lines across the entire deep pass, none of them a tuning update -- so if the
# verbose path also reports nothing, the tuner is not merely ineffective, it is unobservable.
#
# Also records the banner values (phi range, lambda2, tau, codel_eq) so a before/after change in the
# tuner's own reported state could be seen if it prints one.

set -u

WS="${BENCH_OUT:-$PWD}"
OUT="$WS/scx-pandemonium-verbose.txt"
OPS=/sys/kernel/sched_ext/root/ops
STATE=/sys/kernel/sched_ext/state
MARK=$(date +%s)

ops_name()  { head -1 "$OPS" 2>/dev/null | tr -d '\n'; }
set_arm() {
    local tgt="$1" extra="${2:-}"
    if [ -n "$(ops_name)" ]; then
        if [ -n "$extra" ]; then scxctl switch --sched "$tgt" "--args=$extra" >/dev/null 2>&1
        else scxctl switch --sched "$tgt" >/dev/null 2>&1; fi
    else
        if [ -n "$extra" ]; then scxctl start --sched "$tgt" "--args=$extra" >/dev/null 2>&1
        else scxctl start --sched "$tgt" >/dev/null 2>&1; fi
    fi
}

trap 'set_arm cake; sleep 2; echo "restored: $(ops_name)"' EXIT

exec > >(tee "$OUT") 2>&1

echo "=== pandemonium verbose adaptation probe — $(date -Is) ==="

for MODE in "-v" "-v --no-adaptive"; do
    echo
    echo "==================== args: $MODE"
    set_arm pandemonium "$MODE"
    sleep 4
    echo "  attached: ops=$(ops_name) state=$(cat $STATE)"

    # Drive a deliberately varied load. Each class wants a different placement policy, so a tuner
    # that adapts has every opportunity to change its parameters here.
    echo "  phase 1/4: saturate (24 threads, 20 s)"
    stress-ng --cpu 24 -t 20s --quiet >/dev/null 2>&1
    echo "  phase 2/4: interactive burst (schbench, 5 s)"
    schbench -m 2 -t 4 -r 5 >/dev/null 2>&1
    echo "  phase 3/4: wakeup storm (hackbench, ~8 s)"
    hackbench -s 1024 -l 3000 -g 10 -T >/dev/null 2>&1
    echo "  phase 4/4: idle 10 s"
    sleep 10

    echo "  --- everything pandemonium said during that varied load ---"
    journalctl -u scx_loader --since "@$MARK" --no-pager -o cat 2>/dev/null |
        grep -viE 'scxctl|switching|Got event|starting scx_|EXIT|Unregister|detached|shutting down' |
        sed 's/^/      /' | tail -40
    echo "  --- end of its output ---"
    set_arm cake
    sleep 3
done

echo
echo "=== probe complete ==="
