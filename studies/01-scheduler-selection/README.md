# 01 — Choosing an `sched_ext` CPU scheduler for a hybrid-partition laptop

**Question.** On a hybrid Zen 5 / Zen 5c laptop, which of the available `sched_ext` schedulers is
worth using, judged on **power efficiency, latency and overhead (throughput cost)** — and is any of
them better than the kernel's own scheduler?

**Answer, in one line.** None of the `sched_ext` schedulers beats the kernel's stock **BORE** on
latency; `scx_pandemonium` beats it on **power** in every under-saturated workload class, ties it
exactly on throughput, and was therefore adopted — a Pareto choice, not a win. `scx_cake` carries a
**1.9× context-switch penalty**, and `scx_cosmos` **cannot attach at all** on a `PREEMPT_RT` kernel.

---

## 1. Headline results

Pass A, all four schedulers at their **shipped defaults**, 4 arms × 5 repetitions (20 slots,
19.4 min, 0 invalid). `cake` is the control because it was the incumbent.

| metric | BORE | cake | bpfland | pandemonium |
|---|---|---|---|---|
| **hackbench** (s, lower better) | 2.72 | **5.28** | 2.79 | 2.77 |
| **wakeup p50** (µs) | 12 | **5** | 10 | 14 |
| **wakeup p99** (µs) | **1102** | 2092 | 2820 | 2396 |
| **wakeup p99.9** (µs) | **3388** | 4520 | 7768 | 3364 |
| **wakeup max** (µs) | **4754** | 8143 | 12884 | 6066 |
| **`cyclictest` OTHER max** (µs) | **2924** | 24710 | 6605 | 3498 |
| `cyclictest` OTHER avg (µs) | 36 | 255 | 331 | **38.25** |
| **throughput** (bogo ops/s) | **7769** | 7684 | 7618 | 7719 |
| energy per bogo-op (J) | **0.002195** | 0.002222 | 0.002233 | 0.002213 |
| light-load power (J/s) | 17.09 | 17.34 | 17.12 | 17.61 |
| launch under load (ms) | 616 | **443** | 616 | 477 |
| hybrid placement (`fast_excess`, 1.0 = pro-rata) | 0.999 | 1.000 | 1.000 | 1.000 |

Significant against cake (Mann-Whitney, n = 5, p < 0.05): `hackbench` and J-per-hackbench-message
separated **all three** other arms; wakeup p99/p99.9/max separated BORE (better) and bpfland (worse);
`cyclictest` OTHER max separated all three.

**Ordering: BORE ≈ pandemonium > cake > bpfland.** Among the `sched_ext` schedulers, **pandemonium is
the best**. But the kernel's own **BORE matches or beats every `sched_ext` scheduler** — and beats all
of them on the wakeup tail.

Three more results, each developed below:

1. **cake's ~1.9× context-switch penalty is real and large** (§3) — the one genuinely user-visible
   pathology found anywhere in this repository.
2. **The penalty is *not* cake's 3 ms time slice** (§4), even though the slice was the obvious
   suspect. A control arm at cake's exact slice length is 2.6× faster.
3. **BORE and pandemonium are a Pareto frontier, not a ranking** (§7): BORE wins latency and burst
   speed, pandemonium wins power in every under-saturated class, and they tie exactly at saturation.

## 2. The framing correction that made this study meaningful

The first version of this work compared each scheduler **only at its shipped defaults**. That answers
*"which ships best tuned?"* — a packaging question. It is not the scheduling question, and the
research pass produced the proof before any measurement was taken:

| Arm | What its shipped default actually does on this machine | Consequence |
|---|---|---|
| `scx_cosmos` | `-c 0` **disables the locality engine its documentation leads with** | would measure cosmos *with its headline feature switched off* |
| `scx_bpfland` | `-m auto` resolves to **`primary CPU domain = 0xffffff`** — all 24 CPUs, i.e. a no-op | would measure bpfland *with its domain logic idle* |
| `scx_cake` | fixed **3 ms compile-time slice, no tunable surface at all** | its default *is* its design point — a finding, not a gap |
| `scx_pandemonium` | adaptive control loop **on** | its default *is* its design point |

