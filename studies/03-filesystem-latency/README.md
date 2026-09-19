# 03 — Squeezing filesystem latency without spending power

**Question.** Can the filesystem latency of an interactive XFS-on-NVMe laptop be reduced — `rm -r` of
scattered trees, browser launch, cold metadata walks — **without** increasing power draw?

**Answer, in one line.** Almost every lever that was tried made no difference or made things worse;
one setting was found to be **silently dead and was made live**; and the two symptoms that motivated
the work turned out to be **bounded by things no filesystem tuning can reach** — a serial metadata
dependency chain, and application startup time.

This study is published mainly for its **negative results** and for the two bounds. Knowing that
read-ahead, queue depth, dirty-writeback caps and parallel deletion are all dead ends on this kind of
workload is worth more than the one change that was made.

---

## 1. Headline results

| Result | Status |
|---|---|
| `logbsize=256k` was configured in `/etc/fstab` and **had never been applied** | **diagnosed and fixed** — but via `rootflags=`, and the end-to-end *benefit* was not established (§3) |
| Forced XFS log commits for the same 15× metadata storm | **19034 → ~1561** — the mechanism, confirmed |
| `rm -r` of a scattered tree is bound by **serial round trips at queue depth ≈ 1**, and the device is only **20–55% busy** | bound, with numbers (§5) |
| A cold browser launch is **~70–90 ms of filesystem time out of ~530 ms**; a warm launch touches the device for **3 ms** | bound, with numbers (§6) |
| `read_ahead_kb`, `nr_requests`, `xfssyncd_centisecs`, dirty-writeback caps, `vm.swappiness`, parallel deletion | **all refused by measurement** (§4) |

## 2. Method

The same protocol as [`METHODOLOGY.md`](../../METHODOLOGY.md). Two details are specific to this study:

- **Zero installs.** The brief was to measure with the tools already present. The harness is
  `tools/fslat.py` (per-run deltas of PSI stall time, RAPL energy, NVMe queue latency, XFS internal
  counters and reclaim statistics) plus `tools/wl.sh` (named repeatable workloads). Every row carries
  a **self-describing snapshot of the live tunables**, which is what made §3 visible.
- **Cache state is part of the arm.** A "cold" workload drops caches first; a "warm" one does not.
  Mixing the two is a measurement error that this study made once and caught (§3).

## 3. The one real finding: a setting that was never in effect

`/etc/fstab` carried `logbsize=256k`. The live value was `logbsize=32k`. **Both were true**, and the
reason is documented XFS behaviour that is easy to miss:

> **XFS chooses its log geometry at first mount and never changes it afterwards.**

Two consequences follow, and both were verified rather than assumed:

1. **`mount -o remount,logbsize=64k` returns exit status 0 and silently does nothing.** Verified on a
   throwaway loopback XFS with the same feature set as the root filesystem — so the failure is silent
   by construction, not by accident.
2. **The fstab entry could never work**, because the initramfs mounts root *before* `/etc/fstab` is
   consulted. The only route is the kernel command line, which the initramfs passes straight through:
   `mount -t "${rootfstype:-auto}" -o "${rwopt:-ro}${rootflags:+,$rootflags}" …`. So
   `rootflags=logbsize=256k` lands, and it is now on the command line.

The **mechanism is confirmed**: for the same 15× metadata storm, the count of forced log commits (the
direct measure of synchronous metadata round trips) falls from **19034 to ~1561** — a 12× reduction —
with log blocks dropping correspondingly.

**The end-to-end benefit is NOT established, and this study will not claim it.** The comparison row at
32k was recorded **without dropping caches**, so it measured page cache rather than log geometry: its
device reads were 2,070 warm against 32,649 cold in the 256k arm. That is not a like-for-like
comparison and it was rejected as evidence. The on-metal A/B is half complete — the 256k arm is
captured, and the 32k arm requires a reboot to restore the old geometry.

So the honest statement is:

> A silently-dead setting was identified and made live, and the mechanism by which it acts was
> measured. **Whether it makes anything faster on this machine remains unproven**, because the A/B
> that would settle it is incomplete.

This is the most transferable lesson in the study: **reading the configuration file tells you what
was asked for, not what is in effect.** The self-describing tunable snapshot is what surfaced it, and
`/proc/mounts` is where it was confirmed.

## 4. Refused by measurement

Every lever below was implemented, measured against an interleaved control, and reverted.

| Lever | Test | Result |
|---|---|---|
| `read_ahead_kb` 256 → 2048 | 1M-file cold metadata walk | **Identical at every value tested** — 64.5k reads at 95 µs each. XFS metadata reads use fixed buffer sizes and ignore device read-ahead, so the knob has nothing to act on. |
| `nr_requests` 1023 → 256 | 8 GiB write | 3.90 s vs 3.75 s and queue latency 2404 µs vs 1978 µs — **worse**. |
| `fs.xfs.xfssyncd_centisecs` 10000 → 1000 | 15× metadata storm (1.2M log blocks) | 8.35 s vs 8.16 s, log forces 135 vs 191, PSI I/O-full 19 ms vs 12 ms. No latency benefit, **more wakeups** — kept at 10000. |
| `vm.swappiness` 150 vs 195 | 8 GiB anonymous balloon, then cold launch | 395/444 ms vs 389/414 ms — no difference. Launches read ~0 bytes from disk, so nothing was evicted: **swappiness cannot matter when there is no pressure to tune against.** |
| Dirty-writeback caps 128M/512M and 256M/1G with expire 1500 | 8 GiB write + browser launch | PSI I/O-full 46/68 ms and 75/43 ms against a baseline of 83/69 ms — within noise. Probe 389–491 ms across every configuration, same as unloaded. Kept the ratio defaults. |
| Parallel `rm -rf` (`xargs -P8`) | 24k cold delete | 0.66–0.72 s and 7.1–7.5 J against 0.48–0.49 s and 4.5 J serial — **worse on both time and energy**. |

