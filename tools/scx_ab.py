#!/usr/bin/env python3
"""scx_ab.py -- A/B harness for comparing sched_ext schedulers on this machine.

Compares arms on the three axes the campaign cares about:
  * power efficiency  -- RAPL joules per unit of completed work, and idle/light-load draw
  * latency           -- schbench percentiles, cyclictest, interactive launch, PSI stalls
  * overhead          -- fixed-work wall time, sys-time share, hackbench, SCX_EV_* bypass

Reuses fslat.py's proven snapshot/diff code rather than duplicating it, so rows carry the same
`psi_us`, `vmstat`, `nvme`, `xfs` shape that perf-stats.py already knows how to read.

Design rules enforced here, each learned from a real failure during reconnaissance:
  1. `scxctl start` REFUSES while a scheduler is attached -> use `switch`; `start` only when
     nothing is attached.
  2. `--args` values beginning with '-' are eaten by clap -> always pass `--args=VALUE`.
  3. scxctl exit codes are unreliable, so every state assertion reads /sys/kernel/sched_ext.
  4. A scheduler that fails to start leaves sched_ext `disabled`, which silently poisons every
     subsequent arm. Recovery is mandatory between arms, and a row produced against a disabled
     sched_ext is invalid rather than merely unlucky.
  5. The arm is verified twice: /sys/kernel/sched_ext/root/ops read-back AND schbench's
     self-reported `sched_ext` field. A mismatch voids the row.

Non-interactive workloads are the default. fslat.py is imported by path.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
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
import fslat  # noqa: E402  (path insertion must precede this import)

OPS = "/sys/kernel/sched_ext/root/ops"
STATE = "/sys/kernel/sched_ext/state"
REJ = "/sys/kernel/sched_ext/nr_rejected"
EVENTS = "/sys/kernel/sched_ext/root/events"
RESULTS = os.environ.get("SCX_AB_RESULTS") or os.path.join(DATA, "scx-pass-ab.jsonl")
RECEIPT = os.path.join(DATA, "scx-receipt.json")
WL = os.path.join(TOOLS, "wl.sh")

# Zen 5 (fast) and Zen 5c (dense) clusters. The fast cluster is 8 of 24 logical CPUs, so a
# perfectly spread unpinned workload lands ~33.3% of its time there; deviation is placement.
FAST_CPUS = [0, 1, 2, 3, 12, 13, 14, 15]
DENSE_CPUS = [4, 5, 6, 7, 8, 9, 10, 11, 16, 17, 18, 19, 20, 21, 22, 23]
FAIR_FAST_SHARE = len(FAST_CPUS) / 24.0

ALL_ARMS = ["bore", "cake", "bpfland", "cosmos", "pandemonium"]


def slurp(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def ops_name():
    """Name of the attached sched_ext scheduler, or '' when sched_ext is detached (BORE)."""
    return slurp(OPS).splitlines()[0].strip() if slurp(OPS) else ""


def sched_state():
    return slurp(STATE)


def nr_rejected():
    return int(slurp(REJ) or 0)


def scx_events():
    """Kernel-generated, scheduler-agnostic sched_ext counters.

    These are cumulative since attach, so callers take deltas. They are the only overhead
    instrument that works identically for every arm, including the detached BORE case.
    """
    out = {}
    for line in slurp(EVENTS).splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def cpu_stat():
    """Aggregate and per-CPU jiffy counters from /proc/stat.

    Used for two things: the sys-time share of a fixed-work job (scheduler bookkeeping shows up
    as system time), and hybrid placement (which cluster actually ran the work).
    """
    agg = {"busy": 0, "idle": 0}
    per = {}
    for line in slurp("/proc/stat").splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        if parts[0] == "cpu":
            vals = [int(x) for x in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            agg = {"busy": sum(vals) - idle, "idle": idle,
                   "user": vals[0] + (vals[1] if len(vals) > 1 else 0),
                   "sys": vals[2]}
        else:
            try:
                idx = int(parts[0][3:])
            except ValueError:
                continue
            vals = [int(x) for x in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            per[idx] = {"busy": sum(vals) - idle, "idle": idle}
    return agg, per


def freqs():
    """Sample effective CPU frequency per cluster, plus the EPP/governor actually in force.

    This is the confound control. cosmos enables sched_ext cpufreq control by default and
    bpfland's banner claims a perf level, but this machine has no schedutil governor -- so an
    energy or latency difference could be a frequency effect wearing a scheduling costume.
    """
    fast, dense = [], []
    for c in FAST_CPUS + DENSE_CPUS:
        v = slurp(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq")
        if v.isdigit():
            (fast if c in FAST_CPUS else dense).append(int(v) / 1000.0)
    epps = {slurp(f"/sys/devices/system/cpu/cpu{c}/cpufreq/energy_performance_preference")
            for c in (0, 8, 16)}
    return {
        "freq_fast_mhz": round(statistics.mean(fast), 1) if fast else 0,
        "freq_dense_mhz": round(statistics.mean(dense), 1) if dense else 0,
        "epp": ",".join(sorted(e for e in epps if e)) or "unknown",
        "governor": slurp("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "platform_profile": slurp("/sys/firmware/acpi/platform_profile"),
    }


def rapl_j(before, after):
    return fslat.wrap_delta(after["rapl"], before["rapl"], after["rapl_range"]) / 1e6


def psi_totals(snap):
    return {d: snap.get(f"psi_{d}", {}).get("some", 0) for d in ("cpu", "io", "memory")}


def run(cmd, timeout=None, as_root=False, env=None):
    """Run a command, returning (rc, stdout, stderr, wall_seconds).

    Never raises on a non-zero exit: several instruments signal interesting states through their
    return code, and silently losing the output would be worse than recording it.
    """
    if as_root:
        cmd = ["sudo", "-n"] + cmd
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr, time.time() - t0
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", exc.stderr or "", time.time() - t0
    except FileNotFoundError as exc:
        return 127, "", str(exc), time.time() - t0


def read_maybe_root(path):
    """Read a file that an as-root instrument may have created."""
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        rc, out, _, _ = run(["cat", path], as_root=True)
        return out if rc == 0 else ""


# ---------------------------------------------------------------- arm control

def set_arm(arm, args=None, wait=15.0, settle=2.0, verbose=False):
    """Move to `arm` and prove it took effect.

    Returns a dict describing what happened. `ok` is True only when the target is attached AND
    sched_ext is enabled AND it is still that way after `settle` seconds. Anything else -- a
    refused switch, an attach that is then torn down, a disabled sched_ext -- yields ok=False
    with the evidence attached, because the callers must treat those cases differently.
    """
    detail = {"target": arm, "args": args, "ops": "", "state": "", "ok": False, "error": ""}
    attached = bool(ops_name())

    if arm == "bore":
        cmd = ["scxctl", "stop"]
    else:
        # `start` refuses while something is attached; `switch` is required to change arms.
        verb = "switch" if attached else "start"
        cmd = ["scxctl", verb, "--sched", arm]
        if args:
            # The equals form is mandatory: clap mistakes a leading '-' for a flag.
            cmd.append(f"--args={args}")

    t0 = time.time()
    rc, out, err, _ = run(cmd)
    detail["scxctl"] = (out + err).strip().replace("\n", " ")[:300]
    detail["scxctl_rc"] = rc

    want = "" if arm == "bore" else arm + "_"
    while time.time() - t0 < wait:
        cur = ops_name()
        if arm == "bore":
            if not cur:
                break
        elif cur.startswith(want):
            break
        # A failed start leaves sched_ext disabled and the loader retrying; waiting the full
        # timeout would just waste 15 s per failed arm.
        if sched_state() == "disabled" and time.time() - t0 > 3:
            break
        time.sleep(0.1)

    detail["attach_ms"] = int((time.time() - t0) * 1000)
    time.sleep(settle)
    detail["ops"] = ops_name()
    detail["state"] = sched_state()
    detail["nr_rejected"] = nr_rejected()

    if arm == "bore":
        # For the detached arm the assertion is simply that NOTHING is attached.
        # /sys/kernel/sched_ext/state reads `disabled` whenever no scheduler is loaded, which is
        # the expected consequence of detaching rather than a failure -- an earlier version of
        # this check required state != disabled and therefore rejected every BORE slot.
        detail["ok"] = not detail["ops"]
    else:
        detail["ok"] = detail["ops"].startswith(want) and detail["state"] == "enabled"
    if not detail["ok"]:
        detail["error"] = (f"target={arm} ops={detail['ops']!r} state={detail['state']} "
                           f"scxctl={detail['scxctl']!r}")
    if verbose:
        print(f"    set_arm {arm}{' ' + args if args else ''}: ok={detail['ok']} "
              f"attach={detail['attach_ms']}ms ops={detail['ops']!r} state={detail['state']}",
              file=sys.stderr)
    return detail


def recover(target=None, tries=40):
    """Force the machine back to a healthy scheduler.

    This exists because a scheduler that fails to start leaves /sys/kernel/sched_ext/state
    `disabled`, and /sys/kernel/sched_ext/state is read-only -- recovery must go through the
    loader. Without this, one bad arm silently invalidates every later arm, which is exactly the
    trap that produced two false "SURVIVED" verdicts during reconnaissance.

    `target=None` means "whatever the loader is configured to run at boot", resolved through
    scxctl's own `restore` -- deliberately not a hardcoded arm name, so this harness cannot leave a
    machine on a scheduler its owner never chose. Set `SCX_RESTORE_ARM` (or pass a target) to pin it.
    """
    pinned = target or os.environ.get("SCX_RESTORE_ARM") or None

    def healthy():
        if sched_state() != "enabled":
            return False
        return bool(ops_name()) if pinned is None else ops_name().startswith(pinned + "_")

    for _ in range(tries):
        if healthy():
            return True
        if ops_name():
            if pinned is None:
                run(["scxctl", "restore"])
            else:
                run(["scxctl", "switch", "--sched", pinned])
        else:
            if pinned is None:
                run(["scxctl", "restore"])
            else:
                run(["scxctl", "start", "--sched", pinned])
                run(["scxctl", "restore"])
        time.sleep(1)
    run(["systemctl", "restart", "scx_loader"], as_root=True)
    time.sleep(5)
    return healthy()


def quiesce(min_idle=0.55, secs=2.0):
    """Refuse to measure on a busy machine.

    The desktop stays in use throughout this campaign, so instead of assuming quiet we sample
    real CPU utilisation and reject the slot. A slot measured during a compile or a heavy page
    load is discarded rather than averaged in, which is what keeps the ranking honest.
    """
    a, _ = cpu_stat()
    time.sleep(secs)
    b, _ = cpu_stat()
    dt = (b["busy"] + b["idle"]) - (a["busy"] + a["idle"])
    busy = (b["busy"] - a["busy"]) / dt if dt > 0 else 1.0
    return (1.0 - busy) >= min_idle, round(1.0 - busy, 3)


# ---------------------------------------------------------------- instruments

def w_schbench(dur=5):
    """schbench is the primary instrument: it reports wakeup-latency percentiles AND throughput,
    and it records the scheduler it ran under, so a row can never be mis-attributed."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
        path = fh.name
    rc, _, err, wall = run(["schbench", "-m", "2", "-t", "12", "-r", str(dur),
                            "-j", path, "-J", "scxab"], timeout=dur + 40)
    raw = read_maybe_root(path)
    try:
        os.unlink(path)
    except OSError:
        pass
    out = {"schbench_wall_s": round(wall, 3), "schbench_rc": rc, "schbench_self": ""}
    try:
        data = json.loads(raw)
    except Exception:
        out["schbench_err"] = (err or "")[:200]
        return out
    out["schbench_self"] = data.get("normal", {}).get("sched_ext", "")
    i = data.get("int", {})
    for k in ("wakeup_latency_pct50.0", "wakeup_latency_pct90.0", "wakeup_latency_pct99.0",
              "wakeup_latency_pct99.9", "wakeup_latency_max",
              "request_latency_pct50.0", "request_latency_pct99.0", "request_latency_max",
              "rps_pct50.0", "rps_pct99.0"):
        if k in i:
            out["sb_" + k.replace(".", "_").replace("wakeup_latency_", "wk_")
                            .replace("request_latency_", "rq_").replace("rps_", "rps")] = i[k]
    return out


