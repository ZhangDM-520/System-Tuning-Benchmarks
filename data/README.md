# Data dictionary

Everything here is raw output from the harness in [`../tools`](../tools). Nothing is hand-edited, and
every figure in every study was produced by running the analysers in this repository over these files
— see **Regenerating the analysis** below.

Total: **2.3 MB**, 44 files.

---

## 1. Row files

All row files are **JSON Lines**: one JSON object per line, one object per measured slot. Every row
is self-describing — it carries the tunables that were live when it was recorded — so a row can be
interpreted years later without the surrounding context.

| File | Rows | Invalid | Study | Contents |
|---|---|---|---|---|
| `scx-pass-ab.jsonl` | 38 | 0 | [01](../studies/01-scheduler-selection/README.md) | scheduler Pass A (20 rows, shipped defaults) and Pass B (18 rows, designed configurations) |
| `scx-deep.jsonl` | 288 | 0 | [01](../studies/01-scheduler-selection/README.md) | the phase-structured pass: 3 arms × 4 repetitions × 12 phases × 2 passes |
| `scx-freq-probe.jsonl` | 12 | 0 | [01](../studies/01-scheduler-selection/README.md) | per-window CPU clock distributions |
| `tuning-results.jsonl` | 315 | 0 | [02](../studies/02-boot-and-power/README.md), [03](../studies/03-filesystem-latency/README.md) | every lever arm from both campaigns, in one file |
| `tuning-results-pooled.jsonl` | 157 | 0 | [02](../studies/02-boot-and-power/README.md) | pooled repetitions used to build n = 12 on the memory-pressure levers |

