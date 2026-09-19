# 02 — Auditing a laptop against the Arch Wiki performance checklist

**Question.** Given a reasonably well-maintained Arch-derived laptop, how much of the commonly
recommended performance advice is actually **true on this machine**, and how much of it is cargo cult?

**Answer, in one line.** Of 21 levers examined, **7 were adopted**, **12 were refused by measurement**
and 2 were verified already optimal — for a **5.65 s reduction in boot time** and a large reduction in
memory-reclaim stalls, which is a respectable outcome built mostly out of *rejections*.

The most useful results here are the refusals. Each one is a plausible, widely-repeated tuning knob
that this machine's own counters say does nothing — or actively hurts.

---

## 1. Headline results

### Adopted

| # | Change | Evidence |
|---|---|---|
| 1 | Wifi power save **off** | idle-path round-trip latency **24.8 → 15.4 ms** (median, 4/4 reps); burst path unchanged; no measurable energy cost |
| 2 | `cfg80211.ieee80211_regdom=HK` on the kernel command line | mechanism-based, not measured: the module default `00` (world) is the most restrictive regulatory domain |
| 3 | `vm.min_free_kbytes` 524288 → **262144** | pooled n = 12/arm: kswapd steal **−29%** (p = 0.001), kswapd scan **−22%** (p = 0.008), pswpout **−92%** (p = 0.043), energy −5.6% (p = 0.050), nothing worse |
| 4 | `vm.watermark_scale_factor` 150 → **10** | memory-pressure stall **−70%** (p = 0.004), kswapd scan **−53%** (p = 0.004), kswapd steal **−39%** (p = 0.004); 50 and 10 were indistinguishable, so the kernel default was kept |
| 3+4 | the **combination** | kswapd steal **−64%** (p = 0.004), kswapd scan −65% (p = 0.025), device busy −10% (p = 0.037), stalls −91% (p = 0.055); confirmed again on a fresh independent run: kswapd steal −38.5% (p = 0.016) |
| 5 | `NetworkManager-wait-online.service` **disabled** | its own boot cost was **4.007 s** — the largest single userspace item |
| 6 | `cups.service` and `avahi-daemon.service` **no longer forced at boot** | 404 ms (avahi) + 18 ms (cups) of boot work removed; both remain socket/D-Bus activated on demand |
| 7 | `power-profiles-daemon` **masked**, `platform_profile` handed to `asusd` | 756 ms in the boot critical chain, and it removes a genuine double-writer race — see §5 |

### Boot time, before and after

| Stage | Before | After | Δ |
|---|---|---|---|
| firmware | 6.036 s | 6.035 s | 0 |
| bootloader | 0.434 s | 0.430 s | −0.004 s |
| kernel | 1.707 s | 1.676 s | −0.031 s |
| initramfs | 3.753 s | 3.072 s | −0.681 s *(unexplained — see note)* |
| **userspace** | **11.066 s** | **6.128 s** | **−4.938 s** |
| **total** | **22.997 s** | **17.343 s** | **−5.654 s** |
| **to `graphical.target`** | **8.024 s** | **5.660 s** | **−2.364 s** |

Three units disappeared from `systemd-analyze blame` entirely: `NetworkManager-wait-online.service`
(4.007 s), `power-profiles-daemon.service` (756 ms) and `avahi-daemon.service` (404 ms). Those sum to
**5.167 s against an observed userspace reduction of 4.938 s** — consistent within variance, with
`cups.service` (16 ms, socket-activated) and plymouth still present.

**One number is not claimed as a win.** The initramfs stage also fell 0.681 s, and that is
unexplained: the initramfs build was **not** changed (lz4 was tested and rejected — see §3), and
`limine-update` regenerated the image with the same zstd compression. Treat that 0.681 s as variance,
not as an effect of this audit. It is listed in the table because removing it would be dishonesty by
omission.

**Plymouth was measured and deliberately kept.** It costs ~1.5 s of boot in several small units
(616 ms `plymouth-read-write`, 173 ms `plymouth-start`, 155 ms `plymouth-quit-wait`, 155 ms
`plymouth-quit`, 20 ms `plymouth-switch-root`). Removing it would cost the graphical boot splash,
which is a preference rather than a performance question, so it is not in the adopted table.

## 2. Method

Identical to [`METHODOLOGY.md`](../../METHODOLOGY.md): arms interleaved within every repetition, n ≥ 5
for anything adopted, the control's own spread as the first filter, then a tie-corrected
Mann-Whitney U at p < 0.05. Energy from RAPL package counters, because the battery cannot be used on
this machine (AC with an 80% charge cap).

