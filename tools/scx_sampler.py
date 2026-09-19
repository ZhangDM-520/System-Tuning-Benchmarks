#!/usr/bin/env python3
"""scx_sampler.py -- 1 Hz trace sampler. Runs as root (RAPL is root-only).

Launched by scx_deep.py around a slot so that adaptation can be seen as a *trajectory* rather than
a single aggregate. A tuner that learns produces a visible change over time; an aggregate per-phase
number cannot distinguish "adapted early" from "never adapted".

Records, per tick:
  * package RAPL energy (cumulative uJ) -- differenced by the analyser
  * the panel backlight level, because a display that flips costs ~13 W and would otherwise be
    silently attributed to the scheduler (this is exactly what voided Pass A's idle comparison)
  * aggregate /proc/stat, so CPU utilisation and user/sys split are available per second

Usage: scx_sampler.py <outfile.jsonl> <duration_seconds> [interval_seconds]
"""

import json
import os
import sys
import time


def slurp(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        print("Usage: sudo scx_sampler.py <outfile.jsonl> <duration_seconds> "
              "[interval_seconds=1.0]\n"
              "Needs root: RAPL is root-only.")
        return 0 if len(sys.argv) > 1 else 2
    out = sys.argv[1]
    dur = float(sys.argv[2])
    interval = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    t0 = time.time()
    with open(out, "w") as fh:
        while time.time() - t0 < dur:
            try:
                rapl = int(slurp("/sys/class/powercap/intel-rapl:0/energy_uj") or 0)
            except ValueError:
                rapl = 0
            fr = []
            for c in range(24):
                v = slurp(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq")
                if v.isdigit():
                    fr.append(int(v))
            first = slurp("/proc/stat").splitlines()[0].split()
            fh.write(json.dumps({
                "t": round(time.time() - t0, 3),
                "rapl": rapl,
                "bl": slurp("/sys/class/backlight/amdgpu_bl1/brightness"),
                "cpu": [int(x) for x in first[1:]] if len(first) > 4 else [],
                "fk": fr,
            }) + "\n")
            fh.flush()
            time.sleep(interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
