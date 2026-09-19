#!/usr/bin/env python3
"""fslat -- zero-install filesystem latency / energy harness.

For one workload invocation it reports, as deltas across the run:

  wall      elapsed seconds, plus average watts implied by RAPL energy
  PSI       /proc/pressure/{io,memory,cpu} stall microseconds.  The `full`
            io total is "every task stalled on I/O" -- the most direct
            available statement of "the system stalled", and it needs no
            packages installed.
  nvme      device-level ops, sectors, busy ms (io_ticks) and queue ms
            (time_in_queue), plus FLUSH count.  time_in_queue/ops is the
            mean per-request queueing+service delay as the block layer sees it.
  xfs       XFS internal counters.  `log.force` is the count of forced log
            commits -- the direct measure of how often metadata had to hit
            the drive synchronously.
  energy    RAPL package microjoules (wrap-safe) and battery energy delta.
  reclaim   vmstat reclaim/writeback counters and zram compressed bytes.

Every run also records the settings actually in force, so a result row is
self-describing and cannot be misattributed to the wrong configuration.

Usage:
    fslat.py settings
    fslat.py run --label NAME [--drop] [--repeat N] [--note TEXT] -- CMD ...
"""

from __future__ import annotations

import argparse
import json
import os
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

RESULTS = os.environ.get("FSLAT_RESULTS") or os.path.join(DATA, "tuning-results.jsonl")
DEV = "nvme0n1"
XFS_STATS = "/sys/fs/xfs/nvme0n1p2/stats/stats"

# Root-only sources, read through a single sudo call with markers.
ROOT_SOURCES = [
    XFS_STATS,
    "/sys/class/powercap/intel-rapl:0/energy_uj",
    "/sys/class/powercap/intel-rapl:0/max_energy_range_uj",
    "/sys/class/powercap/intel-rapl:0:0/energy_uj",
    "/sys/class/powercap/intel-rapl:0:0/max_energy_range_uj",
]

NVME_FIELDS = [
    "read_ios", "read_merges", "read_sectors", "read_ticks",
    "write_ios", "write_merges", "write_sectors", "write_ticks",
    "in_flight", "io_ticks", "time_in_queue",
    "discard_ios", "discard_merges", "discard_sectors", "discard_ticks",
    "flush_ios", "flush_ticks",
]

VMSTAT_FIELDS = [
    "pgmajfault", "pgscan_direct", "pgscan_kswapd", "pgsteal_direct",
    "pgsteal_kswapd", "pswpin", "pswpout", "nr_dirty", "nr_writeback",
    "nr_dirtied", "nr_written", "nr_free_pages",
]

SETTING_PATHS = {
    "fs.xfs.xfssyncd_centisecs": "/proc/sys/fs/xfs/xfssyncd_centisecs",
    "fs.xfs.speculative_prealloc_lifetime": "/proc/sys/fs/xfs/speculative_prealloc_lifetime",
    "vm.dirty_background_ratio": "/proc/sys/vm/dirty_background_ratio",
    "vm.dirty_ratio": "/proc/sys/vm/dirty_ratio",
    "vm.dirty_background_bytes": "/proc/sys/vm/dirty_background_bytes",
    "vm.dirty_bytes": "/proc/sys/vm/dirty_bytes",
    "vm.dirty_writeback_centisecs": "/proc/sys/vm/dirty_writeback_centisecs",
    "vm.dirty_expire_centisecs": "/proc/sys/vm/dirty_expire_centisecs",
    "vm.swappiness": "/proc/sys/vm/swappiness",
    "vm.vfs_cache_pressure": "/proc/sys/vm/vfs_cache_pressure",
    "vm.page-cluster": "/proc/sys/vm/page-cluster",
    "queue.read_ahead_kb": f"/sys/block/{DEV}/queue/read_ahead_kb",
    "queue.nr_requests": f"/sys/block/{DEV}/queue/nr_requests",
    "queue.scheduler": f"/sys/block/{DEV}/queue/scheduler",
}


def slurp(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def number(path: str) -> int:
    txt = slurp(path).split()
    return int(txt[0]) if txt and txt[0].lstrip("-").isdigit() else 0


def root_read(paths: list[str]) -> dict[str, str]:
    """Read root-only files in one sudo invocation, delimited by markers."""
    script = 'for f in "$@"; do printf "@@@%s\\n" "$f"; cat "$f" 2>/dev/null; done'
    proc = subprocess.run(
        ["sudo", "-n", "sh", "-c", script, "_", *paths],
        capture_output=True, text=True,
    )
    out: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in proc.stdout.splitlines():
        if line.startswith("@@@"):
            if current is not None:
                out[current] = "\n".join(buf).strip()
            current, buf = line[3:], []
        else:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf).strip()
    return out


def parse_xfs(text: str) -> dict[str, int]:
    """XFS stats lines are `name v1 v2 ...`.  Keep them addressable by index."""
    stats: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        for i, raw in enumerate(parts[1:]):
            if raw.lstrip("-").isdigit():
                stats[f"{parts[0]}.{i}"] = int(raw)
    return stats