The one addition specific to this study: **every adopted setting is verified by reading it back from
where its consumer reads it** — the sysctl, `/proc/cmdline`, `NetworkManager --print-config`, the
sysfs path the driver actually consults — never from the file that was edited.
`tools/verify-adopted-settings.sh` is that check, and it reports `PASS`/`FAIL` per setting.

That rule is not pedantry. Two settings on this machine were **silently dead** while looking perfectly
configured:

- a `vm.swappiness = 195` in `/etc/sysctl.d/` that a udev rule overwrote with `150` **at every boot**;
- a `logbsize=256k` in `/etc/fstab` that the kernel ignored because **XFS log geometry is chosen at
  first mount only** (covered in [study 03](../03-filesystem-latency/README.md)).

Both were found only by reading the live value instead of the file.

## 3. Refused by measurement

Each row is a lever that is commonly recommended, that was implemented, measured — and reverted. The
"measurement" column is what refused it.

| Lever | Why it was refused |
|---|---|
| `netdev_max_backlog` 1000 → 5000 | `TcpExtTCPBacklogDrop`, `UdpInErrors` and `IpExtInNoRoutes` are all **zero**. There is no backlog to raise. |
| `tcp_fastopen` 1 → 3 | Every TFO counter is zero, and traffic terminates at a local proxy, so cookies are never negotiated. |
| `wlan0` MTU 1500 → 2304 | The access point **drops >1500-byte ICMP at both MTUs** (100% loss), so the effect is unmeasurable; the `-s 1400` control was identical (**7.0 vs 7.4 ms**). |
| `vm.swappiness` 150 → 60 / 100 | Statistically identical to 150. |
| `vm.swappiness` 150 → **200** | **Catastrophic**, and reproduced: wall 11.25 → 13.55 s, mean power 10.07 → 12.07 W, major faults 5215 → 14472, kswapd scan 670969 → 1400056 (p = 0.004). This finally answers a question an earlier campaign could not: it had compared 150 vs 195 at *zero* memory pressure, where swappiness cannot matter. |
| `wbt_lat_usec` 2000 → **0** | **Disabling writeback throttling makes browser launch 30% slower under write load** (p = 0.009) with queue latency **12–24× worse**. WBT is load-bearing, not overhead. 1000 and 10000 are indistinguishable from 2000. |
| `dirty_writeback_centisecs` 1500 → 500 / 100 | Nothing reached p < 0.05, and launch latency trended worse (+6–9%). |
| initramfs `zstd` → `lz4` / `xz` | Measured on the real 55 MB payload: lz4 decompresses 39 → 24 ms (**15 ms saved**) but grows the image 56.7 → **65.8 MB** (+9.1 MB to read from the ESP). Net ≈ 10–30 ms out of a 3750 ms initramfs stage = **0.3–0.8%**. xz is worse (+39 ms). |
| `io_poll` / polled queues | Mechanism, not measurement: polling applies only to I/O that explicitly requests polled completion (`REQ_POLLED` / `RWF_HIPRI` — `io_uring` `IOPOLL`, `preadv2` hiprio). XFS metadata reads through the page cache never set those flags, so polled queues cannot help and cost a spinning core. |
| `8250.nr_uarts=0` | `journalctl -k` shows **zero** serial8250 output. The 5.5 s `dev-ttyS*.device` entries are device units *activating* at the initramfs→rootfs handoff (all at T = 5.4 s alongside the NVMe device), not 5.5 s of serial probing. `systemd-analyze blame` was misread here — see §6. |
| NVMe APST tuning | `id-ctrl` shows PS3 = 0.015 W / 2.5 ms exit and PS4 = 0.005 W / 6 ms exit, against **0.3 W** idling in PS0–2. Measured transition cost: **+26 µs per request**, ~+1.6 ms per idle→active cycle. That buys ~**285 mW** of idle power — far outside the agreed budget. |
| `profile-sync-daemon` for the browser profile | The 2.8 GiB profile is `storage/default` **site data**, not cache (`~/.cache/zen` is 73 MB). Moving it to tmpfs would risk data and RAM for no I/O win. |

**The one that is a warning rather than a rejection** is `wbt_lat_usec=0`. It is a common
recommendation — "disable writeback throttling, it adds latency" — and here it is exactly backwards:
throttling is what keeps queue latency bounded under a heavy writer, and removing it made the
interactive workload measurably worse. It is worth checking on any machine before disabling.

## 4. Verified already optimal

No action taken, because measurement or mechanism said the current state is already right:

`none` I/O scheduler for NVMe · weekly `fstrim.timer` · BBR congestion control with `fq_codel` ·
`systemd-resolved` caching stub · MGLRU fully enabled (`0x0007`) · `systemd-oomd` ·
`-march=native -O3` with mold, LTO and ccache · `mitigations=off` (a deliberate choice) ·
`nowatchdog` · `amd_pstate=active` with `balance_performance` EPP · 1 MiB-aligned partitions ·
`/tmp` on tmpfs · `max_sectors_kb == max_hw_sectors_kb` (128 KB, the drive's MDTS limit) ·
`/boot` and `/etc` free of `.pacnew` · XFS scrub clean.

## 5. `power-profiles-daemon` vs `asusd` — a general lesson about duplicate writers

`power-profiles-daemon` sat immediately before `graphical.target` in the boot chain (756 ms,
ordered `After=display-manager.target`). The question was whether removing it loses battery-aware
profile switching. It does not, and the evidence is direct:

- `asusctl profile get` reports **`AC profile Balanced` / `Battery profile Quiet`** — `asusd` is
  *already* doing AC/battery-aware switching.
- Stopping `power-profiles-daemon` changed **nothing**: `platform_profile=balanced` and
  `energy_performance_preference=balance_performance` on every `cpufreq` policy, before and after.
- `asusd`'s binary exposes `platform_profile_on_ac`, `platform_profile_on_battery`,
  `change_platform_profile_on_ac`, `change_platform_profile_on_battery`,
  `platform_profile_linked_epp` and per-profile EPP settings. It watches `/sys/class/power_supply`.
  **`power-profiles-daemon` has no AC/battery auto-switching at all.**

The two options were to drop only the boot start (the D-Bus service activation is separate, so
`powerprofilesctl` and desktop widgets would still work), or to hand `platform_profile` to `asusd`
entirely. The second was chosen, after checking that nothing on the desktop queries the PPD D-Bus
interface — no consumer was found, and the desktop's power UI uses `upowerd`, which is a different
service from `org.freedesktop.UPower.PowerProfiles`.

**The generalisable part:** these two daemons were both writing the same sysfs attribute
unconditionally. Whichever wrote last won, and there was no coordination. That is a race that exists
on any system with a vendor daemon *and* a generic one — and it is invisible until you look for two
writers, because each daemon works correctly in isolation. Masking (rather than `disable`) was chosen
deliberately, because it blocks D-Bus activation as well as the boot start, which is what makes the
handover real.

## 6. A `systemd-analyze blame` misreading worth passing on

The boot chart showed `dev-ttyS0.device` through `dev-ttyS4.device` at ~5.5 s, which looks exactly
like 5.5 s of probing five non-existent serial ports — and `8250.nr_uarts=0` is the standard advice
for it. It is wrong here: `journalctl -k` shows **zero** serial8250 output, and every one of those
units is stamped at T ≈ 5.4 s, alongside `dev-nvme0n1p2.device`. They are device units *activating*
during the initramfs→rootfs handoff, not a serial probe.

`systemd-analyze blame` reports **activation**, not **cost**, and the two diverge badly for device
units that are dependencies of the root mount. The lever was refused on that basis, and the
misreading is recorded here because the same trap awaits anyone auditing boot time.

## 7. A silent configuration conflict, found by asking the daemon

`/etc/NetworkManager/conf.d/` held two contradictory files:

- `99-wifi-powersave.conf` → `wifi.powersave = 2` (**disable** power save)
- `default-wifi-powersave-on.conf` → `wifi.powersave = 3` (**enable** power save)

NetworkManager applies these in **lexicographic filename order**, and `d` sorts after `9`, so the file
named "default" silently won and power save was **on** — the exact opposite of the file whose name
and contents stated the opposite intent.

The fix was to archive the later-sorting override. The lesson is in how it was found:
**`NetworkManager --print-config`, not reading the files.** Two files that disagree cannot be resolved
by reading either one; the resolving rule lives in the daemon.

## 8. Hazard — `nvme set-feature -f 0x0c` destroys the APST table

Worth its own section, because the failure mode is silent and the natural recovery does not work.

`nvme set-feature -f 0x0c` is the obvious way to toggle NVMe autonomous power state transitions.
**It does not toggle a bit.** `nvme-cli` writes a zeroed 256-byte data buffer to the feature, which
**wipes the entire APST table** — every entry's idle-time and idle-power values become 0 — and
`-v 1` will not restore it, because there is nothing left to re-enable.

On this machine that would have left the drive idling at ~0.3 W instead of 15 mW: a silent, permanent
regression in exactly the metric this audit was trying to improve.

**Recovery, if it happens to you:** a controller reset re-runs the driver's own APST configuration:

```bash
echo 1 > /sys/class/nvme/nvme0/reset_controller
```

Then verify by reading the table back — APSTE enabled, the expected idle transition times, device
state `live`, SMART still passing.

**The safe way to bound NVMe power states is not this feature at all:**

```
nvme_core.default_ps_max_latency_us=<µs>     # on the kernel command line
```

or a controller reset after any experiment. Save the pre-experiment table
(`nvme id-ctrl` plus `nvme get-feature -f 0x0c`) before touching anything.

## 9. Measured, and deliberately not acted on

**The largest untested throughput lever on this machine is transparent huge pages, and they are
absent.** The running kernel is a `PREEMPT_RT` build without `CONFIG_TRANSPARENT_HUGEPAGE`: there is
no `khugepaged` thread, no `/sys/kernel/mm/transparent_hugepage`, and no `thp_*` vmstat counters.
Browsers, JVMs, virtual machines and compilers all benefit from THP, so this is a real cost — it is a
deliberate trade-off of an RT kernel, and an LTS kernel is installed for an A/B if it ever matters.

Also left alone as deliberate choices: zram sized at 74.9 GiB with a 24 GiB memory limit,
`vm.overcommit_memory=1`, `vm.percpu_pagelist_high_fraction=64`,
`vm.compact_unevictable_allowed=0`, `vm.extfrag_threshold=500`, `vm.max_map_count=2147483642`, and
MGLRU left at the kernel default rather than pinned.

## 10. A bug in the verifier itself

The first version of the verification script decided "has this boot happened since the change?" by
comparing the current boot against a stamp file under `/root/`. `/root` is mode `0700`, a non-root run
could not read it, no reboot was detected, and **every boot-path check stayed at `PEND` — including
after the reboot had actually happened**. Worse, `PEND` incremented neither the pass nor the fail
counter, so the summary read "0 failed" while three checks were silently unresolved.

Both defects are fixed in `tools/verify-adopted-settings.sh`: boot-path checks now assert
**configuration state** (which needs no stamp file and is reboot-independent), and `PEND` is counted,
reported in the summary, and reflected in the exit status.

The general lesson: **a verification tool that can silently report success is worse than no
verification tool.** Any `PASS`/`FAIL`/`PEND` scheme must make unresolved states loud.

## 11. Reproduce this

```bash
# Re-derive any comparison above. The labels are the arms.
FSLAT_RESULTS=$PWD/data/tuning-results.jsonl \
  python3 tools/perf-stats.py show  mem-swap-150 mem-swap-200      # swappiness 150 vs 200
FSLAT_RESULTS=$PWD/data/tuning-results.jsonl \
  python3 tools/perf-stats.py cmp   mem-minfree-524288 mem-minfree-262144
FSLAT_RESULTS=$PWD/data/tuning-results.jsonl \
  python3 tools/perf-stats.py cmp   mem-wmark-150 mem-wmark-10 mem-wmark-50
FSLAT_RESULTS=$PWD/data/tuning-results.jsonl \
  python3 tools/perf-stats.py cmp   wbt-2000 wbt-0 wbt-1000 wbt-10000
FSLAT_RESULTS=$PWD/data/tuning-results.jsonl \
  python3 tools/perf-stats.py show  apst-gap0.02 apst-gap0.15

# Check that adopted settings are live, from where their consumers read them:
sudo tools/verify-adopted-settings.sh
```

The levers themselves were swept with `tools/sysctl-sweep.sh`, `tools/ab-sweep.sh`,
`tools/dw-sweep.sh`, `tools/ra-sweep.sh`, `tools/mem-combo.sh` and `tools/band.sh`.

## 12. Data files

| File | Contents |
|---|---|
| `data/tuning-results.jsonl` | 315 rows — every arm of both this study and [study 03](../03-filesystem-latency/README.md), each with a self-describing tunable snapshot |
| `data/tuning-results-pooled.jsonl` | 157 pooled rows for the memory-pressure levers, which is how n = 12 was reached |
| `data/logs/baseline.log`, `band.log`, `after.log` | the before/band/after run logs, including the workload-comparison tables |
| `data/probes/post-reboot-verify-*.txt` | the verification receipts, after the reboot that activated the boot-path changes |
| `data/probes/boottime-*.txt`, `blame-*.txt` | `systemd-analyze` critical-chain and blame output |

Schemas and label conventions: [`data/README.md`](../../data/README.md).