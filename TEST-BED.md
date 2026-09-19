# Test bed

All measurements in this repository were taken on one machine, by one operator, over three
consecutive days. This document is the machine, precisely enough that a reader can decide how far the
results should transfer.

**Summary:** an ASUS Zenbook S 16 with an AMD Ryzen AI 9 HX 370 (hybrid Zen 5 / Zen 5c, 12 cores /
24 threads) running CachyOS on a `PREEMPT_RT` CachyOS kernel with the BORE scheduler built in.

---

## 1. Hardware

| Component | Detail |
|---|---|
| Chassis | ASUS Zenbook S 16, model `UM5606WA` (board `UM5606WA`) |
| SoC | AMD Ryzen AI 9 HX 370 w/ Radeon 890M |
| Integrated GPU | AMD Radeon 890M (`1002:150e`, Strix) |
| CPU | 12 cores / 24 threads, SMT2, one socket |
| L3 | 24 MiB in **2 instances** — one per cluster |
| Memory | 32 GiB LPDDR5X-7500, 4 × 8 GiB Micron `MT62F2G32D4DS-026 WT` (~29 GiB usable) |
| Storage | WD PC SN560 `SDDPNQE-1T00-1202`, 1 TB NVMe, firmware `74118000` |
| Panel | backlight device `amdgpu_bl1`, `max_brightness` 400000 |
| Power | AC with an 80% charge cap — **the battery cannot be used as an energy oracle here** |

### 1.1 The hybrid split — and the trap in it

The CPU is two clusters with a large clock gap, presented by firmware in a way that hides it:

| | CPUs | `cpuinfo_max_freq` | `acpi_cppc/highest_perf` | L3 instance | `cpu_capacity` |
|---|---|---|---|---|---|
| **Zen 5** (fast) | 0–3, 12–15 | 5,157,895 kHz | 208 (196/202 on 1/2/13/14) | 0–3, 12–15 | **1024** |
| **Zen 5c** (dense) | 4–11, 16–23 | 3,289,474 kHz | 125 | 4–11, 16–23 | **1024** |

**`cpu_capacity` reads 1024 on all 24 CPUs** even though the fast cluster clocks **1.57× higher**.
The real ranking is only visible in `acpi_cppc/highest_perf` and `cpufreq/cpuinfo_max_freq`. Note also
that `highest_perf` is not uniform *within* the fast cluster — cores 1 and 13 rank slightly below 0
and 3, i.e. the firmware exposes a preferred-core ordering even inside a cluster.

Consequences that shaped the studies:

- The analysis never trusts `cpu_capacity`. Hybrid placement is computed from **per-CPU tick deltas**
  and reported as `fast_share` (fraction of ticks on the fast cluster) and `fast_excess`
  (that fraction divided by its pro-rata expectation, so **1.0 = exactly pro-rata**).
- SMT siblings pair across the clusters: `(0,12)`, `(1,13)`, … `(11,23)`. This makes "pin to a fast
  CPU" and "avoid an SMT sibling" different problems, and it is why the scheduler study reports
  per-cluster frequencies rather than a single CPU number.
- A scheduler that keeps the fast cluster at its ceiling and one that never leaves the dense ceiling
  can show the same *elapsed time* and very different *energy*. That is the mechanism behind the
  headline result of the scheduler study.

## 2. Software

| Item | Version |
|---|---|
| Distribution | CachyOS (Arch-derived) |
| Kernel | `7.2.5-1-cachyos-rt-bore-lto` (package `linux-cachyos-rt-bore-lto`) |
| Kernel build | `#1 SMP PREEMPT_RT` |
| CPU scheduler (kernel) | **BORE** — `CONFIG_SCHED_BORE=y`, `sched_bore=1` |
| `scx-scheds-git` | `1.1.3.r358.g3c4506bfd-1` (builds from `sched-ext/scx@3c4506bfd`) |
| Scheduler loader | `scx_loader` (D-Bus `org.scx.Loader`), `scxctl` CLI |

### 2.1 Kernel configuration facts that matter

| Config | Value | Why it matters |
|---|---|---|
| `CONFIG_PREEMPT_RT` | `y` | Undocumented in combination with sched_ext; directly implicated in a failure this repository reports |
| `CONFIG_HZ` | `300` | RT-tuned; wakeup-granularity floor for every latency number here |
| `CONFIG_SCHED_BORE` | `y` | **The kernel's own scheduler is BORE, not stock EEVDF.** "Kernel baseline" in these studies means BORE |
| `CONFIG_X86_AMD_PSTATE` | `y` | `amd-pstate-epp`, see §3 |
| `CONFIG_TRANSPARENT_HUGEPAGE` | **absent** | No `khugepaged`, no `/sys/kernel/mm/transparent_hugepage`, no `thp_*` vmstat counters. A deliberate RT-build trade-off, and the largest single untested throughput lever on this machine |

### 2.2 Kernel command line

```
quiet zswap.enabled=0 amd_pstate=active mitigations=off nowatchdog splash rw
root=<uuid> rootflags=logbsize=256k cfg80211.ieee80211_regdom=HK
```

