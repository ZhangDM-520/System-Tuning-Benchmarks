#!/usr/bin/env python3
"""scx_freq_probe.py -- is the BORE-vs-pandemonium energy gap a frequency effect?

The deep pass found BORE holding a materially higher package power than pandemonium in every
UNDER-SATURATED phase (idle +12%, io +54%, reclaim +49%) while at saturation the two are identical
(~17 W, a hardware ceiling), and the CPU-busy traces did not explain it -- pandemonium was if
anything busier. That leaves frequency / power state as the live hypotheses, and scaling_cur_freq
was only ever sampled at slot end, which is useless.

This probe measures the thing directly: sample every CPU's scaling_cur_freq across an idle window
and a reclaim window for each arm, alongside RAPL, so the energy difference can be attributed to
clock rate or explicitly exonerated from it.
"""

import json
import os
import statistics
import subprocess
import sys
import time

# --- portability shim ---------------------------------------------------------
ROOT = os.environ.get("BENCH_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")

sys.path.insert(0, TOOLS)
import scx_ab as A  # noqa: E402
import scx_deep as D  # noqa: E402

ARMS = [("bore", ""), ("pandemonium", "")]
SAMPLES = os.environ.get("SCX_FREQ_PROBE_OUT") or os.path.join(DATA, "scx-freq-probe.jsonl")


def sample_rapl():
    return int(A.slurp("/sys/class/powercap/intel-rapl:0/energy_uj") or 0)


def freqs_now():
    out = []
    for c in range(24):
        v = A.slurp(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq")
        if v.isdigit():
            out.append(int(v) / 1000.0)
    return out


def window(label, runner, seconds):
    """Run one workload window while sampling frequency, returning energy and clock stats."""
    a_rapl = sample_rapl()
    fs = []
    t0 = time.time()
    proc = subprocess.Popen(runner, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while time.time() - t0 < seconds:
        fs.append(freqs_now())
        time.sleep(0.5)
    proc.terminate()
    proc.wait()
    b_rapl = sample_rapl()
    wall = time.time() - t0
    flat = [v for row in fs for v in row]
    j = (b_rapl - a_rapl) / 1e6
    return {
        "window": label, "arm_wall_s": round(wall, 2),
        "J": round(j, 2), "W": round(j / wall, 2) if wall else 0,
        "freq_mean_mhz": round(statistics.mean(flat), 1) if flat else 0,
        "freq_max_mhz": round(max(flat), 1) if flat else 0,
        "freq_min_mhz": round(min(flat), 1) if flat else 0,
        "freq_n": len(flat),
    }


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        print("Usage: sudo scx_freq_probe.py [reps]     # reps defaults to 2\n"
              "Appends to $SCX_FREQ_PROBE_OUT, else data/scx-freq-probe.jsonl.\n"
              "This is a live measurement: ~40 s per repetition per arm, and it switches\n"
              "the running scheduler.  Needs root (RAPL is root-only, scxctl switches arms).")
        return 0
    if os.geteuid() != 0:
        print("scx_freq_probe.py needs root: RAPL is root-only and scxctl switches the live\n"
              "scheduler.  Re-run under sudo, or pass --help to read the usage.", file=sys.stderr)
        return 2
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    rows = []
    for rep in range(reps):
        for arm, args in ARMS:
            if not A.set_arm(arm, args or None)["ok"]:
                print(f"  {arm}: attach failed, skipping", flush=True)
                continue
            time.sleep(2)
            idle = window("idle", ["sleep", "9"], 8)
            mem = window("mem", ["stress-ng", "--vm", "1", "--vm-bytes", "16G", "--vm-keep",
                                 "--vm-hang", "0", "-t", "10s", "--quiet"], 11)
            for w in (idle, mem):
                w["arm"] = arm
                w["rep"] = rep
                rows.append(w)
                print(f"  rep{rep} {arm:<12} {w['window']:<5} {w['J']:7.2f}J {w['W']:6.2f}W "
                      f"freq mean={w['freq_mean_mhz']:7.1f} max={w['freq_max_mhz']:7.1f} "
                      f"min={w['freq_min_mhz']:7.1f} MHz", flush=True)
    A.set_arm("cake", None)
    with open(SAMPLES, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"\n--- medians")
    for w in ("idle", "mem"):
        print(f"  {w}:")
        for arm, _ in ARMS:
            v = [r for r in rows if r["arm"] == arm and r["window"] == w]
            if not v:
                continue
            print(f"    {arm:<12} W={statistics.median(r['W'] for r in v):6.2f}  "
                  f"freq mean={statistics.median(r['freq_mean_mhz'] for r in v):7.1f}  "
                  f"max={statistics.median(r['freq_max_mhz'] for r in v):7.1f} MHz")
    print(f"\nwrote {SAMPLES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