**"Invalid: 0" is a result, not a formality.** A row is marked invalid when the arm's identity could
not be confirmed from **two independent sources** — see
[`METHODOLOGY.md` §3](../METHODOLOGY.md#3-arm-identity-verification-do-not-skip-this). The
`valid` and `invalid_reason` fields are always present, and an invalid row is kept rather than
deleted, so the exclusion is auditable.

### 1.1 Scheduler rows (`scx-*.jsonl`)

| Field | Meaning |
|---|---|
| `label` | `<pass>:<arm>` — the unit `perf-stats.py` selects on, e.g. `passA:cake` |
| `arm` | scheduler name: `bore`, `cake`, `bpfland`, `pandemonium`, `cosmos` |
| `args` | extra CLI flags, or `null`. **Part of the arm identity** — `pandemonium` and `pandemonium --no-adaptive` share an `arm` and differ only here |
| `pass` | run identifier (`deepA`, `deepA2`). Used to keep passes separate, because a regime change between passes makes them incomparable |
| `rep` | repetition index. **`rep 0` is excluded by the analyser by default** |
| `phase` / `cycle` | deep pass only: workload class and which of the two cycles per slot |
| `mode` | `phase` or `alt` |
| `wall_s` | slot wall time |
| `rapl_J`, `avg_W` | package energy and mean power for the slot |
| `psi_us` | pressure-stall counters (I/O, memory, CPU), in µs |
| `vmstat` | reclaim counters: `kswapd`/direct scan and steal, `pswpout`, major faults |
| `sched` | the scheduler battery: `hackbench_s`, `bogo_rate`, `j_per_bogo`, `sb_wk_pct50_0`/`pct99_0`/`pct99_9`/`max` (schbench wakeup percentiles), `cyc_other_*`/`cyc_fifo_*` (cyclictest), `fast_ticks`/`dense_ticks`/`fast_share`/`fast_excess` (hybrid placement), `launch_under_load_ms` |
| `sched.phase_J` / `phase_W` / `phase_wall_s` | deep pass only: the energy, mean power and duration **of that phase alone** — the numbers study 01 §6.1 compares |
| `sched.bl_before` / `bl_after` / `bl_stable` | deep pass only: the panel backlight level either side of the phase, and whether it moved. A display flip costs ~13 W, so `bl_stable: false` rows are filtered out by `--bl 0` |
| `set_arm` / `post` | the arm read-back before and after the slot: `ops`, `state`, `nr_rejected`, and the workload's own `sched_ext` self-report |
| `receipt` | **identity proof**: `binary_sha256`, `kernel`, `sched_bore`, `args`, `arm`, `ops_at_entry` |
| `cfg` | the live tunable snapshot (§2) |

`fast_excess` deserves its own line: it is the fraction of ticks that landed on the fast cluster
divided by its pro-rata share, computed from per-CPU tick deltas. **1.0 means exactly pro-rata.** It
exists because `cpu_capacity` is flat on this SoC and cannot be trusted — see
[`TEST-BED.md` §1.1](../TEST-BED.md#11-the-hybrid-split--and-the-trap-in-it).

### 1.2 Frequency probe rows (`scx-freq-probe.jsonl`)

`window` (`idle` / `mem`), `arm`, `rep`, and the clock distribution for that window:
`freq_mean_mhz`, `freq_min_mhz`, `freq_max_mhz`, `freq_n`, plus `W`/`J`. **The `W`/`J` fields are
zero here** — this probe ran unprivileged and RAPL is root-only. All energy figures in the studies
come from the row files, never from this one.

### 1.3 Lever rows (`tuning-results*.jsonl`)

| Field | Meaning |
|---|---|
| `label` | `<arm>` or `<arm>-<value>`, e.g. `mem-wmark-10`, `wbt-0`, `logbsize256-mkstorm15` |
| `rep` | repetition index |
| `cmd` | the exact command that produced the row |
| `note` | free-text description of what the arm is |
| `wall_s`, `rapl_J`, `rapl_core_J`, `avg_W`, `bat_uWh` | time and energy (`bat_uWh` is unusable on this machine — AC with a charge cap) |
| `psi_us` | pressure-stall counters |
| `vmstat` | reclaim counters |
| `nvme` | per-run deltas of NVMe queue counters: reads/writes/flushes, `io_ticks` (device busy), `time_in_queue` (used for mean request latency), merges |
| `xfs` | **flat dotted keys**, e.g. `xfs.log.0` — which for this kernel is the count of **forced log commits**, the direct measure of synchronous metadata round trips. `xfs.log.1` is log blocks |
| `zram` | swap-in/out counters |
| `mem_kB` | memory state at record time |
| `cfg` | the live tunable snapshot (§2) |

## 2. The tunable snapshot (`cfg`)

Present in every scheduler and lever row. This is the field that surfaced the dead `logbsize` setting,
and it is the reason a row can be re-interpreted later:

```
fs.xfs.xfssyncd_centisecs   fs.xfs.speculative_prealloc_lifetime
vm.dirty_background_ratio   vm.dirty_ratio
vm.dirty_background_bytes   vm.dirty_bytes
vm.dirty_writeback_centisecs  vm.dirty_expire_centisecs
vm.swappiness               vm.vfs_cache_pressure
vm.page-cluster             queue.read_ahead_kb
queue.nr_requests           queue.scheduler
mount.logbufs               mount.logbsize
scx                         scx_proc
mount_options
```

The **scheduler** rows carry five extra fields, because the clock and power state are the mechanism
under test: `freq_fast_mhz`, `freq_dense_mhz` (per-cluster mean clock for the slot), `epp`,
`governor` and `platform_profile`.

**Read it as "what the kernel had at that moment", not "what a config file said".** In the recorded
defaults `mount.logbsize` is `32k` while `/etc/fstab` said `256k` — the discrepancy is the finding in
[study 03 §3](../studies/03-filesystem-latency/README.md#3-the-one-real-finding-a-setting-that-was-never-in-effect).

## 3. Traces (`traces/`, 24 files)

`<pass>-<arm>[-<args>]-r<rep>.jsonl`, one per deep-pass slot: a **1 Hz** trajectory of package RAPL
energy, the panel backlight level, and aggregate `/proc/stat`. Produced by
`tools/scx_sampler.py`, which runs as root because RAPL is root-only.

The backlight field is not decoration: the panel blanking mid-run costs ~13 W and voided one
comparison outright. Every phase records it on both sides so the regime can be filtered
(`scx_deep_analyse.py --bl 0`). Inspect a trace with:

```bash
python3 tools/scx_deep.py trace --file deepA2-bore-r1.jsonl
```

## 4. Logs (`logs/`, 7 files)

| File | Contents |
|---|---|
| `scx-passA.log`, `scx-passB.log` | per-slot validity and headline metrics, plus total run time |
| `scx-deepA.log`, `scx-deepA2.log` | per-slot arm and duration |
| `baseline.log`, `band.log`, `after.log` | the filesystem/boot campaigns' before, during and after runs, including the workload-comparison tables quoted in study 02 |

## 5. Probes and receipts (`probes/`, 8 files)

| File | Contents |
|---|---|
| `scx-w0-attach.txt` | the attach feasibility gate: each candidate scheduler attached and verified, the step that caught `scx_cosmos` before it could pollute a pass |
| `scx-w0b/c/d-cosmos.txt` | three further `scx_cosmos` configurations, all failing identically — the evidence behind study 01 §10 |
| `scx-pandemonium-verbose.txt` | the raw `-v` telemetry, with the adaptive and `--no-adaptive` runs side by side — the evidence behind study 01 §9 |
| `post-reboot-verify-*.txt` | the settings read back from where their consumers read them, after the reboot that activated the boot-path changes |
| `boottime-*.txt`, `blame-*.txt` | `systemd-analyze` output for the boot-time result |

## 6. Regenerating the analysis

These files are checked in so the analysis is reproducible without re-measuring anything. To regenerate
`analysis/` from the row files:

```bash
python3 tools/scx_deep_analyse.py --passes deepA,deepA2 \
    > data/analysis/deep-all.txt
python3 tools/scx_deep_analyse.py --bl 0 --passes deepA,deepA2 \
    > data/analysis/deep-display-stable.txt

FSLAT_RESULTS=$PWD/data/scx-pass-ab.jsonl \
  python3 tools/perf-stats.py cmp passA:cake passA:bore passA:bpfland passA:pandemonium \
    > data/analysis/passA-vs-cake.txt

FSLAT_RESULTS=$PWD/data/scx-pass-ab.jsonl \
  python3 tools/perf-stats.py cmp passB:cake passB:bore passB:pandemonium \
      'passB:pandemonium[--no-adaptive]' \
    > data/analysis/passB-vs-cake.txt
```

`FSLAT_RESULTS` and the `SCX_*_RESULTS` environment variables override the default data paths; the
defaults resolve to `data/` relative to the repository root.

## 7. Invariants worth asserting on any file here

- Every row has a `valid` field, and `invalid_reason` explains any `false`.
- `rep 0` exists in every arm and must be **excluded** for the scheduler passes — see
  [`METHODOLOGY.md` §8.2](../METHODOLOGY.md#82-first-slot-contamination).
- `mount.logbsize` reads `32k` in the **filesystem/boot** rows (recorded before the change — that
  discrepancy *is* the finding in study 03 §3) and `256k` in the **scheduler** rows, which were
  recorded after the reboot that made it live. The field is provenance, so it is deliberately never
  rewritten.
- No row contains a hostname, an IP address, a MAC address or a personal filesystem path.