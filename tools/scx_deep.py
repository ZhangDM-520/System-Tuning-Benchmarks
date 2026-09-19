#!/usr/bin/env python3
"""scx_deep.py -- deep head-to-head: kernel BORE vs scx_pandemonium, with the learning mechanics isolated.

Why this exists. The first campaign (scx_ab.py) measured STEADY STATE: one fixed workload sequence,
always the same length, machine never required to pivot. That is precisely the regime where a static
scheduler and an adaptive one look identical -- and indeed `pandemonium --no-adaptive` measured equal
to adaptive pandemonium. That null does NOT test the adaptivity claim; it tests the one condition the
claim is silent about. This harness exercises the condition the claim is ABOUT: change.

Two instruments:

  phase : one slot = 6 workload classes x N cycles, metrics per phase. A tuner that learns should do
          better on cycle 2 than cycle 1, and that comparison is the point.
  alt   : the direct test. Alternate a saturating class and an interactive class every `--seg`
          seconds. Segments of the same type are then comparable across position, so "does it get
          better with repetition" and "how fast does it recover after a pivot" are both measurable.

A 1 Hz root-side trace (RAPL / backlight / /proc/stat) runs alongside every slot, because adaptation
is a trajectory and an aggregate cannot distinguish "adapted early" from "never adapted".

The third arm, `pandemonium@--no-adaptive`, is what makes the result interpretable: it is the same
scheduler with the Rust control loop switched off, so any adaptive-vs-static difference is
attributable to the learning layer specifically.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

# --- portability shim ---------------------------------------------------------
# ROOT    : repo root -- `data/` and `tools/` live directly under it
# SCRATCH : where generated workload trees are written (never inside the repo)
ROOT = os.environ.get("BENCH_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
SCRATCH = os.environ.get("BENCH_SCRATCH") or os.path.join(
    os.path.expanduser("~"), ".cache", "bench-scratch")

sys.path.insert(0, TOOLS)
import scx_ab as A  # noqa: E402

RESULTS = os.environ.get("SCX_DEEP_RESULTS") or os.path.join(DATA, "scx-deep.jsonl")
SAMPLER = os.path.join(TOOLS, "scx_sampler.py")

# Workload classes. Names are stable because they become part of every label.
PHASES = ["idle", "light", "sat", "storm", "io", "mem"]

# The interactive class used by the `alt` mode -- schbench with few workers is the closest thing to
# a user-facing responsiveness probe that is also repeatable.
ALT_SAT = "sat"
ALT_INT = "storm"


def slurp(p):
    return A.slurp(p)


def backlight():
    return slurp("/sys/class/backlight/amdgpu_bl1/brightness")


def start_sampler(dur):
    path = f"/tmp/scx-trace-{os.getpid()}.jsonl"
    proc = subprocess.Popen(["sudo", "-n", "python3", SAMPLER, path, str(dur)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, path


def load_trace(path):
    out = []
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return out


def stop_sampler(proc, path):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        pass
    trace = load_trace(path)
    A.run(["rm", "-f", path], as_root=True)
    return trace


def emit(row):
    with open(RESULTS, "a") as fh:
        fh.write(json.dumps(row) + "\n")


# CHUNK2

def _bracket(fn, *a, **k):
    """Run one phase bracketed by RAPL + PSI snapshots and backlight reads.

    The backlight is captured on both sides on purpose: a display that flips mid-phase changes
    package power by ~13 W, which is larger than any scheduler effect and would otherwise be
    silently attributed to the arm.
    """
    bl0 = backlight()
    s0 = A.fslat.snapshot()
    t0 = time.time()
    out = fn(*a, **k)
    wall = time.time() - t0
    s1 = A.fslat.snapshot()
    d = A.fslat.diff(s0, s1, wall)
    out["phase_J"] = round(A.rapl_j(s0, s1), 3)
    out["phase_wall_s"] = round(wall, 3)
    out["phase_W"] = round(out["phase_J"] / wall, 2) if wall > 0 else 0
    out["psi"] = d["psi_us"]
    out["bl_before"], out["bl_after"] = bl0, backlight()
    out["bl_stable"] = bl0 == out["bl_after"]
    return out


def ph_idle(dur):
    time.sleep(dur)
    return {}


def ph_light(dur):
    """Interactive class at low width: schbench with few workers plus one pinned spinner, so the
    scheduler has a real (if small) load to place."""
    spin = subprocess.Popen(["taskset", "-c", "0", "python3", "-c",
                             A.BUSY_LOOP.format(dur + 1)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out = A.w_schbench(max(3, int(dur)))
    spin.terminate()
    spin.wait()
    return out


def ph_sat(dur):
    """Saturating class, run as TWO half-length jobs.

    The split is the adaptation probe: a tuner that learns within a run should produce a better
    second half than first, so `rate_gain` is the signal. Comparing arms then shows whether
    pandemonium's learning layer actually beats a static policy on a repeated saturating burst.
    """
    half = max(3, int(dur // 2))
    r1 = A.w_stressng(24, half)
    r2 = A.w_stressng(24, half)
    out = {f"h1_{k}": v for k, v in r1.items()}
    out.update({f"h2_{k}": v for k, v in r2.items()})
    if r1.get("bogo_rate") and r2.get("bogo_rate"):
        out["rate_gain"] = round(r2["bogo_rate"] / r1["bogo_rate"] - 1, 4)
    return out


def ph_storm(dur):
    """Wakeup-storm class, also split in two so repetition is visible. This is the class where cake
    collapsed by 1.9x, so it is the most sensitive probe of scheduling policy available here."""
    loops = max(200, int(300 * dur))
    a = A.w_hackbench(loops=loops)
    b = A.w_hackbench(loops=loops)
    out = {"t1": a.get("hackbench_s"), "t2": b.get("hackbench_s"), "loops": loops}
    if a.get("hackbench_s") and b.get("hackbench_s"):
        out["storm_gain"] = round(b["hackbench_s"] / a["hackbench_s"] - 1, 4)
    return out


def ph_io(dur):
    """Metadata + durable-write class: a cold metadata walk, then a 512 MiB write forced to
    durability inside the timed window (that sync is what exposes writeback stalls)."""
    _, _, meta_s = A.w_wl("coldmeta")
    os.makedirs(SCRATCH, exist_ok=True)
    rc, _, _, io_s = A.run(["dd", "if=/dev/zero", f"of={SCRATCH}/big.bin", "bs=1M", "count=512",
                            "conv=fsync", "status=none"], timeout=dur + 60)
    return {"meta_s": meta_s, "dd_s": round(io_s, 3), "dd_rc": rc}


def ph_mem(dur):
    """Reclaim class: an anonymous balloon big enough to force reclaim against the 74.9 G zram
    swap, so the kernel must choose between evicting file pages and compressing anon pages."""
    rc, _, _, wall = A.run(["stress-ng", "--vm", "1", "--vm-bytes", "16G", "--vm-keep",
                            "--vm-hang", "0", "-t", f"{max(3, int(dur))}s", "--quiet"],
                           timeout=dur + 120)
    return {"vm_rc": rc, "vm_s": round(wall, 3)}


PHASE_FN = {"idle": ph_idle, "light": ph_light, "sat": ph_sat,
            "storm": ph_storm, "io": ph_io, "mem": ph_mem}


def run_phases(phases, dur):
    """Execute a list of phase names, returning per-phase metric dicts in order."""
    out = []
    for name in phases:
        m = _bracket(PHASE_FN[name], dur)
        m["phase"] = name
        out.append(m)
    return out


# CHUNK3

def slot(arm, args, rep, mode, pass_name, dur=8, cycles=2, seg=20, trace_dir=None,
         force=False, verbose=True):
    """One deep measurement slot for one arm.

    Deliberately does NOT route through cake between slots the way the first harness did. Recovery
    to cake would inject a third scheduler into the sequence and reset any adaptive state for
    reasons unrelated to the arm under test; instead the arm itself is asserted before the slot and
    `A.recover` is kept strictly as a failure escape hatch.
    """
    label = f"deep:{arm}" + (f"[{args}]" if args else "")
    base = {"label": label, "arm": arm, "args": args, "rep": rep, "mode": mode,
            "pass": pass_name, "valid": True, "invalid_reason": ""}

    st = A.set_arm(arm, args)
    base["set_arm"] = {k: st[k] for k in ("ok", "attach_ms", "ops", "state", "error")}
    if not st["ok"]:
        base.update(valid=False, invalid_reason="arm did not attach: " + st["error"])
        emit(base)
        A.recover()
        return 0

    if not force:
        quiet, idle = A.quiesce()
        base["gate_idle"] = idle
        if not quiet:
            base.update(valid=False, invalid_reason=f"quiescence gate failed (idle {idle})")
            emit(base)
            return 0

    if mode == "phase":
        seq = PHASES * cycles
        est = len(seq) * dur + 40
    else:
        seq = [ALT_SAT, ALT_INT] * cycles
        est = len(seq) * seg + 40

    proc, tpath = start_sampler(est)
    try:
        per = run_phases(seq, dur if mode == "phase" else seg)
    finally:
        trace = stop_sampler(proc, tpath)

    post_ops, post_state = A.ops_name(), A.sched_state()
    self_ok = (not post_ops) if arm == "bore" else post_ops.startswith(arm + "_")
    if not self_ok or (arm != "bore" and post_state != "enabled"):
        base.update(valid=False,
                    invalid_reason=f"arm lost during slot: ops={post_ops!r} state={post_state}")

    # Save the per-second trace: adaptation is a trajectory, so the aggregate alone is not enough.
    tdir = trace_dir or os.path.join(DATA, "traces")
    os.makedirs(tdir, exist_ok=True)
    tfile = os.path.join(tdir, f"{pass_name}-{arm.replace('/', '_')}"
                               f"{'-' + args.replace(' ', '_').replace('--', '') if args else ''}"
                               f"-r{rep}.jsonl")
    try:
        with open(tfile, "w") as fh:
            for r in trace:
                fh.write(json.dumps(r) + "\n")
    except OSError:
        pass

    cfg = dict(A.fslat.capture_settings())
    cfg.update(A.freqs())

    # One row per phase, so perf-stats.py can rank a single workload class on its own terms rather
    # than averaging unrelated classes together.
    for i, m in enumerate(per):
        cycle = i // len(PHASES) if mode == "phase" else i // 2
        row = dict(base)
        row["phase"] = m.get("phase", "?")
        row["cycle"] = cycle
        row["idx"] = i
        row["label"] = f"{label}:{row['phase']}"
        row["trace"] = os.path.basename(tfile)
        row["post"] = {"ops": post_ops, "state": post_state, "bl": backlight()}
        row["cfg"] = cfg
        row["wall_s"] = m.get("phase_wall_s", 0)
        row["rapl_J"] = m.get("phase_J", 0)
        row["avg_W"] = m.get("phase_W", 0)
        row["psi_us"] = m.get("psi", {})
        row["sched"] = {k: v for k, v in m.items() if k not in ("psi",)}
        emit(row)
        if verbose:
            key = {"sat": "h2_bogo_rate", "storm": "t2", "light": "sb_wk_pct99_0"}.get(row["phase"])
            extra = f" {key}={m.get(key)}" if key and m.get(key) is not None else ""
            print(f"      c{cycle} {row['phase']:<6} {m.get('phase_wall_s', 0):5.1f}s "
                  f"{m.get('phase_J', 0):7.2f}J bl={m.get('bl_before')}"
                  f"{'' if m.get('bl_stable') else '->' + str(m.get('bl_after'))}{extra}",
                  flush=True)
    return 0


def cmd_run(args):
    arms = [a for a in args.arms.split(",") if a.strip()]
    print(f"=== scx deep pass '{args.passname}': {len(arms)} arms x {args.reps} reps, mode={args.mode} ===")
    print(f"    arms: {', '.join(arms)}")
    print(f"    dur={args.dur}s seg={args.seg}s cycles={args.cycles}  results -> {RESULTS}", flush=True)
    started = time.time()
    for rep in range(args.reps):
        k = rep % len(arms)
        order = arms[k:] + arms[:k]
        print(f"\n--- rep {rep}  order: {' -> '.join(order)}", flush=True)
        for spec in order:
            arm, extra = A.parse_arm_spec(spec)
            t0 = time.time()
            slot(arm, extra, rep, args.mode, args.passname, dur=args.dur, cycles=args.cycles,
                 seg=args.seg, force=args.force, verbose=True)
            print(f"    [{arm}{' ' + extra if extra else ''}] slot {time.time() - t0:.1f}s", flush=True)
    print(f"\n=== deep run complete in {(time.time() - started) / 60:.1f} min ===")
    return 0


def _load(label_filter=None):
    rows = []
    if not os.path.exists(RESULTS):
        return rows
    with open(RESULTS) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if label_filter and not str(r.get("label", "")).startswith(label_filter):
                continue
            rows.append(r)
    return rows


def _dig(row, path):
    cur = row
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur if isinstance(cur, (int, float)) and not isinstance(cur, bool) else None


def cmd_summary(args):
    """Per-phase medians, plus the cycle-1 -> cycle-2 comparison that is the adaptivity signal."""
    rows = [r for r in _load(args.label) if r.get("valid", True)]
    if not rows:
        print("no valid rows")
        return 1
    by = {}
    for r in rows:
        by.setdefault(r["label"], []).append(r)

    keys = [("J", "sched.phase_J"), ("bl_stable", "sched.bl_stable"),
            ("sat rate1", "sched.h1_bogo_rate"), ("sat rate2", "sched.h2_bogo_rate"),
            ("sat rate_gain", "sched.rate_gain"),
            ("storm t1", "sched.t1"), ("storm t2", "sched.t2"), ("storm gain", "sched.storm_gain"),
            ("wk p50", "sched.sb_wk_pct50_0"), ("wk p99", "sched.sb_wk_pct99_0"),
            ("meta s", "sched.meta_s"), ("dd s", "sched.dd_s"), ("vm s", "sched.vm_s"),
            ("psi io some", "psi_us.io_some"), ("psi mem full", "psi_us.memory_full")]

    def med(lab, path, cycle=None):
        vals = []
        for r in by.get(lab, []):
            if cycle is not None and r.get("cycle") != cycle:
                continue
            v = _dig(r, path)
            if v is not None:
                vals.append(v)
        return statistics.median(vals) if vals else None

    for lab in sorted(by):
        c1 = [r for r in by[lab] if r.get("cycle") == 0]
        c2 = [r for r in by[lab] if r.get("cycle") == 1]
        print(f"\n=== {lab}   rows={len(by[lab])} (c1={len(c1)} c2={len(c2)})")
        for name, path in keys:
            v = med(lab, path)
            if v is None:
                continue
            line = f"    {name:<14} {v:>12.4g}"
            if c2:
                v1, v2 = med(lab, path, 0), med(lab, path, 1)
                if v1 not in (None, 0) and v2 is not None:
                    line += f"   (c1={v1:.4g} c2={v2:.4g} D={(v2 / v1 - 1) * 100:+.1f}%)"
            print(line)

    print("\n=== cycle trajectory per arm/label (the adaptivity signal) ===")
    for name, path in [("sat rate2", "sched.h2_bogo_rate"), ("storm t2", "sched.t2"),
                       ("sat rate_gain", "sched.rate_gain"),
                       ("wk p99", "sched.sb_wk_pct99_0"), ("wk p50", "sched.sb_wk_pct50_0")]:
        print(f"  {name}:")
        for lab in sorted(by):
            cycles = sorted({r.get("cycle", 0) for r in by[lab]})
            vals = [(c, med(lab, path, c)) for c in cycles]
            vals = [(c, v) for c, v in vals if v is not None]
            if len(vals) < 2:
                continue
            traj = "  ".join(f"c{c}={v:.4g}" for c, v in vals)
            first = vals[0][1]
            last = vals[-1][1]
            d = f"  D_last_first={(last / first - 1) * 100:+.1f}%" if first else ""
            print(f"    {lab:<42} {traj}{d}")
    return 0


def cmd_slot(args):
    arm, extra = A.parse_arm_spec(args.arm)
    if args.args:
        extra = args.args
    slot(arm, extra, args.rep, args.mode, args.passname, dur=args.dur, cycles=args.cycles,
         seg=args.seg, force=args.force, verbose=True)
    return 0


def cmd_trace(args):
    """Print the 1 Hz power/backlight trajectory for one saved slot."""
    path = os.path.join(DATA, "traces", args.file)
    rows = load_trace(path)
    if not rows:
        print(f"no trace at {path}")
        return 1
    prev = None
    for r in rows:
        extra = ""
        if prev and r.get("rapl") and prev.get("rapl"):
            extra = f"  {(r['rapl'] - prev['rapl']) / 1e6:6.2f} J/s"
        print(f"  t={r['t']:6.2f}  bl={r['bl']:>7}{extra}")
        prev = r
    return 0


def main():
    ap = argparse.ArgumentParser(description="deep BORE vs pandemonium harness")
    sub = ap.add_subparsers(dest="mode", required=True)

    p = sub.add_parser("run", help="interleaved deep run")
    p.add_argument("--reps", type=int, default=4)
    p.add_argument("--arms", default="bore,pandemonium,pandemonium@--no-adaptive")
    p.add_argument("--mode", default="phase", choices=["phase", "alt"])
    p.add_argument("--pass", dest="passname", default="deepA")
    p.add_argument("--dur", type=int, default=8, help="seconds per phase (phase mode)")
    p.add_argument("--seg", type=int, default=20, help="seconds per segment (alt mode)")
    p.add_argument("--cycles", type=int, default=2)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("slot", help="one slot")
    p.add_argument("--arm", required=True)
    p.add_argument("--args", default="")
    p.add_argument("--rep", type=int, default=0)
    p.add_argument("--mode", default="phase", choices=["phase", "alt"])
    p.add_argument("--pass", dest="passname", default="deepA")
    p.add_argument("--dur", type=int, default=8)
    p.add_argument("--seg", type=int, default=20)
    p.add_argument("--cycles", type=int, default=2)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_slot)

    p = sub.add_parser("summary", help="per-phase medians and cycle deltas")
    p.add_argument("--label", default="deep:")
    p.set_defaults(fn=cmd_summary)

    p = sub.add_parser("trace", help="print a saved 1 Hz trace")
    p.add_argument("file")
    p.set_defaults(fn=cmd_trace)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())


