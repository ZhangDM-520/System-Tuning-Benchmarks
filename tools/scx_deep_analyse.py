#!/usr/bin/env python3
"""scx_deep_analyse.py -- per-phase comparison of BORE vs pandemonium, with regime filtering.

The deep pass spans a display-blank event: the panel went from 300000 to 0 part-way through, and a
display that flips costs ~13 W -- larger than any scheduler effect. So every row carries its
backlight level before and after its phase, and this analyser can restrict to phases where the panel
did NOT move, and optionally to a single panel state, before comparing arms.

Per phase it reports each arm's median and runs a Mann-Whitney U test (tie-corrected, imported from
perf-stats.py so the statistic is identical to the rest of the campaign) of each arm against BORE.

Usage:
  scx_deep_analyse.py [--bl 0|300000|any] [--passes deepA,deepA2] [--metric PATH ...]
"""

import argparse
import json
import os
import statistics
import sys

# --- portability shim ---------------------------------------------------------
ROOT = os.environ.get("BENCH_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")

import importlib.util

spec = importlib.util.spec_from_file_location("perfstats", os.path.join(TOOLS, "perf-stats.py"))
perfstats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(perfstats)

RESULTS = os.environ.get("SCX_DEEP_RESULTS") or os.path.join(DATA, "scx-deep.jsonl")

# Lower is better for everything marked here; used only for the "winner" annotation.
LOWER_BETTER = {
    "sched.t1", "sched.t2", "sched.storm_gain", "sched.sb_wk_pct50_0", "sched.sb_wk_pct99_0",
    "sched.phase_J", "sched.phase_W", "sched.meta_s", "sched.dd_s", "sched.vm_s",
    "psi_us.io_some", "psi_us.io_full", "psi_us.memory_full", "psi_us.cpu_some",
}
DEFAULT_METRICS = [
    "sched.h2_bogo_rate", "sched.rate_gain", "sched.t2", "sched.storm_gain",
    "sched.sb_wk_pct50_0", "sched.sb_wk_pct99_0", "sched.sb_wk_max",
    "sched.phase_J", "sched.phase_W", "sched.meta_s", "sched.dd_s", "sched.vm_s",
]


def load(passes=None):
    rows = []
    with open(RESULTS) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if passes and r.get("pass") not in passes:
                continue
            if not r.get("valid", True):
                continue
            rows.append(r)
    return rows


def regime_ok(row, want):
    bl0 = row.get("sched", {}).get("bl_before")
    bl1 = row.get("sched", {}).get("bl_after")
    if bl0 != bl1:
        return False
    if want in (None, "any"):
        return True
    return bl0 == want


def armkey(row):
    """Arm identity including its configuration.

    `row["arm"]` alone is not enough: the deep pass runs pandemonium twice, once with
    `--no-adaptive`, and keying on arm name alone would silently pool the adaptive and static arms
    together -- which is exactly the comparison this pass exists to make.
    """
    return row["arm"] + (f"[{row['args']}]" if row.get("args") else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bl", default="any", help="restrict to this panel level (or 'any')")
    ap.add_argument("--passes", default="", help="comma-separated pass names to include")
    ap.add_argument("--metric", action="append", default=None)
    ap.add_argument("--control", default="bore")
    ap.add_argument("--exclude-reps", default="0",
                    help="comma-separated reps to drop. Rep 0 of a run is dropped by default: the "
                         "rotation makes the first arm (bore) run first, and harness/agent activity "
                         "at launch contaminates it -- visible as idle energy of 88-124 J against a "
                         "~29 J baseline in BOTH deep runs.")
    args = ap.parse_args()

    passes = [p for p in args.passes.split(",") if p] or None
    metrics = args.metric or DEFAULT_METRICS
    bad = {int(x) for x in args.exclude_reps.split(",") if x.strip().isdigit()}
    rows = [r for r in load(passes) if r.get("rep") not in bad]
    kept = [r for r in rows if regime_ok(r, args.bl)]
    dropped = len(rows) - len(kept)
    print(f"rows={len(rows)} kept={len(kept)} dropped_by_regime={dropped} "
          f"excluded_reps={sorted(bad) or 'none'} passes={passes or 'ALL'} bl={args.bl}")

    by = {}
    for r in kept:
        by.setdefault(r["phase"], {}).setdefault(armkey(r), []).append(r)

    for phase in sorted(by):
        arms = sorted(by[phase])
        n = {a: len(by[phase][a]) for a in arms}
        print(f"\n=== phase {phase}   n: " + "  ".join(f"{a}={n[a]}" for a in arms))
        others = [a for a in arms if a != args.control]
        print(f"    {'metric':<20}{args.control:>13}"
              + "".join(f"{a:>13}" for a in others)
              + "   " + " ".join(f"{'p(' + a[:6] + ')':>10}" for a in others))
        for path in metrics:
            ctl = [v for v in (perfstats.get(r, path) for r in by[phase].get(args.control, []))
                   if v is not None]
            if len(ctl) < 3:
                continue
            cells = [f"{statistics.median(ctl):>13.4g}"]
            tests = []
            for a in others:
                vals = [v for v in (perfstats.get(r, path) for r in by[phase][a]) if v is not None]
                cells.append(f"{statistics.median(vals):>13.4g}" if vals else f"{'-':>13}")
                if len(vals) >= 3:
                    res = perfstats.mannwhitney(ctl, vals)
                    tests.append(f"{res[2]:>9.3f}{'*' if res and res[2] < 0.05 else ' '}"
                                 if res else f"{'n/a':>10}")
                else:
                    tests.append(f"{'n/a':>10}")
            print(f"    {path.split('.', 1)[1]:<20}" + "".join(cells) + "   " + " ".join(tests))
    return 0


if __name__ == "__main__":
    sys.exit(main())