def w_cyclictest(policy, prio, dur, cpus="0-3", under_load=False):
    """cyclictest in two roles.

    --policy=other is the scheduler-sensitive measurement: those threads are SCHED_OTHER, which
    sched_ext does schedule. --policy=fifo is an INVARIANT CONTROL: sched_ext is handed only
    NORMAL/BATCH/IDLE, so RT-class threads never touch it and their numbers must be identical
    across arms. If the FIFO row moves, the harness is contaminated, not the scheduler.
    """
    load = None
    if under_load:
        load = subprocess.Popen(["stress-ng", "--cpu", "10", "-t", f"{dur + 1}s", "--quiet"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
    path = f"/tmp/scx-cyc-{policy}-{os.getpid()}.json"
    rc, _, err, _ = run(["cyclictest", "-a", cpus, "-t", "4", "-p", str(prio),
                         f"--policy={policy}", "-i", "500", "-D", f"{dur}s", "-q",
                         f"--json={path}"], timeout=dur + 20, as_root=True)
    if load:
        load.terminate()
        load.wait()
    raw = read_maybe_root(path)
    try:
        os.unlink(path)
    except OSError:
        pass
    out = {f"cyc_{policy}_rc": rc}
    try:
        data = json.loads(raw)
    except Exception:
        out[f"cyc_{policy}_err"] = (err or "")[:200]
        return out
    threads = list(data.get("thread", {}).values())
    if threads:
        out[f"cyc_{policy}_max"] = max(t.get("max", 0) for t in threads)
        out[f"cyc_{policy}_avg"] = round(
            statistics.mean(t.get("avg", 0) for t in threads), 2)
        out[f"cyc_{policy}_min"] = min(t.get("min", 0) for t in threads)
    return out


def w_hackbench(loops=1500, groups=10):
    """hackbench at fixed work: pure context-switch / wakeup cost. Lower wall time = less overhead."""
    rc, out, err, wall = run(["hackbench", "-s", "1024", "-l", str(loops), "-g", str(groups),
                              "-T"], timeout=120)
    txt = out + err
    val = None
    for line in txt.splitlines():
        if line.startswith("Time:"):
            try:
                val = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return {"hackbench_s": val if val is not None else 0, "hackbench_wall_s": round(wall, 3),
            "hackbench_rc": rc, "hackbench_loops": loops}


def w_stressng(cpus=24, dur=10, method="matrixprod"):
    """Fixed-work CPU job. Gives throughput (bogo ops/s), the sys-time share (scheduler
    bookkeeping lands in system time), and the hybrid placement split."""
    a_agg, a_per = cpu_stat()
    # NOTE: --quiet must NOT be used here. It suppresses stress-ng's `metrc:` lines, which is
    # exactly where the bogo-ops figures come from; using it silently zeroed the throughput axis.
    rc, out, err, wall = run(["stress-ng", "--cpu", str(cpus), "--cpu-method", method,
                              "-t", f"{dur}s", "--metrics-brief"], timeout=dur + 60)
    b_agg, b_per = cpu_stat()
    txt = out + err
    bogo = real = usr = sys = bogo_rate = 0.0
    for line in txt.splitlines():
        if "metrc:" in line and " cpu " in line:
            nums = line.split()
            try:
                i = nums.index("cpu")
                bogo, real, usr, sys = (float(nums[i + 1]), float(nums[i + 2]),
                                        float(nums[i + 3]), float(nums[i + 4]))
                bogo_rate = float(nums[i + 5])
            except (ValueError, IndexError):
                pass
    d_busy = b_agg["busy"] - a_agg["busy"]
    d_sys = b_agg["sys"] - a_agg["sys"]
    d_user = b_agg["user"] - a_agg["user"]

    def cluster_busy(per_a, per_b, cpus_list):
        return sum(per_b[c]["busy"] - per_a[c]["busy"] for c in cpus_list if c in per_b and c in per_a)

    fb = cluster_busy(a_per, b_per, FAST_CPUS)
    db = cluster_busy(a_per, b_per, DENSE_CPUS)
    shade = fb / (fb + db) if (fb + db) > 0 else 0
    return {
        "bogo_ops": bogo, "bogo_real_s": real, "bogo_rate": bogo_rate,
        "stressng_wall_s": round(wall, 3), "stressng_rc": rc,
        "cpu_usr_s": usr, "cpu_sys_s": sys,
        "sys_ticks": d_sys, "user_ticks": d_user, "busy_ticks": d_busy,
        # System time as a share of consumed CPU time: the scheduler's own cost shows up here.
        "sys_frac": round(d_sys / d_busy, 4) if d_busy > 0 else 0,
        "fast_ticks": fb, "dense_ticks": db,
        "fast_share": round(shade, 4),
        # >1 means the fast cluster got more than its 33.3% pro-rata share of the work.
        "fast_excess": round(shade / FAIR_FAST_SHARE, 3) if shade else 0,
    }


def w_wl(name, *args, timeout=180):
    """Run one of wl.sh's proven filesystem workloads."""
    rc, out, err, wall = run(["bash", WL, name, *[str(a) for a in args]], timeout=timeout)
    probes = {}
    for line in (err + out).splitlines():
        if line.startswith("PROBE "):
            for tok in line.split()[1:]:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    try:
                        probes[k] = float(v)
                    except ValueError:
                        probes[k] = v
    return probes, rc, round(wall, 3)


def with_rapl(fn, *args, **kwargs):
    """Run an instrument bracketed by RAPL snapshots, so energy can be attributed to that
    specific workload rather than smeared across the whole slot."""
    a = fslat.snapshot()
    out = fn(*args, **kwargs)
    b = fslat.snapshot()
    return out, round(rapl_j(a, b), 3)


# ---------------------------------------------------------------- the battery

BUSY_LOOP = ("import time;t=time.time()+{};"
             "exec('while time.time()<t:\\n pass')")


def battery(scale=1.0, verbose=False):
    """One consolidated slot covering all six workload families and all three axes.

    Deliberately a single sequence rather than six separate passes: the budget only allows
    4 arms x 5 reps, so every workload has to earn its seconds. Idle and light-load windows are
    measured first, before any workload can leave the machine hot.
    """
    m = {}
    ev0 = scx_events()
    slot_a = fslat.snapshot()

    # --- power efficiency: idle and light load, measured cold ---------------------
    idle_s = max(3.0, 6.0 * scale)
    s0 = fslat.snapshot()
    time.sleep(idle_s)
    s1 = fslat.snapshot()
    m["idle_J"] = round(rapl_j(s0, s1), 3)
    m["idle_s"] = idle_s
    m["idle_J_per_s"] = round(m["idle_J"] / idle_s, 3)

    light_s = max(3.0, 6.0 * scale)
    s0 = fslat.snapshot()
    run(["taskset", "-c", "0", "python3", "-c", BUSY_LOOP.format(light_s)], timeout=light_s + 30)
    s1 = fslat.snapshot()
    m["light_J"] = round(rapl_j(s0, s1), 3)
    m["light_J_per_s"] = round(m["light_J"] / light_s, 3)

    # --- latency: schbench (percentiles + throughput, self-labelled) --------------
    m.update(w_schbench(int(round(5 * scale))))

    # --- latency: cyclictest, scheduler-sensitive then the RT invariant control ---
    m.update(w_cyclictest("other", 0, int(round(5 * scale)), under_load=True))
    m.update(w_cyclictest("fifo", 90, max(2, int(round(3 * scale)))))

    # --- overhead: hackbench at fixed work, with its own energy -------------------
    hb, hb_j = with_rapl(w_hackbench)
    m.update(hb)
    m["hackbench_J"] = hb_j
    if hb.get("hackbench_loops"):
        m["j_per_hackbench"] = round(hb_j / hb["hackbench_loops"], 7)

    # --- overhead + hybrid placement: 24-thread fixed work ------------------------
    sg, sg_j = with_rapl(w_stressng, 24, int(round(10 * scale)))
    m.update(sg)
    m["work_J"] = sg_j
    if sg.get("bogo_ops"):
        m["j_per_bogo"] = round(sg_j / sg["bogo_ops"], 8)
        m["bogo_per_J"] = round(sg["bogo_ops"] / sg_j, 3) if sg_j else 0

    # --- latency: interactive launch under a large write (the known symptom) ------
    probes, _, wud = w_wl("launchunderload", 2048, 2)
    m.update(probes)
    m["launchunderload_wall_s"] = wud

    # --- I/O metadata storm over the reused tree, then memory pressure ------------
    _, _, cm = w_wl("coldmeta")
    m["coldmeta_s"] = cm
    _, _, mp = w_wl("mempress", 24, max(3, int(round(6 * scale))))
    m["mempress_s"] = mp

    # --- whole-slot deltas: PSI stalls and the kernel's own bypass accounting -----
    slot_b = fslat.snapshot()
    d = fslat.diff(slot_a, slot_b, slot_b["t"] - slot_a["t"])
    m["psi_us"] = d["psi_us"]
    m["slot_rapl_J"] = d["rapl_J"]
    m["slot_wall_s"] = d["wall_s"]
    m["slot_avg_W"] = d["avg_W"]
    m["vmstat"] = d["vmstat"]

    ev1 = scx_events()
    m["ev"] = {}
    for k in sorted(set(ev0) | set(ev1)):
        dv = ev1.get(k, 0) - ev0.get(k, 0)
        if dv:
            m["ev"][k] = dv
    # Bypass is the kernel stepping in when the scheduler could not dispatch: the most direct
    # scheduler-overhead signal available, and it is generated by the kernel, not the scheduler.
    m["bypass_ms"] = round(m["ev"].get("SCX_EV_BYPASS_DURATION", 0) / 1e6, 3)
    m["bypass_activate"] = m["ev"].get("SCX_EV_BYPASS_ACTIVATE", 0)
    m["bypass_dispatch"] = m["ev"].get("SCX_EV_BYPASS_DISPATCH", 0)
    return m


# ---------------------------------------------------------------- slot + receipt

def binary_hash(arm):
    """Receipt: the hash of the exact binary under test, so a row stays attributable later."""
    if arm == "bore":
        return ""
    rc, out, _, _ = run(["sha256sum", f"/usr/bin/scx_{arm}"])
    return out.split()[0] if rc == 0 and out else ""


def receipt(arm, args):
    return {
        "arm": arm,
        "args": args,
        "binary_sha256": binary_hash(arm),
        "kernel": os.uname().release,
        "ops_at_entry": ops_name(),
        "nr_rejected": nr_rejected(),
        "sched_bore": slurp("/proc/sys/kernel/sched_bore"),
    }


def run_slot(arm, args=None, rep=0, label=None, scale=1.0, force=False, verbose=False):
    """Measure one arm for one repetition.

    Every failure path produces a row too. A failed arm is a *result* -- scx_cosmos is excluded
    from the comparison precisely because it cannot start here, and that fact is evidence, not
    an absence of it.
    """
    label = label or arm
    # Each arm gets its own label ("passA:cake"). perf-stats.py selects rows by label, so a
    # shared label would leave it unable to tell the arms apart when rank-testing. When a
    # configuration is supplied it joins the label too, otherwise the two bpfland variants in
    # Pass B would land in the same bucket and be silently pooled.
    label = f"{label}:{arm}" + (f"[{args}]" if args else "")
    note = f"{arm}{' ' + args if args else ''}"
    row = {"label": label, "rep": rep, "note": note, "arm": arm, "args": args,
           "valid": True, "invalid_reason": ""}

    # Always start from a known-healthy scheduler so a previous arm cannot leak into this one.
    if not recover():
        row.update(valid=False, invalid_reason="could not recover to a healthy cake before the slot")
        emit(row)
        return row

    if not force:
        quiet, idle_frac = quiesce()
        row["idle_frac_at_gate"] = idle_frac
        if not quiet:
            row.update(valid=False, invalid_reason=f"quiescence gate failed (idle {idle_frac})")
            emit(row)
            return row

    st = set_arm(arm, args, verbose=verbose)
    row["receipt"] = receipt(arm, args)
    row["set_arm"] = {k: st[k] for k in ("ok", "attach_ms", "ops", "state", "scxctl", "error",
                                         "nr_rejected")}
    if not st["ok"]:
        row.update(valid=False, invalid_reason="arm did not attach: " + st["error"])
        emit(row)
        recover()
        return row

    m = battery(scale=scale, verbose=verbose)

    # Post-hoc integrity: the arm must still be attached, and schbench must independently agree
    # about which scheduler it ran under. Either check failing voids the row.
    post_ops, post_state = ops_name(), sched_state()
    self_label = m.get("schbench_self", "")
    expect = "" if arm == "bore" else arm + "_"
    if arm == "bore":
        # The detached arm is only valid if nothing got attached behind our back.
        if post_ops:
            row.update(valid=False,
                       invalid_reason=f"BORE slot but a scheduler is attached ({post_ops!r})")
    else:
        if not post_ops.startswith(expect):
            row.update(valid=False,
                       invalid_reason=f"arm lost during slot: ops={post_ops!r}")
        elif post_state != "enabled":
            row.update(valid=False,
                       invalid_reason=f"sched_ext not enabled during slot: {post_state}")
        elif self_label and not self_label.startswith(expect):
            row.update(valid=False,
                       invalid_reason=f"schbench self-label disagrees: {self_label!r} vs {expect!r}")
    row["post"] = {"ops": post_ops, "state": post_state, "schbench_self": self_label,
                   "nr_rejected": nr_rejected()}

    row["cfg"] = dict(fslat.capture_settings())
    row["cfg"].update(freqs())
    row["cfg"]["arm"] = arm
    row["cfg"]["arm_args"] = args or ""

    # Hoist the fields perf-stats.py already understands, keep the rest grouped under "sched".
    row["wall_s"] = m.get("slot_wall_s", 0)
    row["rapl_J"] = m.get("slot_rapl_J", 0)
    row["avg_W"] = m.get("slot_avg_W", 0)
    row["psi_us"] = m.get("psi_us", {})
    row["vmstat"] = m.get("vmstat", {})
    row["sched"] = {k: v for k, v in m.items()
                    if k not in ("psi_us", "vmstat", "slot_wall_s", "slot_rapl_J", "slot_avg_W")}
    row["probe"] = {k: v for k, v in m.items() if k.startswith("launch")}

    recover()
    emit(row)
    if verbose:
        print(f"    slot done: valid={row['valid']} "
              f"bogo_rate={m.get('bogo_rate')} wk99={m.get('sb_wk_pct99_0')} "
              f"hb={m.get('hackbench_s')}", file=sys.stderr)
    return row


def emit(row):
    with open(RESULTS, "a") as fh:
        fh.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------- driver

def parse_arm_spec(spec):
    """'cosmos@-c 50' -> ('cosmos', '-c 50'); 'cake' -> ('cake', None)."""
    if "@" in spec:
        arm, extra = spec.split("@", 1)
        return arm.strip(), extra.strip()
    return spec.strip(), None


def cmd_run(args):
    arms = [a for a in args.arms.split(",") if a.strip()]
    print(f"=== scx A/B run: {len(arms)} arms x {args.reps} reps, scale={args.scale} ===")
    print(f"    arms: {', '.join(arms)}")
    print(f"    results -> {RESULTS}", flush=True)
    started = time.time()
    for rep in range(args.reps):
        # Rotate the starting arm each repetition. A fixed order would alias any slow thermal or
        # background drift onto whichever arm always goes first.
        k = rep % len(arms)
        order = arms[k:] + arms[:k]
        print(f"\n--- rep {rep}  order: {' -> '.join(order)}", flush=True)
        for spec in order:
            arm, extra = parse_arm_spec(spec)
            t0 = time.time()
            row = run_slot(arm, extra, rep=rep, label=args.label, scale=args.scale,
                           force=args.force, verbose=True)
            flag = "ok " if row["valid"] else "INVALID"
            print(f"    [{flag}] {arm:<12} {time.time() - t0:6.1f}s"
                  f"  {row.get('invalid_reason', '')}", flush=True)
    print(f"\n=== run complete in {(time.time() - started) / 60:.1f} min ===")
    return 0


def cmd_slot(args):
    arm, extra = parse_arm_spec(args.arm)
    if args.args:
        extra = args.args
    row = run_slot(arm, extra, rep=args.rep, label=args.label, scale=args.scale,
                   force=args.force, verbose=True)
    print(json.dumps({k: row[k] for k in ("arm", "valid", "invalid_reason", "set_arm", "post")
                      if k in row}, indent=2))
    return 0


def cmd_receipt(args):
    arms = ["bore"] + [a for a in ALL_ARMS if a != "bore"]
    out = {"kernel": os.uname().release, "cmdline": slurp("/proc/cmdline"),
           "arms": {a: receipt(a, None) for a in arms},
           "scx_loader_config": slurp("/etc/scx_loader.toml")}
    with open(RECEIPT, "w") as fh:
        json.dump(out, fh, indent=2)
    print(json.dumps(out, indent=2)[:4000])
    print(f"\nwrote {RECEIPT}")
    return 0


def restore_entry(entry_ops):
    """Put the live scheduler back the way we found it.

    Every command in this harness that attaches an arm is destructive to whatever was running, so
    each one has to be reversible. This records the entry state and restores it, because leaving a
    measurement tool's own scaffolding attached to a live desktop is not an acceptable side effect.
    """
    entry_arm = (entry_ops or "").split("_", 1)[0]
    if not entry_arm:
        # A kernel scheduler was live: nothing was attached, so detach again.
        run(["scxctl", "stop"])
        time.sleep(1)
        if ops_name():
            run(["systemctl", "stop", "scx_loader"], as_root=True)
            time.sleep(1)
        return not ops_name()
    if recover(entry_arm):
        return True
    # Could not name or reach it -- fall back to the loader's own configured default, which is
    # what survives a reboot anyway.
    run(["scxctl", "restore"])
    run(["systemctl", "restart", "scx_loader"], as_root=True)
    time.sleep(2)
    return ops_name().startswith(entry_arm + "_")


def cmd_arms(args):
    """Feasibility sweep: which arms can actually start here, and what each one reports.

    Restores whatever scheduler was attached on entry, so it is safe to run on a live machine.
    Writes no results.
    """
    entry_ops = ops_name()
    print(f"=== arm feasibility (entry: ops={entry_ops!r} state={sched_state()}) ===")
    try:
        for arm in ALL_ARMS:
            st = set_arm(arm, None, verbose=True)
            print(f"  {arm:<12} ok={str(st['ok']):<5} attach={st['attach_ms']:>5}ms "
                  f"ops={st['ops']!r} state={st['state']}")
            # A failed arm leaves sched_ext disabled and poisons the *next* one's reading, so the
            # machine has to be brought back to a healthy scheduler before each probe.
            recover()
    finally:
        back = restore_entry(entry_ops)
        print(f"=== restored: ops={ops_name()!r} ({'ok' if back else 'FAILED'}) ===")
    return 0 if back else 1


def cmd_status(args):
    print(json.dumps({
        "ops": ops_name(), "state": sched_state(), "nr_rejected": nr_rejected(),
        "freqs": freqs(),
        "scx_events": scx_events(),
    }, indent=2))
    return 0


def cmd_summary(args):
    """Quick per-arm median table. perf-stats.py does the rank testing; this is the eyeball pass."""
    if not os.path.exists(RESULTS):
        print("no results yet")
        return 1
    rows = []
    with open(RESULTS) as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if args.label:
        # Prefix match, because each arm's label is "<pass>:<arm>".
        rows = [r for r in rows if str(r.get("label", "")).startswith(args.label)]
    keys = ["sched.bogo_rate", "sched.sys_frac", "sched.hackbench_s", "sched.j_per_bogo",
            "sched.sb_wk_pct50_0", "sched.sb_wk_pct99_0", "sched.sb_wk_pct99_9",
            "sched.cyc_other_max", "sched.cyc_fifo_max", "sched.idle_J_per_s",
            "sched.light_J_per_s", "sched.bypass_ms", "sched.fast_excess",
            "sched.launch_under_load_ms", "sched.rps_rps_pct50_0"]

    def dig(row, path):
        cur = row
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur if isinstance(cur, (int, float)) and not isinstance(cur, bool) else None

    by = {}
    for r in rows:
        by.setdefault(r.get("label", "?"), {}).setdefault(r.get("arm", "?"), []).append(r)
    for label, arms in sorted(by.items()):
        print(f"\n=== {label}")
        hdr = f"{'metric':<26}" + "".join(f"{a:>14}" for a in sorted(arms))
        print(hdr)
        for k in keys:
            cells = []
            for a in sorted(arms):
                vals = [v for v in (dig(r, k) for r in arms[a]) if v is not None]
                cells.append(f"{statistics.median(vals):>14.4g}" if vals else f"{'-':>14}")
            if any(c.strip() != "-" for c in cells):
                print(f"{k.split('.', 1)[1]:<26}" + "".join(cells))
        inv = {a: sum(1 for r in arms[a] if not r.get("valid")) for a in sorted(arms)}
        print(f"{'invalid rows':<26}" + "".join(f"{inv[a]:>14}" for a in sorted(arms)))
    return 0


def main():
    ap = argparse.ArgumentParser(description="sched_ext scheduler A/B harness")
    sub = ap.add_subparsers(dest="mode", required=True)

    p = sub.add_parser("run", help="interleaved multi-arm run")
    p.add_argument("--reps", type=int, default=5)
    p.add_argument("--arms", default="bore,cake,bpfland,pandemonium")
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--label", default="passA")
    p.add_argument("--force", action="store_true", help="skip the quiescence gate")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("slot", help="measure a single arm once")
    p.add_argument("--arm", required=True)
    p.add_argument("--args", default="")
    p.add_argument("--rep", type=int, default=0)
    p.add_argument("--label", default="passA")
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_slot)

    p = sub.add_parser("receipt", help="write the run receipt")
    p.set_defaults(fn=cmd_receipt)

    p = sub.add_parser("arms", help="feasibility sweep over every arm")
    p.set_defaults(fn=cmd_arms)

    p = sub.add_parser("status", help="current scheduler state")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("summary", help="per-arm medians from the results file")
    p.add_argument("--label", default=None)
    p.set_defaults(fn=cmd_summary)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())