def parse_pressure(path: str) -> dict[str, int]:
    """`some avg10=.. avg60=.. avg300=.. total=123` -> {'some': 123, 'full': 123}."""
    out: dict[str, int] = {}
    for line in slurp(path).splitlines():
        parts = line.split()
        if not parts:
            continue
        total = 0
        for kv in parts[1:]:
            if kv.startswith("total="):
                total = int(kv.split("=", 1)[1])
        out[parts[0]] = total
    return out


def snapshot() -> dict:
    """Capture every counter we compare before/after a workload."""
    snap: dict = {"t": time.time()}

    snap["nvme"] = dict(zip(NVME_FIELDS, (int(x) for x in slurp(f"/sys/block/{DEV}/stat").split())))

    root = root_read(ROOT_SOURCES)
    snap["xfs"] = parse_xfs(root.get(XFS_STATS, ""))
    for key, name in (("rapl", "intel-rapl:0"), ("rapl_core", "intel-rapl:0:0")):
        base = f"/sys/class/powercap/{name}"
        snap[key] = int(root.get(f"{base}/energy_uj", "0") or 0)
        snap[key + "_range"] = int(root.get(f"{base}/max_energy_range_uj", "0") or 0)

    for domain in ("io", "memory", "cpu"):
        snap[f"psi_{domain}"] = parse_pressure(f"/proc/pressure/{domain}")

    snap["vmstat"] = {}
    vmtext = slurp("/proc/vmstat")
    for line in vmtext.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in VMSTAT_FIELDS:
            snap["vmstat"][parts[0]] = int(parts[1])

    mem = {}
    for line in slurp("/proc/meminfo").splitlines():
        parts = line.replace(":", "").split()
        if len(parts) >= 2:
            mem[parts[0]] = int(parts[1])
    snap["mem"] = {k: mem.get(k, 0) for k in
                   ("MemAvailable", "Cached", "Dirty", "Writeback", "AnonPages", "SwapFree")}

    snap["zram"] = [int(x) for x in slurp("/sys/block/zram0/mm_stat").split()]
    snap["bat_energy"] = number("/sys/class/power_supply/BAT0/energy_now")
    snap["bat_status"] = slurp("/sys/class/power_supply/BAT0/status")
    return snap


def wrap_delta(after: int, before: int, rng: int) -> int:
    """RAPL energy is a free-running counter; honour its wrap point."""
    if rng > 0 and after < before:
        return after + rng - before
    return after - before


def diff(before: dict, after: dict, wall: float) -> dict:
    nv = {}
    for f in NVME_FIELDS:
        nv[f] = after["nvme"].get(f, 0) - before["nvme"].get(f, 0)
    ops = nv["read_ios"] + nv["write_ios"]
    # time_in_queue is aggregated milliseconds across requests -> per-request us
    nv["mean_queue_us"] = round(nv["time_in_queue"] * 1000 / ops, 1) if ops else 0

    xs = {}
    for key in set(before["xfs"]) | set(after["xfs"]):
        d = after["xfs"].get(key, 0) - before["xfs"].get(key, 0)
        if d:
            xs[key] = d

    psi = {}
    for domain in ("io", "memory", "cpu"):
        for kind in ("some", "full"):
            psi[f"{domain}_{kind}"] = (
                after[f"psi_{domain}"].get(kind, 0) - before[f"psi_{domain}"].get(kind, 0))

    j = wrap_delta(after["rapl"], before["rapl"], after["rapl_range"]) / 1e6
    jc = wrap_delta(after["rapl_core"], before["rapl_core"], after["rapl_core_range"]) / 1e6
    bat = abs(after["bat_energy"] - before["bat_energy"])

    vm = {f: after["vmstat"].get(f, 0) - before["vmstat"].get(f, 0) for f in VMSTAT_FIELDS}
    mem = {k: after["mem"][k] - before["mem"][k] for k in before["mem"]}
    zram = [a - b for a, b in zip(after["zram"], before["zram"])]

    return {
        "wall_s": round(wall, 3),
        "rapl_J": round(j, 3),
        "rapl_core_J": round(jc, 3),
        "avg_W": round(j / wall, 2) if wall > 0 else 0,
        "bat_uWh": bat,
        "psi_us": psi,
        "nvme": nv,
        "xfs": xs,
        "vmstat": vm,
        "mem_kB": mem,
        "zram": zram,
    }


def capture_settings() -> dict:
    cfg = {}
    for name, path in SETTING_PATHS.items():
        val = slurp(path)
        if name == "queue.scheduler":
            val = val.strip("[]") if "[" not in val else val.split("[")[1].split("]")[0]
        cfg[name] = val
    # First-mount-only XFS log geometry: only visible via the live mount.
    for line in slurp("/proc/mounts").splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[1] == "/":
            for opt in fields[3].split(","):
                if opt.startswith(("logbsize=", "logbufs=", "allocsize=")):
                    cfg["mount." + opt.split("=")[0]] = opt
    cfg["scx"] = slurp("/sys/kernel/sched_ext/state")
    cfg["scx_proc"] = subprocess.run(
        ["sh", "-c", "ps -eo comm | grep -E '^scx_' | grep -v -e scx_loader -e helper || true"],
        capture_output=True, text=True).stdout.strip()
    cfg["mount_options"] = next(
        (l.split()[3] for l in slurp("/proc/mounts").splitlines()
         if len(l.split()) >= 4 and l.split()[1] == "/"), "")
    return cfg