`mitigations=off` and `nowatchdog` are deliberate on this machine; they are not part of any result
below, but they are part of the baseline that every result was measured against.

### 2.3 sched_ext schedulers present

`beerland bpfland cake cosmos flash flow forge lavd mlfq pandemonium p2dq tickless rustland rusty`

Three of these matter to the published study, because they are the ones with documented adaptive or
latency-oriented designs: `cake`, `bpfland`, `pandemonium`. `cosmos` is the fourth and it **could not
be attached at all** on this kernel — see study 01 §6.

## 3. Power and clock management

| Knob | Value |
|---|---|
| `scaling_driver` | `amd-pstate-epp` |
| `scaling_governor` | `powersave` |
| `energy_performance_preference` | `balance_power` (uniform across policies) |
| Available governors | `performance`, `powersave` — **there is no `schedutil`** |
| Available EPP | `default performance balance_performance balance_power power custom` |
| Platform profile provider | `asusd` (AC → Balanced, battery → Quiet), with `platform_profile_linked_epp` |

Two consequences:

- **There is no `schedutil`.** Any scheduler that assumes it can steer frequency through utilisation
  has nothing to steer on this machine: EPP and the platform profile are the only levers, and both
  are firmware-side. Scheduling decisions can therefore change *which core* runs work, but never
  *how fast that core is allowed to clock* — which is exactly how the frequency mechanism in study 01
  gets its signature.
- `power-profiles-daemon` is masked, so `asusd` is the only writer of `platform_profile`. Having two
  writers of the same sysfs attribute is a real race, and the boot/power study documents how this was
  found and resolved.

## 4. Storage and filesystem

| Item | Value |
|---|---|
| Root filesystem | XFS on `nvme0n1p2` |
| Mount options | `rw,lazytime,relatime,inode64,logbufs=8,logbsize=256k,noquota` |
| I/O scheduler | `none` (NVMe) |
| Swap | zram, 74.9 GiB, priority 32767 |
| Read-ahead / queue depth | `read_ahead_kb=256`, `nr_requests=1023` |

`logbsize=256k` arrives via `rootflags=` on the command line, **not** via `/etc/fstab` — XFS chooses
log geometry at first mount and ignores the fstab option. Study 03 explains why this distinction cost
a measurement campaign.

## 5. Measured configuration snapshot

Every row in every `*.jsonl` file carries the tunables that were live when it was recorded, so any
row is self-describing. The defaults across the campaigns were:

```json
{
  "fs.xfs.xfssyncd_centisecs": "10000",
  "fs.xfs.speculative_prealloc_lifetime": "300",
  "vm.dirty_background_ratio": "5",
  "vm.dirty_ratio": "10",
  "vm.dirty_background_bytes": "0",
  "vm.dirty_bytes": "0",
  "vm.dirty_writeback_centisecs": "1500",
  "vm.dirty_expire_centisecs": "3000",
  "vm.swappiness": "150",
  "vm.vfs_cache_pressure": "50",
  "vm.page-cluster": "0",
  "queue.read_ahead_kb": "256",
  "queue.nr_requests": "1023",
  "queue.scheduler": "none",
  "mount.logbufs": "logbufs=8",
  "mount.logbsize": "logbsize=32k",
  "mount_options": "rw,lazytime,relatime,inode64,logbufs=8,logbsize=32k,noquota"
}
```

Note `mount.logbsize = 32k` here against the `logbsize=256k` in §4: this snapshot is from the
**filesystem and boot campaigns**, recorded *before* the command-line change, and it is the recorded
proof that the fstab option was dead. The **scheduler** rows carry `logbsize=256k`, because they were
recorded after the reboot that made it live. The self-describing snapshot is what made the
discrepancy visible without a reboot, and it is why the field is never rewritten.

## 6. Does your machine resemble this?

The results should transfer best — and should be re-measured anyway — if:

- [ ] Your CPU is **hybrid with a large clock gap between clusters** (Intel P/E, AMD Zen 5 + Zen 5c,
      ARM big.LITTLE). Several conclusions are *about* that split and about a scheduler's willingness
      to use the fast cluster.
- [ ] Your kernel is **`PREEMPT_RT`**. The `scx_cosmos` failure in study 01 is an RT-specific defect,
      and it is worth testing before you rely on any scheduler.
- [ ] You are running **sched_ext** at all. `scx_loader`/`scxctl` behaviour (modes, `--args` parsing,
      unreliable exit codes) is version-specific; the study pins exact binary hashes.
- [ ] You have **no `schedutil`** governor (i.e. `amd-pstate-epp`). Without it, clock behaviour is
      firmware-driven and behaves like this machine's.
- [ ] You care about **laptop power and thermals**, not just throughput. The headline trade in
      study 01 is energy-versus-latency, and it only exists when energy is weighted.

Results that should transfer regardless of hardware, because they are methodological rather than
platform facts, are collected in the [top-level README](README.md#what-generalises-off-this-machine).