The `swappiness` row deserves emphasis, because it connects to
[study 02](../02-boot-and-power/README.md): swappiness only matters *under memory pressure*. That
study later produced pressure deliberately, and found 150 vs 200 there is dramatic (stalls +1986%,
energy +39%, p = 0.004) while 150 vs 195 *without* pressure is indistinguishable. **A null result on
a lever whose precondition was absent is not evidence that the lever does nothing** — it is evidence
that the experiment did not test the lever.

## 5. Bound 1 — `rm -r` is a serial chain, not a bandwidth problem

The original symptom was that deleting a large scattered tree felt very slow. It is slow, and the
reason is not the drive:

| workload | wall | device reads | device busy | per-request | log items |
|---|---|---|---|---|---|
| 24k files, cold `rm -r` | 0.48–0.57 s | 2100–3400 | 140–277 ms | 72–78 µs | ~169k |
| 64k files, cold `rm -r` | 1.22–1.30 s | 4400–4528 | 250 ms | 72 µs | 452k |
| 1M files (real tree), cold metadata walk | 10.87 s | 64,527 | 5940 ms | 95 µs | — |

**The device is only 20–55% busy.** At 72–95 µs per request and a busy fraction below half, these
operations are bound by **serial round trips at queue depth ≈ 1** — `rm` and `find` are
single-threaded, and XFS metadata reads are fixed-size, so neither read-ahead nor queue depth changes
anything. That also explains the parallel-deletion result in §4: the work is *serial*, so parallelising
it adds scheduling and energy cost without shortening the chain.

Extrapolating the per-request latency, a 1M-file cold delete is on the order of **20–40 s of mostly
serial metadata work**. No sysctl makes that fast; only a warmer cache does, which is a different
problem.

## 6. Bound 2 — the browser launch is mostly not the filesystem

The other symptom was a slow browser launch. Decomposed:

| state | wall |
|---|---|
| warm (no cache drop) | 0.42–0.47 s |
| cold (cache dropped) | 0.51–0.56 s |
| cold, with an 8 GiB write in flight | 0.44–0.49 s |

**The filesystem accounts for only ~70–90 ms of the ~530 ms launch.** In a warm launch the process
touches the device for **3 ms**; the remaining ~440 ms is application process and JavaScript startup.
A write in flight does not lengthen the launch at all.

Two consequences:

- **The best case is bounded at ~0.42 s** — the warm floor. No filesystem tuning can move a number
  whose removable part is already down to tens of milliseconds.
- **The observed "slowness" was not filesystem-bound**, and the honest answer to the original question
  is that this particular symptom is out of scope for filesystem tuning. That is a useful result even
  though it is a null one.

## 7. Deliberately not changed

`allocsize` · `noatime` (`lazytime` already defers atime updates) · online `discard` · alternative
I/O schedulers · ASPM / APST / governor / EPP · `io_poll` · `rcu_nocbs` · `vm.laptop_mode` ·
`vm.vfs_cache_pressure` · and the scheduler's time slice.

The last one is worth a postscript, because it was originally on the list. At the time, the machine
ran `scx_cake`, and its 3 ms slice was the natural suspect for wake-up-bound latency. It was dropped
here because measurement showed the browser launch is **CPU-bound rather than wake-bound**. Months of
later work in [study 01](../01-scheduler-selection/README.md) vindicated that decision in an
unexpected way: cake *does* have a real problem, but it is a **1.9× context-switch penalty**, and the
slice was **refuted** as its cause. The lever that looked right here belongs in a different study.

## 8. Reproduce this

```bash
# Re-derive any comparison. Labels are the arms.
FSLAT_RESULTS=$PWD/data/tuning-results.jsonl \
  python3 tools/perf-stats.py show base-mkstorm logbsize256-mkstorm15
FSLAT_RESULTS=$PWD/data/tuning-results.jsonl \
  python3 tools/perf-stats.py cmp  base-mkstorm xfssyncd_centisecs-1000 xfssyncd_centisecs-10000
FSLAT_RESULTS=$PWD/data/tuning-results.jsonl \
  python3 tools/perf-stats.py cmp  base-rm-serial3 base-rm-parallel8

# Or produce fresh measurements (needs root for RAPL and for drop_caches):
sudo python3 tools/fslat.py run --label <name> --wl wl.sh coldmeta
sudo tools/ra-sweep.sh        # read_ahead_kb sweep
sudo tools/dw-sweep.sh        # dirty-writeback shaping
```

To re-test the `logbsize` mechanism specifically, `tools/loopback-logbsize.sh` reproduces the
throwaway-image experiment that proved the option is first-mount-only, and `tools/logbsize-ab.sh`
drives the cache-dropped on-metal A/B that §3 leaves open.

## 9. Data files

| File | Contents |
|---|---|
| `data/tuning-results.jsonl` | 315 rows, shared with [study 02](../02-boot-and-power/README.md); each row carries its own tunable snapshot — the artifact that exposed §3 |
| `data/tuning-results-pooled.jsonl` | pooled rows where n was built up across runs |
| `data/logs/baseline.log`, `band.log`, `after.log` | before / during / after run logs with the workload-comparison tables |

Schemas and label conventions: [`data/README.md`](../../data/README.md).