So the study reports **both**: the defaults pass (Pass A) as a controlled reference that bounds how
much tuning buys, and designed configurations (Pass B) as the actual comparison — plus an attribution
pass (Pass C) that tests the leading causal hypothesis.

To keep this honest, two guards were fixed **before** any number was seen, and are described in
[`METHODOLOGY.md` §7](../../METHODOLOGY.md#7-two-design-guards): **no benchmark-fitted tuning** (every
configuration comes from the scheduler's own documented intent) and **no hidden weighting** (report
the Pareto frontier; state the weighting as a separate, arguable claim).

## 3. `scx_cake` has a ~1.9× context-switch penalty

`s hackbench -s 1024 -l 1500 -g 10 -T` — 400 tasks doing pipe ping-pong, a context-switch benchmark.
Lower is better. Every independent repetition is shown:

| arm | Pass A (5 reps) | Pass B (3 reps) |
|---|---|---|
| BORE | 2.686 / 2.702 / 2.720 / 2.728 / 2.854 | 2.466 / 2.483 / 2.513 |
| **cake** | **5.183 / 5.233 / 5.275 / 5.277 / 5.361** | **5.063 / 5.188 / 5.291** |
| bpfland | 2.490 / 2.790 / 2.795 / 2.877 / 2.960 | 2.238 / 2.340 / 2.554 |
| pandemonium | 2.688 / 2.747 / 2.765 / 2.770 / 2.778 | 2.675 / 2.695 / 2.740 |

**Zero overlap between cake and every other arm across all 8 independent repetitions.** This is the
largest effect in the campaign, and the one most likely to be felt as "the machine is sluggish under
thread-heavy load".

The same signal appears in the scheduling-latency counters: cake's `cyclictest` OTHER-class maximum
was **24710 µs** against BORE's **2924 µs**, and its mean 255 µs against BORE's 36 µs.

**The honest counterpoint:** cake is *best* on one metric — browser launch under load was **443 ms**
for cake against 616 ms for BORE and bpfland in Pass A. A scheduler that switches contexts more
slowly can still protect a single interactive process better, and cake's wakeup p50 was also the
lowest at 5 µs. It is a low-throughput, low-jitter design, and the penalty it pays is throughput
under thread-heavy load.

## 4. Pass C — is it cake's time slice? **No.**

The obvious explanation for a context-switch penalty is a long time slice, and cake's slice is a
**compile-time constant of 3 ms** with no flag to change it. That makes it impossible to sweep
directly — so it was tested by substitution instead: `bpfland` *does* accept a slice length, and was
run at **cake's exact 3000 µs**.

| arm | slice | `hackbench` |
|---|---|---|
| cake (fixed) | 3000 µs | **5.19 s** |
| bpfland `-s 3000` | 3000 µs | **2.01 s** |
| bpfland `-m performance` | default | 2.34 s |

**Same slice length, 2.6× apart.** The slice is **refuted** as the cause of cake's penalty. Something
else in cake's dispatch path — not its slice budget — is responsible. This is a negative result on the
hypothesis, but a strong positive result for the method: the "obvious" cause was cheap to test and
would otherwise have been published as an explanation.

As a bonus, the substitution arm is the best-behaved single configuration measured on throughput and
energy simultaneously: **2.01 s** on `hackbench` (fastest of any arm in any pass) with **675.7 J**
package energy — 11.4% below cake and the lowest of any Pass B arm — for a throughput cost of −0.3%
(not significant). It pays for that on latency, where its wakeup p99 is **2644 µs**, more than double
cake's.

## 5. Arms, versions and receipts

