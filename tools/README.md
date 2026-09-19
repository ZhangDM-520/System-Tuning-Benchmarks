# Tools

The measurement harness behind every study in this repository. Nothing here is a library — each tool
is a script that either **records rows** or **reads rows and answers a question about them**.

The analysis tools run against the checked-in data with no setup at all. The recording tools need
hardware, root, and about an hour.

---

## 1. Reading the data (no setup)

### `perf-stats.py` — is this difference real?

```bash
FSLAT_RESULTS=$PWD/data/tuning-results.jsonl python3 perf-stats.py show mem-swap-150 mem-swap-200
FSLAT_RESULTS=$PWD/data/tuning-results.jsonl python3 perf-stats.py cmp  mem-swap-150 mem-swap-200
FSLAT_RESULTS=$PWD/data/tuning-results.jsonl python3 perf-stats.py test mem-swap-150 mem-swap-200
```

| Mode | Output |
|---|---|
| `show LABEL…` | per-label noise band: `n`, min, median, max, spread% for every metric. **Use this first** — it establishes a control's own repeatability. |
| `cmp CONTROL LABEL…` | each treatment's median relative to control, as a percentage, with the control's spread printed alongside. An effect inside the control's spread is not a result. |
| `test CONTROL LABEL…` | tie-corrected Mann-Whitney U, p < 0.05. |

Metric paths are dotted into the row, so `xfs.log.0` is the count of forced XFS log commits and
`vmstat.kswapd_steal` is reclaim stealing. `perf-stats.py` without arguments prints the whole
default metric set.

`FSLAT_RESULTS` selects the row file (default `data/tuning-results.jsonl`). `perf-stats.py` is also
imported as a module by `scx_deep_analyse.py`, so the statistic is identical across the studies.

### `scx_deep_analyse.py` — the scheduler pass, per workload class

```bash
python3 scx_deep_analyse.py --passes deepA,deepA2           # all rows
python3 scx_deep_analyse.py --bl 0 --passes deepA,deepA2    # display-stable regime only
```

Per phase and per metric it prints each arm's median and a Mann-Whitney p against the control.
Options: `--bl 0|300000|any` (panel state), `--passes`, `--metric`, `--control`, `--exclude-reps`.

> **`rep 0` is excluded by default.** The rotation always places one arm first, and harness activity
> at launch lands in it — in both deep runs the first arm's idle energy read 88–124 J against a ~29 J
> baseline. See [`METHODOLOGY.md` §8.2](../METHODOLOGY.md#82-first-slot-contamination).

### `scx_deep.py trace` — inspect one slot's power trajectory

```bash
python3 scx_deep.py trace --file deepA2-bore-r1.jsonl
```

## 2. Recording rows

### Filesystem and general tuning

| Tool | Role | Needs root |
|---|---|---|
| `fslat.py` | the row recorder: per-run deltas of PSI stalls, RAPL energy, NVMe queue counters, XFS internal counters and reclaim stats, plus the live tunable snapshot | yes (RAPL, `drop_caches`) |
| `wl.sh` | named repeatable workloads: `gen`, `mktar`, `rm`, `rmpar`, `extract`, `mkstorm`, `coldmeta`, `coldread`, `coldapp`, `launchzen`, `launchunderload`, `wbig`, `pressure` | no |

```bash
sudo python3 fslat.py run --label myarm --wl wl.sh coldmeta
sudo python3 fslat.py settings          # print the tunable snapshot on its own
```

`fslat.py` appends to `$FSLAT_RESULTS`, default `data/tuning-results.jsonl`.

**Workload data goes to `$BENCH_SCRATCH`** (default `~/.cache/bench-scratch`), never inside the
repository. `wl.sh` guards every destructive path against that directory, so a mistyped argument
cannot delete anything else.

### Single-knob sweeps

| Tool | Sweeps |
|---|---|
| `ab-sweep.sh` | any one knob, with arms **interleaved inside every repetition** — the pattern the whole methodology rests on |
| `sysctl-sweep.sh` | one sysctl across several values, restoring the original afterwards |
| `ra-sweep.sh` | block read-ahead (`read_ahead_kb`) |
| `dw-sweep.sh` | dirty-writeback shaping, measured by browser-launch-under-write |
| `mem-combo.sh` | a **combination** of memory knobs as one arm (`min_free_kbytes` + `watermark_scale_factor`) |
| `band.sh` | the noise band: every instrument at its control setting, enough repetitions to state its spread |
| `mtu-sweep.sh`, `wifi-sweep.sh` | network MTU and wifi power save, measured end to end |
| `loopback-logbsize.sh` | proves on a throwaway loopback image how XFS treats `logbsize` **without touching the running root filesystem** |
| `logbsize-ab.sh` | the cache-dropped on-metal `logbsize` A/B |
| `verify-adopted-settings.sh` | reads every adopted setting back **from where its consumer reads it** — `/proc/mounts`, sysfs, `NetworkManager --print-config`, the device — and prints `PASS`/`FAIL`/`PEND` |

```bash
sudo ./ab-sweep.sh --key vm.swappiness --values 150 200 --wl 'wl.sh pressure'
sudo ./verify-adopted-settings.sh
```

### `sched_ext` schedulers

| Tool | Role |
|---|---|
| `scx_ab.py` | the A/B harness: arm switching with recovery, a quiescence gate, the workload battery, and validity gating. Subcommands: `run`, `slot`, `arms`, `status`, `summary`, `receipt` |
| `scx_deep.py` | the phase-structured harness: six workload classes × two cycles per slot, per-phase metrics, and a 1 Hz trace. Subcommands: `run`, `slot`, `summary`, `trace` |
| `scx_sampler.py` | the 1 Hz trace recorder: RAPL, panel backlight, aggregate `/proc/stat` |
| `scx_freq_probe.py` | the clock-distribution probe — the instrument that turned "uses less power" into a mechanism |
| `scx-relabel.py` | normalises result labels to `<pass>:<arm>[<args>]` (needed because `perf-stats.py` selects rows by label) |

```bash
sudo python3 scx_ab.py run --pass passA --reps 5 \
     --arms bore cake bpfland pandemonium --out ../data/scx-pass-ab.jsonl
sudo python3 scx_deep.py run --pass deepA --reps 4
sudo python3 scx_freq_probe.py 3
```

**Run the feasibility gate first.** `scx-w0-attach.sh` attaches each candidate in turn with default
flags, captures the scheduler's own startup banner (the only place a resolved CPU-domain mask is ever
printed), verifies it stayed attached, and restores the original scheduler on every exit path:

```bash
sudo ./scx-w0-attach.sh
sudo python3 scx_ab.py arms      # the same sweep from the Python side
```

**Any command that attaches an arm changes your live scheduler, so it restores what it found.**
`scx_ab.py arms`, `scx_ab.py run` and `scx_deep.py run` all record the scheduler attached on entry
and put it back on the way out. `recover()` resolves its target through scxctl's own `restore`, i.e.
**whatever your loader is configured to run at boot** — never a hardcoded arm name, so a crashed run
cannot leave you on a scheduler you never chose. Set `SCX_RESTORE_ARM=<arm>` to pin the recovery
target explicitly.

**A failed arm poisons the next one.** A scheduler that fails to start leaves
`/sys/kernel/sched_ext/state` at `disabled`, and the following arm then measures the kernel's own
scheduler instead. Recovery therefore runs *between* arms, not only at the end — which is why the
sweep above reports `cosmos ok=False` and still reports `pandemonium ok=True` immediately after it.

`scx-w0b/c/d-cosmos.sh` are the follow-up probes that characterised the `scx_cosmos` attach failure
in isolation, including the two harness bugs they exist to avoid (an "attached" verdict that only
checks *some* scheduler is loaded, and the `--args=` equals-form quoting requirement).

## 3. Operationally important, learned the hard way

These are recorded here because each one produced a wrong answer first:

1. **`scxctl start` refuses while a scheduler is attached.** Use `switch`. `start` only works from a
   detached state.
2. **`--args` needs the equals form**, `--args=-m performance`, or argument parsing consumes the
   leading `-`.
3. **`scxctl` exit codes are unreliable** — it has printed `error:` and returned `0`. Always verify
   through `/sys/kernel/sched_ext/root/ops` and `state`, never through the exit status.
4. **A failed scheduler leaves `state=disabled` and poisons the next arm**, which then measures the
   kernel's own scheduler. Recovery is mandatory between arms; `/sys/kernel/sched_ext/state` is
   read-only.
5. **`stress-ng --quiet` suppresses its `metrc:` lines**, so the throughput metric parses as zero and
   looks like a catastrophic regression. The harness now distinguishes "failed to parse" from "zero".
6. **`scaling_cur_freq` sampled once at the end of a slot is worthless** — one arm moved from 1712 to
   4469 MHz within a single slot.
7. **The panel backlight must be recorded per phase.** A display that blanks costs ~13 W, more than
   any effect measured here.
8. **Root is required for RAPL** (`/sys/class/powercap/intel-rapl:0/energy_uj`), for `drop_caches`,
   and for `scxctl`. The battery is not a substitute on a machine on AC with a charge cap.

## 4. Dependencies

| Tool | Needs |
|---|---|
| `perf-stats.py`, `scx_deep_analyse.py` | Python 3 standard library only |
| `scx_ab.py`, `scx_deep.py`, `scx_sampler.py`, `scx_freq_probe.py` | `schbench`, `stress-ng`, `rt-tests` (`cyclictest`), `hackbench`, `scxctl`/`scx_loader`, root |
| `fslat.py`, `wl.sh` | `nvme-cli`, `xfsprogs` (for `xfs_db` counters where available), `tar`, `dd`, root for RAPL and cache dropping |
| `wifi-sweep.sh`, `mtu-sweep.sh` | `ping`, `nmcli` |
| `loopback-logbsize.sh` | `xfsprogs`, `losetup`, root |

Nothing needs to be installed to **read or re-analyse** the checked-in data — only to record new rows.

## 5. Conventions

- **Environment overrides:** `BENCH_ROOT` (repository root), `BENCH_SCRATCH` (generated workload
  data), `BENCH_OUT` (probe receipts), `FSLAT_RESULTS`, `SCX_AB_RESULTS`, `SCX_DEEP_RESULTS`,
  `SCX_FREQ_PROBE_OUT`, `SCX_RESTORE_ARM` (pin the scheduler that recovery restores).
- **Defaults resolve relative to the repository**, so a fresh clone works with no configuration.
- **Row files are append-only during a run** and are never rewritten by the analysis tools.
  `scx-relabel.py` is the one exception and therefore requires an explicit path.
