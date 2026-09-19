#!/usr/bin/env python3
"""perf-stats -- read results.jsonl and answer "is this difference real?".

Two modes:

  perf-stats.py show [LABEL-PREFIX ...]
      Per-label noise band: n, min, median, max and spread% for every metric.
      This is how a control's own repeatability is established before any
      treatment is allowed to claim a win.

  perf-stats.py cmp CONTROL LABEL [LABEL ...]
  perf-stats.py test CONTROL LABEL [LABEL ...]
      Median of each treatment relative to CONTROL, as a percentage, with the
      control's own spread printed alongside.  A treatment only counts as a
      result when its delta exceeds the control's spread.

Metric paths are dotted into the result row, so `xfs.log.0` means "XFS log
counter index 0" -- for this kernel that is the count of forced log commits,
which is the direct measure of synchronous metadata round trips.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys

# --- portability shim ---------------------------------------------------------
ROOT = os.environ.get("BENCH_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

RESULTS = os.environ.get("FSLAT_RESULTS") or os.path.join(DATA, "tuning-results.jsonl")

METRICS = [
    "wall_s",
    "rapl_J",
    "avg_W",
    "psi_us.io_some",
    "psi_us.io_full",
    "psi_us.memory_full",
    "psi_us.cpu_some",
    "nvme.read_ios",
    "nvme.write_ios",
    "nvme.flush_ios",
    "nvme.io_ticks",
    "nvme.mean_queue_us",
    "xfs.log.0",
    "xfs.log.1",
    "xfs.log.2",
    "xfs.trans.1",
    "xfs.push_ail.0",
    "vmstat.pgmajfault",
    "vmstat.pgscan_kswapd",
    "vmstat.pgscan_direct",
    "vmstat.pgsteal_kswapd",
    "vmstat.pgsteal_direct",
    "vmstat.pswpout",
    "probe.launch_under_load_ms",
    # --- sched_ext campaign: throughput / overhead ---
    "sched.bogo_rate",
    "sched.bogo_ops",
    "sched.sys_frac",
    "sched.sys_ticks",
    "sched.hackbench_s",
    "sched.bypass_ms",
    "sched.bypass_dispatch",
    "sched.bypass_activate",
    # --- power efficiency ---
    "sched.j_per_bogo",
    "sched.bogo_per_J",
    "sched.j_per_hackbench",
    "sched.idle_J_per_s",
    "sched.light_J_per_s",
    "sched.slot_avg_W",
    # --- latency ---
    "sched.sb_wk_pct50_0",
    "sched.sb_wk_pct90_0",
    "sched.sb_wk_pct99_0",
    "sched.sb_wk_pct99_9",
    "sched.sb_wk_max",
    "sched.sb_rq_pct50_0",
    "sched.sb_rq_pct99_0",
    "sched.cyc_other_max",
    "sched.cyc_other_avg",
    "sched.cyc_fifo_max",
    # --- deep pass (phase-structured) ---
    "sched.h1_bogo_rate",
    "sched.h2_bogo_rate",
    "sched.rate_gain",
    "sched.t1",
    "sched.t2",
    "sched.storm_gain",
    "sched.phase_J",
    "sched.phase_W",
    "sched.meta_s",
    "sched.dd_s",
    "sched.vm_s",
    "sched.fast_share",
    "sched.fast_excess",
    "cfg.freq_fast_mhz",
    "cfg.freq_dense_mhz",
    "idle_frac_at_gate",
]

SHORT = {
    "wall_s": "wall s",
    "rapl_J": "pkg J",
    "avg_W": "avg W",
    "psi_us.io_some": "psi io some ms",
    "psi_us.io_full": "psi io FULL ms",
    "psi_us.memory_full": "psi mem FULL ms",
    "psi_us.cpu_some": "psi cpu some ms",
    "nvme.read_ios": "read ios",
    "nvme.write_ios": "write ios",
    "nvme.flush_ios": "flush ios",
    "nvme.io_ticks": "dev busy ms",
    "nvme.mean_queue_us": "qmean us",
    "xfs.log.0": "log FORCE",
    "xfs.log.1": "log blocks",
    "xfs.log.2": "log no-space",
    "xfs.trans.1": "trans commit",
    "xfs.push_ail.0": "ail pushes",
    "vmstat.pgmajfault": "majflt",
    "vmstat.pgscan_kswapd": "kswapd scan",
    "vmstat.pgscan_direct": "DIRECT scan",
    "vmstat.pgsteal_kswapd": "kswapd steal",
    "vmstat.pgsteal_direct": "DIRECT steal",
    "vmstat.pswpout": "pswpout",
    "probe.launch_under_load_ms": "launch ms",
    "sched.bogo_rate": "bogo ops/s",
    "sched.bogo_ops": "bogo ops",
    "sched.sys_frac": "sys frac",
    "sched.sys_ticks": "sys ticks",
    "sched.hackbench_s": "hackbench s",
    "sched.bypass_ms": "bypass ms",
    "sched.bypass_dispatch": "bypass disp",
    "sched.bypass_activate": "bypass act",
    "sched.j_per_bogo": "J/bogo-op",
    "sched.bogo_per_J": "bogo/J",
    "sched.j_per_hackbench": "J/hb msg",
    "sched.idle_J_per_s": "idle J/s",
    "sched.light_J_per_s": "light J/s",
    "sched.slot_avg_W": "slot avg W",
    "sched.sb_wk_pct50_0": "wk p50 us",
    "sched.sb_wk_pct90_0": "wk p90 us",
    "sched.sb_wk_pct99_0": "wk p99 us",
    "sched.sb_wk_pct99_9": "wk p99.9 us",
    "sched.sb_wk_max": "wk max us",
    "sched.sb_rq_pct50_0": "rq p50 us",
    "sched.sb_rq_pct99_0": "rq p99 us",
    "sched.cyc_other_max": "cyc OTHER max",
    "sched.cyc_other_avg": "cyc OTHER avg",
    "sched.cyc_fifo_max": "cyc FIFO max",
    "sched.launch_under_load_ms": "launch ms",
    "sched.fast_share": "fast share",
    "sched.fast_excess": "fast excess",
    "cfg.freq_fast_mhz": "fast MHz",
    "cfg.freq_dense_mhz": "dense MHz",
    "idle_frac_at_gate": "gate idle",
    "sched.h1_bogo_rate": "sat r1 ops/s",
    "sched.h2_bogo_rate": "sat r2 ops/s",
    "sched.rate_gain": "sat rate gain",
    "sched.t1": "storm t1 s",
    "sched.t2": "storm t2 s",
    "sched.storm_gain": "storm gain",
    "sched.phase_J": "phase J",
    "sched.phase_W": "phase W",
    "sched.meta_s": "meta s",
    "sched.dd_s": "dd s",
    "sched.vm_s": "vm s",
}


def get(row: dict, path: str):
    # XFS counter names themselves contain dots ("log.0"), so they are looked
    # up straight from the xfs sub-dict rather than being split.
    if path.startswith("xfs."):
        cur = row.get("xfs", {})
        return float(cur[path[4:]]) if path[4:] in cur else None
    cur = row
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    if isinstance(cur, bool) or not isinstance(cur, (int, float)):
        return None
    return float(cur)


def load() -> list[dict]:
    rows = []
    with open(RESULTS) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def collect(rows, label):
    return [r for r in rows if r.get("label") == label]


def band(vals):
    if not vals:
        return None
    lo, hi, med = min(vals), max(vals), statistics.median(vals)
    spread = (hi - lo) / med * 100 if med else 0.0
    return lo, med, hi, spread


def cmd_show(rows, prefixes):
    labels = [l for l in dict.fromkeys(r["label"] for r in rows)
              if not prefixes or any(l.startswith(p) for p in prefixes)]
    for label in labels:
        sel = collect(rows, label)
        note = sel[0].get("note", "")
        print(f"\n### {label}  n={len(sel)}  ({note})")
        print(f"    {'metric':<18} {'n':>2} {'min':>12} {'median':>12} "
              f"{'max':>12} {'spread%':>8}")
        for path in METRICS:
            vals = [v for v in (get(r, path) for r in sel) if v is not None]
            if not vals or all(v == 0 for v in vals):
                continue
            b = band(vals)
            print(f"    {SHORT.get(path, path):<18} {len(vals):>2} {b[0]:>12.2f} "
                  f"{b[1]:>12.2f} {b[2]:>12.2f} {b[3]:>8.1f}")


def cmd_cmp(rows, control, labels):
    ctl = collect(rows, control)
    if not ctl:
        print(f"no rows for control {control}", file=sys.stderr)
        return 1
    print(f"control {control}  n={len(ctl)}")
    print(f"    {'metric':<18} {'ctl med':>12} {'ctl spread%':>11} "
          + " ".join(f"{l:>18}" for l in labels))
    for path in METRICS:
        cvals = [v for v in (get(r, path) for r in ctl) if v is not None]
        if not cvals or all(v == 0 for v in cvals):
            continue
        cb = band(cvals)
        cells = []
        for label in labels:
            sel = collect(rows, label)
            vals = [v for v in (get(r, path) for r in sel) if v is not None]
            if not vals:
                cells.append(f"{'-':>18}")
                continue
            m = statistics.median(vals)
            pct = (m - cb[1]) / cb[1] * 100 if cb[1] else 0.0
            # A delta is only meaningful if it clears the control's own spread.
            mark = "*" if abs(pct) > cb[3] else " "
            cells.append(f"{m:>10.2f} {pct:>+5.1f}%{mark}")
        print(f"    {SHORT.get(path, path):<18} {cb[1]:>12.2f} {cb[3]:>11.1f} "
              + " ".join(cells))
    print("\n    '*' = delta exceeds the control's own min-max spread")
    return 0


def mannwhitney(a: list[float], b: list[float]):
    """Two-sided Mann-Whitney U with tie correction, normal approximation.

    min/max spread is outlier-driven: one slow repetition inflates it and hides
    a real, consistent shift.  A rank test asks the better question -- "do the
    two arms interleave, or do they separate?" -- and is immune to the size of
    the outliers.  Returns (U, z, p).
    """
    n1, n2 = len(a), len(b)
    if n1 < 3 or n2 < 3:
        return None
    combined = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks: dict[int, float] = {}
    i = 0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        i = j + 1
    rank_a = sum(ranks[k] for k in range(len(combined)) if combined[k][1] == 0)
    u1 = rank_a - n1 * (n1 + 1) / 2
    mu = n1 * n2 / 2
    # tie correction
    tie_term = 0.0
    i = 0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        t = j - i + 1
        if t > 1:
            tie_term += t ** 3 - t
        i = j + 1
    n = n1 + n2
    sd = ((n1 * n2 / 12) * ((n + 1) - tie_term / (n * (n - 1)))) ** 0.5
    if sd == 0:
        return None
    z = (u1 - mu) / sd
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / (2 ** 0.5))))
    return u1, z, max(min(p, 1.0), 0.0)


def cmd_test(rows, control, labels):
    ctl = collect(rows, control)
    print(f"control {control}  n={len(ctl)}")
    print(f"    {'metric':<18} " + " ".join(f"{l:>26}" for l in labels))
    for path in METRICS:
        cvals = [v for v in (get(r, path) for r in ctl) if v is not None]
        if len(cvals) < 3 or all(v == 0 for v in cvals):
            continue
        cells = []
        for label in labels:
            sel = collect(rows, label)
            vals = [v for v in (get(r, path) for r in sel) if v is not None]
            res = mannwhitney(cvals, vals)
            if not res:
                cells.append(f"{'-':>26}")
                continue
            _, z, p = res
            m1, m2 = statistics.median(cvals), statistics.median(vals)
            pct = (m2 - m1) / m1 * 100 if m1 else 0.0
            flag = "SIG" if p < 0.05 else "   "
            cells.append(f"{pct:>+8.1f}% p={p:.3f} {flag}")
        print(f"    {SHORT.get(path, path):<18} " + " ".join(cells))
    print("\n    SIG = Mann-Whitney p < 0.05 (rank-separated, outlier-immune)")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    rows = load()
    if not argv or argv[0] == "show":
        cmd_show(rows, argv[1:])
        return 0
    if argv[0] == "cmp" and len(argv) >= 3:
        return cmd_cmp(rows, argv[1], argv[2:])
    if argv[0] == "test" and len(argv) >= 3:
        return cmd_test(rows, argv[1], argv[2:])
    if argv[0] == "labels":
        for label in dict.fromkeys(r["label"] for r in rows):
            print(label)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
