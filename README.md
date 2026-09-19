# System Tuning Benchmarks

Three measurement campaigns run on one laptop, published with their raw data so the results can be
checked and repeated rather than believed.

The machine is an **ASUS Zenbook S 16 (UM5606WA)** — AMD Ryzen AI 9 HX 370, a hybrid Zen 5 / Zen 5c
part with 12 cores and 24 threads — running **CachyOS** on a **`PREEMPT_RT`** kernel with the **BORE**
scheduler. Full specification: [`TEST-BED.md`](TEST-BED.md).

The point of publishing this is not that the conclusions are universal. It is that the **method** is
transferable, and that a lot of the value is in what measurement **refused to approve**. Across 21
tuning levers, roughly half were actively disproved; a scheduler that everybody uses turned out to
carry a 1.9× penalty; and one confidently-recommended setting turned out to make things measurably
worse.

---

## The three studies

| Study | Question | Headline result |
|---|---|---|
| [**01 — Scheduler selection**](studies/01-scheduler-selection/README.md) | Which `sched_ext` CPU scheduler is worth running? | No `sched_ext` scheduler beats the kernel's own BORE on latency. `scx_pandemonium` beats BORE on **power in every under-saturated class** (−11% idle, −35% I/O burst, −30% reclaim energy) while **tying it exactly on throughput**. `scx_cake` carries a **1.9× context-switch penalty** and `scx_cosmos` **cannot attach at all** on `PREEMPT_RT`. |
| [**02 — Boot and power audit**](studies/02-boot-and-power/README.md) | How much of the standard performance checklist is actually true here? | **7 levers adopted, 12 refused by measurement**, for **−5.65 s of boot time** and a large cut in memory-reclaim stalls. Includes a hazard that silently destroys the NVMe APST table. |
| [**03 — Filesystem latency**](studies/03-filesystem-latency/README.md) | Can filesystem latency be cut without spending power? | Mostly **no**. One silently-dead setting was found and made live; `rm -r` is bounded by serial metadata round trips, and a browser launch is only ~70–90 ms of filesystem out of ~530 ms. |

326 measured scheduler slots and 315 tuning rows, **every one passing its own identity check**.

## Results worth reading even on different hardware

These transfer because they are about how systems behave, not about this CPU:

1. **A setting in a config file is not a setting in effect.** `/etc/fstab` asked for
   `logbsize=256k`; the kernel had `32k`, because **XFS log geometry is chosen at first mount and a
   remount silently returns success and does nothing.** A `vm.swappiness = 195` was overwritten with
   `150` by a udev rule at every boot. Both looked perfectly configured.
   *Read every setting back from where its consumer reads it.*
2. **Two daemons writing one sysfs attribute is a silent race.** `power-profiles-daemon` and
   `asusd` both wrote `platform_profile` unconditionally; whichever ran last won. Each works
   correctly alone, so the bug is invisible until you look for a second writer.
3. **Some "tuning" is load-bearing.** Disabling writeback throttling (`wbt_lat_usec=0`) made browser
   launch under write load **30% slower** with queue latency 12–24× worse. The standard advice is
   backwards.
4. **A flat platform signal can hide a 1.57× difference.** `cpu_capacity` reads **1024 on all 24
   CPUs** here, despite one cluster clocking 5.16 GHz and the other 3.29 GHz. Any heuristic that
   trusts it — scheduler, load balancer, your own script — sees a uniform machine. The real ranking
   is in `acpi_cppc/highest_perf` and `cpufreq/cpuinfo_max_freq`.
5. **A scheduler's shipped default may be a no-op.** `scx_bpfland`'s `-m auto` resolves to *all 24
   CPUs* here, i.e. its domain logic is disabled; the same flag set to `performance` resolves to
   exactly the fast cluster. **Best scheduler and best-configured scheduler are different questions.**
6. **Watching your own benchmark can cost more than the effect.** A display blanking mid-run changes
   package power by **~13 W** — larger than any scheduler or sysctl effect measured in this
   repository. Measure and record the ambient state, or measure nothing.
7. **An adaptive system can act and still not matter.** `scx_pandemonium`'s learning loop
   demonstrably retunes (slice 999 → 1398 → 1114 µs, nine retunes recorded, oscillator active), yet
   is **measurably identical** to its own `--no-adaptive` build across six workload classes at
   n = 11–12. "It adapts" and "adapting helps" are separate claims needing separate tests.
8. **`systemd-analyze blame` reports activation, not cost.** Five `dev-ttyS*.device` entries at ~5.5 s
   look like serial-port probing; they are device units activating during the initramfs→rootfs
   handoff, and `8250.nr_uarts=0` would have fixed nothing.

## How to read this repository

```
README.md            you are here
METHODOLOGY.md       the protocol, and the catalogue of ways it has already been fooled
TEST-BED.md          the exact hardware and software — and whether yours resembles it
studies/             01 scheduler selection · 02 boot and power · 03 filesystem latency
data/                raw rows, traces, logs, probes  →  data/README.md is the dictionary
tools/               the harness  →  tools/README.md has the invocation recipes
```

**Read in that order if you want to judge the work rather than just the results.** The studies assume
[`METHODOLOGY.md`](METHODOLOGY.md); most of the objections a careful reader would raise — why
Mann-Whitney and not a t-test, why medians, why the first repetition is discarded, why the control's
own spread comes first — are answered there.

Every study ends with what it does **not** claim. The largest open item is stated plainly: the power
advantage of `scx_pandemonium` is established for light, bursty and reclaim-heavy work, and **not**
for sustained load, because the longest saturating phase was 8 seconds and that is nowhere near
thermal equilibrium.

## Reproduce it

No setup is needed to re-derive every published number from the checked-in data:

```bash
python3 tools/scx_deep_analyse.py --passes deepA,deepA2

FSLAT_RESULTS=$PWD/data/tuning-results.jsonl \
  python3 tools/perf-stats.py cmp mem-swap-150 mem-swap-200

FSLAT_RESULTS=$PWD/data/scx-pass-ab.jsonl \
  python3 tools/perf-stats.py cmp passA:cake passA:bore passA:bpfland passA:pandemonium
```

Recording new measurements needs the hardware, root, and about an hour per pass — see
[`tools/README.md`](tools/README.md). If you are considering a `sched_ext` scheduler, start with
`tools/scx-w0-attach.sh`: it is a feasibility gate that verifies each candidate actually stays
attached, and it is what caught the `scx_cosmos` failure before it could pollute a measurement pass.

## Caveats

- **One machine, one operator, three days.** Nothing here is a claim about your hardware.
- **Power figures come from RAPL package counters.** The battery cannot be used on this laptop (AC
  with an 80% charge cap, so `power_now` reads zero).
- **The kernel's "stock" scheduler is BORE, not vanilla EEVDF.** Every comparison against "the
  kernel" means against BORE on this specific kernel build.
- **`PREEMPT_RT` × `sched_ext` is thinly tested upstream**, and this repository contains direct
  evidence of that in the form of a scheduler that cannot start.
- **Kernel, tool and scheduler versions matter.** Binary SHA-256 hashes are pinned in study 01 §5 so a
  reader can tell whether they are comparing like with like.

## Licence

Reports and data are licensed **[CC BY 4.0](LICENSE)** — use them, quote them, build on them, with
attribution. The code in [`tools/`](tools/) is **[MIT](LICENSE-MIT)**.

Corrections and replications are the point of publishing this. If a number here does not reproduce on
your machine, that is a more interesting result than agreement.