def drop_caches() -> None:
    subprocess.run(["sudo", "-n", "sh", "-c",
                    "sync; echo 3 > /proc/sys/vm/drop_caches"],
                   check=True, capture_output=True)


def parse_probes(text: str) -> dict:
    """Workloads emit `PROBE key=value` on stderr; those are metrics too.

    The launch workloads print their own end-to-end latency this way, because
    that number cannot be derived from /proc deltas.  Capturing it here keeps
    every reported metric in one row.
    """
    out: dict = {}
    for line in text.splitlines():
        if not line.startswith("PROBE "):
            continue
        for kv in line.split()[1:]:
            if "=" not in kv:
                continue
            key, val = kv.split("=", 1)
            out[key] = int(val) if val.lstrip("-").isdigit() else val
    return out


def report(label: str, note: str, result: dict, cfg: dict, cmd: str, rep: int,
           probes: dict | None = None) -> None:
    psi, nv, xs = result["psi_us"], result["nvme"], result["xfs"]
    print(f"\n=== {label}  rep{rep}  wall={result['wall_s']}s  "
          f"{result['rapl_J']}J pkg ({result['avg_W']}W)  bat={result['bat_uWh']}uWh")
    print(f"    PSI        io some={psi['io_some']/1000:.0f}ms full={psi['io_full']/1000:.0f}ms"
          f" | mem full={psi['memory_full']/1000:.0f}ms | cpu some={psi['cpu_some']/1000:.0f}ms")
    ops = nv["read_ios"] + nv["write_ios"]
    print(f"    nvme       r={nv['read_ios']} w={nv['write_ios']} flush={nv['flush_ios']}"
          f" sectors r={nv['read_sectors']} w={nv['write_sectors']}"
          f" busy={nv['io_ticks']}ms qmean={nv['mean_queue_us']}us")
    log = {k: v for k, v in xs.items() if k.startswith(("log.", "trans.", "push_ail."))}
    print(f"    xfs        {json.dumps(log, sort_keys=True)}")
    zr = result["zram"]
    print(f"    reclaim    majflt={result['vmstat']['pgmajfault']}"
          f" direct_scan={result['vmstat']['pgscan_direct']}"
          f" direct_steal={result['vmstat']['pgsteal_direct']}"
          f" pswpout={result['vmstat']['pswpout']}"
          f" zram_orig={zr[0] if zr else 'n/a'} zram_compr={zr[1] if len(zr) > 1 else 'n/a'}"
          f" zram_mem={zr[2] if len(zr) > 2 else 'n/a'}")
    if probes:
        print(f"    probe      {json.dumps(probes, sort_keys=True)}")
    row = {"label": label, "rep": rep, "note": note, "cmd": cmd,
           "cfg": cfg, "probe": probes or {}, **result}
    with open(RESULTS, "a") as fh:
        fh.write(json.dumps(row) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)

    sub.add_parser("settings")

    run = sub.add_parser("run")
    run.add_argument("--label", required=True)
    run.add_argument("--note", default="")
    run.add_argument("--drop", action="store_true",
                     help="drop caches (pagecache+dentries+inodes) before each repetition")
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--pre", default="",
                     help="command run UNTIMED before each repetition (regenerate test data)")
    run.add_argument("--sleep", type=float, default=0.0,
                     help="settle seconds between repetitions, to let PSI/energy settle")
    run.add_argument("cmd", nargs=argparse.REMAINDER)

    args = ap.parse_args()

    if args.mode == "settings":
        print(json.dumps(capture_settings(), indent=2, sort_keys=True))
        return 0

    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if not cmd:
        print("no command given", file=sys.stderr)
        return 2

    cfg = capture_settings()
    for rep in range(1, args.repeat + 1):
        if args.pre:
            subprocess.run(args.pre, shell=True, check=False)
        if args.drop:
            drop_caches()
        if args.sleep:
            time.sleep(args.sleep)
        before = snapshot()
        t0 = time.monotonic()
        with tempfile.TemporaryFile() as errf:
            proc = subprocess.run(cmd, stderr=errf)
            errf.seek(0)
            err_txt = errf.read().decode("utf-8", "replace")
        wall = time.monotonic() - t0
        sys.stderr.write(err_txt)
        after = snapshot()
        report(args.label, args.note, diff(before, after, wall), cfg,
               " ".join(cmd), rep, parse_probes(err_txt))
        if proc.returncode != 0:
            print(f"    !! workload exited {proc.returncode}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