Every row carries its own receipt: the arm's **binary SHA-256**, the kernel release, and the
`sched_bore` sysctl. Rows whose live-scheduler read-back disagreed with the workload's own
`sched_ext` self-report were marked invalid — see
[`METHODOLOGY.md` §3](../../METHODOLOGY.md#3-arm-identity-verification-do-not-skip-this).

| arm | version | binary SHA-256 (first 12) |
|---|---|---|
| `bore` | kernel `7.2.5-1-cachyos-rt-bore-lto`, `sched_bore=1` | (kernel-resident) |
| `scx_cake` | 1.2.1 | `46fe11c3cdf6` |
| `scx_bpfland` | 1.1.3 | `da38635668bb` |
| `scx_cosmos` | 1.1.6 | `22fe18d9d56d` |
| `scx_pandemonium` | 5.20.0 | `ab2f6e2f5b13` |

All four `sched_ext` binaries come from `scx-scheds-git 1.1.3.r358.g3c4506bfd-1`, built from
`sched-ext/scx@3c4506bfd`. Switching between them is **live** — `scx_loader` performs the swap over
D-Bus, with no reboot, which is what makes this many arms affordable.

**"Default flags" means a bare launch** for all four: cake (no flags), bpfland (`-m auto`, which *is*
`-m`'s own default), cosmos (no flags), pandemonium (no flags).

**Designed configurations (Pass B)** were, in full:

| label | configuration | why |
|---|---|---|
| `bpfland -m performance` | stated intent: restrict the primary domain to fast CPUs | resolves to `primary CPU domain = 0x00f00f` = CPUs 0–3, 12–15 exactly — the Zen 5 cluster |
| `bpfland -s 3000` | cake's slice length | the substitution control for §4 |
| `bpfland -m auto` | shipped default | Pass A reference — resolves to `0xffffff`, all 24 CPUs, i.e. a no-op |
| `pandemonium` | shipped default, adaptive on | its design point |
| `pandemonium --no-adaptive` | BPF-only fallback | isolates the contribution of the adaptive layer |

That `-m performance` lands exactly on the Zen 5 cluster, while `-m auto` lands on *nothing*, is worth
its own note: the tool's own topology detection is correct, and the shipped default simply declines to
use it.

## 6. The deep pass — BORE vs pandemonium, phase-structured

Passes A–C reduce the field to two arms, so the budget goes into depth. **3 arms × 4 repetitions × 12
phases × 2 passes = 288 slots, 0 invalid** (18.0 + 18.1 min).

Why a phase-structured pass was necessary: the earlier passes measured **steady state** — one fixed
workload sequence, always the same length, the machine never required to *pivot*. That is exactly the
regime in which a static scheduler and an adaptive one look identical, which is what Pass B found
(`pandemonium --no-adaptive ≡ pandemonium` on every axis). **A null result there does not test an
adaptivity claim; it tests the one condition the claim is silent about.** So the deep pass crosses six
distinct workload classes, twice per slot, with per-phase metrics.

### 6.1 Result — a class-specific split that the mixed battery averaged away

Analysed with `tools/scx_deep_analyse.py --passes deepA,deepA2` (repetition 0 excluded, see
[`METHODOLOGY.md` §8.2](../../METHODOLOGY.md#82-first-slot-contamination)); medians, n = 11–12 per arm
per phase, p from a tie-corrected Mann-Whitney U against BORE.

| phase | metric | BORE | pandemonium | `--no-adaptive` | p vs BORE |
|---|---|---|---|---|---|
| `light` | wakeup p99 (µs) | **1011** | 2476 | 2288 | 0.000 |
| `light` | wakeup p50 (µs) | **11** | 13 | 13 | 0.000 |
| `light` | phase energy (J) | 144.9 | 144.2 | 144.2 | 0.001 |
| `storm` | wall (s) | **4.018** | 4.309 | 4.295 | 0.000 |
| `storm` | energy (J) | **139.7** | 149.3 | 149.0 | 0.000 |
| `storm` | mean power (W) | 17.01 | 17.03 | 17.02 | 0.116 |
| `sat` | throughput (bogo ops/s) | 8194 | 8212 | 8199 | **1.000 (tie)** |
| `sat` | energy (J) | 137.3 | 137.4 | 137.4 | 0.073 |
| `sat` | mean power (W) | 17.05 | 17.06 | 17.06 | 0.329 |
| `idle` | mean power (W) | 4.175 | **3.72** | 3.73 | 0.018 |
| `idle` | energy (J) | 33.39 | **29.76** | 29.85 | 0.018 |
| `io` | mean power (W) | 14.37 | **9.31** | 9.755 | 0.000 |
| `io` | energy (J) | 4.882 | **3.295** | 3.47 | 0.000 |
| `io` | metadata walk (s) | 0.0505 | **0.045** | 0.0475 | 0.000 |
| `io` | durable write (s) | **0.281** | 0.309 | 0.3075 | 0.001 |
| `mem` | mean power (W) | 9.68 | **6.495** | 6.385 | 0.000 |
| `mem` | energy (J) | 92.1 | **64.86** | 63.7 | 0.000 |
| `mem` | wall (s) | **9.513** | 9.957 | 9.944 | 0.000 |

Two mechanisms, cleanly separated:

1. **Under saturation, the power ceiling is a hardware property, not a scheduling choice.** `sat` and
   `storm` both sit at **~17.0 W for every arm**, and energy per unit work is identical (137.3 vs
   137.4 J for the same ~8200 bogo-ops). The storm's energy gap between arms is purely **time** —
   BORE finishes 7% sooner at the same power.
2. **In under-saturated phases the arms differ in *power state*, not in work done.** A CPU-busy trace
   during reclaim showed pandemonium *busier* (9.8% vs 7.3%) while using **30% less energy**, which
   exonerates CPU time and points at clock rate.

The robustness check is the same analysis restricted to the display-stable regime
(`--bl 0`, n = 10–11, 32 rows dropped): every direction and every significance above survives, with
`idle` at 4.09 W / 3.685 W and `io` at 14.68 W / 9.31 W. Raw output for both regimes is in
`data/analysis/`.

## 7. The Pareto frontier — and the choice that was made

Neither arm dominates. The trade is:

| axis | winner | magnitude | is it perceptible? |
|---|---|---|---|
| interactive wakeup p99 | **BORE** | 1011 vs 2476 µs (**2.4×**) | both are **sub-3 ms** — no |
| wakeup storm wall time | **BORE** | 4.02 vs 4.31 s (**7%**) | marginal |
| durable-write burst | **BORE** | 0.281 vs 0.309 s (**9%**) | marginal |
| idle power | **pandemonium** | 4.175 vs 3.72 W (**−11%**) | continuously: heat, fan, battery |
| I/O-burst power | **pandemonium** | 14.37 vs 9.31 W (**−35%**) | continuously |
| memory-reclaim power / energy | **pandemonium** | 9.68 vs 6.50 W; 92.1 vs 64.9 J (**−30%**) | continuously |
| throughput at saturation | **tie** | 8194 vs 8212 bogo ops/s, p = 1.000 | n/a |
| energy per unit work | **tie** | 137.3 vs 137.4 J for the same work | n/a |

**This is the key observation for anyone weighing it up: the axis one arm wins is perceptually empty,
and the axis the other arm wins is physical and continuous.** A 2.4× improvement in wakeup p99 sounds
decisive until the absolute values are read — both arms are comfortably below the 3 ms that a person
can notice. A 30% reduction in reclaim energy is not a factor anyone has to be told about; it is heat
and fan noise and battery runtime, all day.

**The decision, stated as a preference rather than a result:** for a laptop, thermal efficiency is
part of overall performance and power is weighted above sub-millisecond latency. On that weighting,
`scx_pandemonium` is the choice, and it is a *safe* one in three specific ways:

- **Nothing is conceded on throughput.** Exact ties on both bogo-ops and J-per-bogo-op at saturation,
  and pandemonium is never worse on power in any phase measured.
- **Leaving the incumbent was not optional.** cake's context-switch cost was 5.19 s against
  pandemonium's 2.69 s on the same workload (§3) — a 1.9× penalty, the one pathology in this campaign
  a user would actually feel.
- **The fallback is the performance winner.** If `scx_loader` ever fails to load pandemonium, the
  kernel's own scheduler runs, and that is BORE — the latency winner. The downside is bounded.

A reader who weights latency above power should choose the opposite arm and will be equally well
served: **BORE is a legitimate answer to this question, not a consolation prize.** That is the point
of publishing a frontier instead of a score.

**An advantage that no metric captures:** behind `-v`, pandemonium reports its own slice, batch
window, wake latency, p50/p99, retune count and adaptive estimate **every second**, at default
verbosity, where the default build prints none of it. Neither cake (attach/detach only) nor BORE
(nothing at all) offers anything comparable. Observability is not throughput, but it is why the
learning-layer question in §9 could be answered at all.

## 8. The mechanism — clock rate, not CPU time

"It uses less power" is not a mechanism. The probe that found it samples `scaling_cur_freq` on all 24
CPUs ~31 times per window (`tools/scx_freq_probe.py`), because a single end-of-slot reading is
worthless — one arm was observed moving from 1712 to 4469 MHz *within one slot*.

| window | arm | mean clock | **max clock** |
|---|---|---|---|
| `idle` | BORE | 862 MHz | **2820 MHz** |
| `idle` | pandemonium | 887 MHz | **2289 MHz** |
| `reclaim` | BORE | 1008 MHz | **5095 MHz** |
| `reclaim` | pandemonium | 917 MHz | **3288 MHz** |

During reclaim, **BORE turbos the Zen 5 fast cores to ~5.1 GHz** — against their 5157 MHz ceiling —
while **pandemonium never exceeds ~3288 MHz, which is the Zen 5c ceiling almost exactly**. Same
pattern at idle. The energy gap is therefore a **clock-rate effect**: comparable elapsed time, very
different turbo behaviour on the four fast cores.

The independent Pass B slot averages point the same way, from a different instrument (per-CPU tick and
frequency counters rather than sampling):

| arm | mean clock, fast cluster | mean clock, dense cluster |
|---|---|---|
| cake (control) | 2106 MHz | 903 MHz |
| BORE | 1953 MHz (−7.3%) | 1002 MHz (+10.9%) |
| pandemonium | **782 MHz (−62.9%)** | 1522 MHz (+68.6%) |
| `pandemonium --no-adaptive` | 1135 MHz (−46.1%) | 1781 MHz (+97.2%) |

The signature is unambiguous: pandemonium keeps work off the fast cluster and off high clocks, and
pushes it onto the dense cluster. That it does this **while tying BORE exactly on throughput** is the
substantive finding — the work still gets done, at a lower power state.

Two caveats, stated plainly: the frequency probe's own RAPL reads returned 0 because it ran
unprivileged, so **all energy figures in §6.1 come from the deep pass, not from this probe**; and
these are whole-slot averages including idle time, so the absolute MHz values are not comparable
across passes.

## 9. The learning layer: real, observable — and inert here

`scx_pandemonium` ships an adaptive control loop, and it makes a testable claim: it should do better
as it learns. That claim was tested two ways, and the answers differ.

### 9.1 The mechanism demonstrably acts

`pandemonium -v` emits a per-second telemetry line that the default build hides completely. Over a
~70 s probe of varied load (saturate → interactive burst → wakeup storm → idle), the tuner moved:

| field | start | peak | final |
|---|---|---|---|
| `slice` | 999 µs | **1398 µs** | 1114 µs |
| `batch` | 10000 µs | **28203 µs** | 15000 µs |
| `chaos: lam` (oscillator) | 0.00 | **2.75** | 2.75 |
| `frozen (n=)` (retunes recorded) | 0 | — | **9** |
| `retune_iv` | 2 [240] | — | **6 [24276]** |
| `graph: n=` (live estimate) | 0.0 | **0.932** | 0.401 |

Note that the tuner moves *and then partly returns* — slice 999 → 1398 → 1114, batch 10000 → 28203 →
15000. A static configuration cannot do that; this is a control loop responding to a changing load,
and it is the direct evidence that the adaptive layer is not decorative.

The control run makes it sharper: **`--no-adaptive` reports a strict subset of the same line**, with
none of `slice`, `batch`, `p99`, `sleep: io`, `rescue`, `chaos`, `frozen`, `retune_iv` or `graph`
present at all, and every line tagged `[BPF]` rather than showing oscillator state. The fields are not
merely uninformative without adaptivity — they do not exist.

### 9.2 …and produces no measurable benefit here

- **Adaptive ≡ `--no-adaptive` on every metric measured** — six workload classes, two cycles, n = 11–12.
  The single statistically significant latency difference (`light` p99, 2476 µs adaptive vs 2288 µs
  static, p = 0.000) **favours the static build**.
- **No within-slot learning either.** `rate_gain`, the cycle-2-minus-cycle-1 change, was
  −0.0008 / −0.0003 / −0.0035 for BORE / pandemonium / `--no-adaptive`, all p ≥ 0.236. Cycle 2 was no
  better than cycle 1 anywhere.
- **The one place the two builds visibly differ is frequency, not outcome.** Adaptive pandemonium ran
  the fast cluster at 782 MHz against the static build's 1135 MHz — a 31% difference in *how* it
  achieved almost identical energy (13.33 W vs 13.35 W) and identical throughput. The tuner changes
  the route, not the destination.

**Conclusion.** The adaptive claim is **mechanically substantiated** — the tuner demonstrably retunes
in response to load — and **behaviourally inert on this machine and these workloads**. That is a
carefully bounded statement: it does not say the learning layer is useless, and it does not test
workloads the battery does not represent. It says that on six workload classes, exercised repeatedly,
with the layer on and off, no benefit was measurable.

Note that this null is *informative* here precisely because §6 crossed workload classes and the
telemetry in §9.1 proves the tuner was active throughout. The earlier steady-state null was not.

## 10. Defect report — `scx_cosmos` cannot attach on `PREEMPT_RT`

`scx_cosmos` is disqualified from this comparison, and the disqualification is a finding rather than a
measurement.

**Observed.** `scx_cosmos` fails to attach **5 times out of 5**, across three different
configurations (shipped default, `-c 50`, and the loader's Gaming mode):

```
runtime error (ops.init_task() failed (-12) for ksoftirqd/0[15]) on CPU 16
... 5 attempts ... → state=disabled
```

`-12` is `-ENOMEM`. The failing task differs between runs — one attempt blamed
`kworker/R-mm_pe[14]`, and the CPU varies — so **`ksoftirqd/0` is positional, not special**: it is
simply whichever task the failing CPU happened to be starting.

**Identified cause, by elimination.** `cosmos_init_task` has exactly three `-ENOMEM` return sites:
BPF task storage, `scx_pmu_task_init`, and `init_cpumask` → `bpf_cpumask_create()`. The last is the
only one with a suspicious allocator: the kernel's BPF cpumask allocator is served **from a per-CPU
free list with no fallback to the page allocator**, starts with a prefill of 1–4 against a low
watermark of 32, and defers refills to an `irq_work`. On a `PREEMPT_RT` kernel that `irq_work` runs in
a **per-CPU kthread**, so a tight, lock-holding enable loop can outrun the refill. Free RAM is
irrelevant — the allocator never reaches the page allocator.

**This is a defect in the interaction between the scheduler and RT, not a tuning problem.** Nothing is
fixable locally, and it is worth stressing that it is not a benchmark result: `scx_cosmos` was never
measured, because it never ran.

**Stated honestly: the cause is identified by elimination, not by direct observation.** The decisive
test is a one-line patch that distinguishes the three return sites — change them to return `-ENOMEM`,
`-ENOMEM` and `-EAGAIN` respectively, and see which one fires. If it is the third, the diagnosis is
confirmed. Two lesser observations from the same investigation, both cosmetic:

- a "Performance counters configured successfully" line is printed even when counters are disabled;
- a `&& cpu == 0` condition swallows per-CPU setup failures, so only CPU 0's error is ever reported.

Searches found no existing upstream report of this failure.

## 11. Threats to validity

**Method faults found and fixed during this work** — each is documented because each one produced a
wrong answer first (full details in
[`METHODOLOGY.md` §8](../../METHODOLOGY.md#8-the-confound-catalogue)):

1. **Display blanking (~13 W)** voided the first idle-power comparison outright — larger than any
   scheduler effect. Fixed by recording backlight on both sides of every phase and re-running.
2. **First-slot contamination** made BORE's idle energy read 88–124 J against a ~29 J baseline in
   *both* deep runs, because rotation always placed BORE first. Repetition 0 is now excluded by
   default. This artifact had previously produced a bogus "BORE draws 32% more idle power" claim.
3. **A control that did not replicate.** `cyclictest --policy=fifo` is an invariant control —
   sched_ext only schedules `SCHED_NORMAL`/`BATCH`/`IDLE`, so a FIFO thread is outside the scheduler
   under test. It separated dramatically in Pass A (19 µs vs 189 µs) and **did not** in Pass B
   (both ~201 µs). The Pass A separation is therefore **discarded** and is not used anywhere above.
4. Two harness bugs caught by unit-checking the tooling: an attribute named `args.pass` (invalid
   Python) and a pooled `arm` key that silently merged the two pandemonium variants into one arm.

**Explicitly NOT claimed:**

- **`scx_pandemonium` has not been shown to run cooler under *sustained* load.** The saturating phase
  was 8 s — nowhere near thermal steady state — so at 17 W both arms tied. The power advantage is
  established for **light, bursty and reclaim-heavy** work only. The test that would close this is
  specified in §12 and was not run.
- **No claim about games, video calls, virtualisation or any interactive workload the battery does not
  represent.**
- **No claim that these results transfer to a non-hybrid CPU**, or to a kernel without `PREEMPT_RT`.
  §10 is direct evidence that RT × sched_ext is under-tested upstream.

## 12. Reproduce this

Everything below runs from a checkout; paths are relative and no personal configuration is assumed.
The harness needs `schbench`, `stress-ng`, `rt-tests` (`cyclictest`) and `hackbench`, and root for
RAPL and for `scxctl`.

```bash
# 1. What has the deep pass got to say, using the data in this repository?
python3 tools/scx_deep_analyse.py --passes deepA,deepA2        # all rows
python3 tools/scx_deep_analyse.py --bl 0 --passes deepA,deepA2  # display-stable only

# 2. Pass A / Pass B, cake as control, with the noise band printed alongside:
FSLAT_RESULTS=$PWD/data/scx-pass-ab.jsonl \
  python3 tools/perf-stats.py cmp passA:cake passA:bore passA:bpfland passA:pandemonium

FSLAT_RESULTS=$PWD/data/scx-pass-ab.jsonl \
  python3 tools/perf-stats.py cmp passB:cake passB:bore passB:pandemonium \
      'passB:pandemonium[--no-adaptive]'

# 3. Re-run the measurement itself on your own machine:
sudo python3 tools/scx_ab.py run --pass passA --reps 5 \
     --arms bore cake bpfland pandemonium --out data/scx-pass-ab.jsonl

sudo python3 tools/scx_deep.py run --pass deepA --reps 4 --out data/scx-deep.jsonl
```

Before spending an hour on a full pass, run **`tools/scx-w0-attach.sh`** — it is a feasibility gate
that attaches each candidate scheduler in turn and verifies it actually stayed attached. It is the
cheapest way to find out whether a scheduler works on your kernel at all; on the test bed it is what
caught `scx_cosmos` before it could pollute a measurement pass.

**To close the one open claim** (§11), the sustained-thermal run is: hold a saturating load for
**10 minutes per arm**, sampling `k10temp` Tctl, the iGPU temperature, RAPL package energy and
per-CPU clocks at 1 Hz, then compare steady-state temperature, power and sustained throughput. The
8-second phase in §6.1 is deliberately not that experiment.

## 13. Data files

| File | Rows | Contents |
|---|---|---|
| `data/scx-pass-ab.jsonl` | 38 | Pass A (20) and Pass B (18), one row per slot, with receipts |
| `data/scx-deep.jsonl` | 288 | deep pass, 3 arms × 4 reps × 12 phases × 2 passes |
| `data/scx-freq-probe.jsonl` | 12 | per-window clock distributions and energy |
| `data/traces/*.jsonl` | 24 | 1 Hz RAPL / backlight / CPU trajectories, one per deep slot |
| `data/analysis/*.txt` | — | analyser output, regenerated from the files above |
| `data/logs/scx-*.log` | — | run logs, including per-slot validity and timing |
| `data/probes/scx-w0*.txt` | — | the attach feasibility gate and the cosmos characterisation |
| `data/probes/scx-pandemonium-verbose.txt` | — | the raw `-v` telemetry behind §9 |

Schema and invariants are in [`data/README.md`](../../data/README.md).
