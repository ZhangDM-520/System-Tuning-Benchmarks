# Methodology

Every number in this repository came out of the same protocol. This document is that protocol, and
— just as importantly — the list of ways it has already been fooled.

The short version: **measure before changing, interleave the arms, verify the thing you think you are
testing is actually loaded, treat the control's own spread as the first filter, and only then bring in
a rank test.** Most of the value in the studies is not the wins; it is the levers that measurement
refused to approve.

---

## 1. The unit of measurement: one slot, one arm

A **slot** is one arm held fixed for one run of a workload battery. Slots are the row unit in every
`*.jsonl` file here.

**Arms are interleaved inside every repetition, and the starting arm rotates.** Repetition 1 runs
arms A, B, C, D; repetition 2 runs B, C, D, A; and so on. This is the single most important design
decision in the repository, because a laptop is not a stable instrument:

- ambient/thermal state drifts monotonically over a session,
- the harness itself perturbs the machine when it starts,
- the display blanks on its own schedule (see §7.1),
- background daemons wake up when they feel like it.

Sequential blocks (all of A, then all of B) would attribute any of those to the arm. Interleaving
spreads them across arms. Rotation matters *as well*, because the first slot in a repetition is the
one that eats the harness's own startup cost — which is exactly the artifact described in §7.2.

## 2. Repetition count and `n`

- Screening passes: **n ≥ 3** per arm.
- Anything that will be **adopted** or **refuted**: **n ≥ 5** per arm, and often the passes were
  pooled to reach n = 10–12.
- The deep scheduler pass reported **n = 10–12 per arm per workload class** (2 passes × 4 reps × 2
  cycles, minus the excluded first repetition, minus regime-filtered rows).

Report the achieved `n` next to every claim. `n = 3` distinguishes a 2× effect; it does not
distinguish a 5% one, and the studies say so where it applies.

## 3. Arm-identity verification (do not skip this)

**The most dangerous failure mode is not noise — it is measuring the wrong thing and not knowing.**
Three separate incidents during this work produced confident numbers for an arm that was not loaded:

1. A scheduler restart returned exit code 0 while printing `error:` — and the previously loaded
   scheduler was still attached.
2. A scheduler failed to attach and left `state=disabled`, so the *next* arm's first slot measured
   the kernel's own scheduler instead.
3. A verdict of "SURVIVED" was recorded for a scheduler that had simply never detached.

The protocol therefore verifies the arm **twice, from two independent sources**:

| Source | What it proves |
|---|---|
| `/sys/kernel/sched_ext/root/ops` read back after the wait | the kernel agrees which scheduler is attached |
| The workload's own self-report — e.g. schbench's JSON emits `"sched_ext": "cake_1.2.1_x86_64_unknown_linux_gnu"` | the *process under measurement* was actually scheduled by that scheduler |

Every row in `data/scx-*.jsonl` carries both. A row whose two sources disagree is marked invalid.
Exit codes are never trusted; state is always read from sysfs.

The same principle generalises beyond schedulers, and it is the single most transferable lesson in
this repository: **read a setting back from where the consumer reads it, not from where you wrote
it.** Two settings on the test bed were silently dead for exactly this reason — a `logbsize=256k` in
`/etc/fstab` that the kernel ignored because XFS log geometry is first-mount-only, and a
`vm.swappiness=195` that a udev rule overwrote with `150` at every boot. Both looked configured.
Neither was.

## 4. Statistics: the control's own spread, then a rank test

Two gates, in order.

**Gate 1 — does the effect exceed the control's own repeatability?** For every metric the harness
prints `n`, `min`, `median`, `max` and `spread%` for the control label. An effect that lives inside
the control's own spread is not a result, regardless of how good it looks. This gate exists because
min/max spread is outlier-driven and would otherwise hide real shifts — but it is still the right
first filter, because it is computed from the control alone and cannot be flattered by the treatment.

**Gate 2 — Mann-Whitney U, tie-corrected, two-sided, p < 0.05.** Chosen over a t-test because
latency and energy metrics in this dataset are decidedly not normal: they are bounded below, heavily
right-skewed, and contain genuine outliers. A rank test asks the question that matters — *is one
arm's distribution shifted relative to the other's?* — without assuming a shape.

Percentages quoted in the studies are **medians relative to the control's median**, not means.
Medians are stable here; means are not.

**Multiple comparisons are handled by the adoption rule, not by a correction.** The campaign tests
~45 metrics per comparison, so individual p-values must be read with that in mind. The rule that
protects against this is in §6: nothing is adopted unless it separates **and** nothing gets
significantly worse. A spurious win in one metric out of 45 is very likely; a spurious win in one
metric *with no cost anywhere else* is much less so, and the studies prefer composite claims over
single-metric ones. Where a claim rests on one metric, the study says so.

## 5. Energy: RAPL is the oracle

- **Oracle:** `/sys/class/powercap/intel-rapl:0/energy_uj` (package domain), read as a difference
  across the slot. Root-only. Also recorded: the core subdomain, and per-phase where applicable.
- **Why not the battery:** this laptop runs on AC with an 80% charge cap, so `power_now` reads `0`
  and the battery counters are unusable as an energy oracle. Anyone reusing this harness on a
  discharge-capable machine can use the battery fields, which are recorded alongside.
- **Reported as:** median package joules per slot, mean watts, and — critically — **energy per unit of
  work** (J per hackbench message, J per bogo-op). Energy alone is not a result: doing less work
  faster often looks like a power win. Dividing by work is what makes the "no throughput conceded"
  claim in the scheduler study checkable.

## 6. The adoption rule

A change is adopted only when **all three** hold:

1. It separates from control at **p < 0.05** on at least one metric that matters for the workload in
   question.
2. **No** metric is significantly *worse*. A latency win paid for with an energy regression is not a
   win; it is a trade, and trades need to be stated as trades.
3. The mechanism is understood. "It is faster and we do not know why" is a prompt to keep measuring,
   not a result. Several levers in the boot/power study were rejected on this basis alone.

When **no** arm satisfies all three — which is the normal case for schedulers — the study reports the
**Pareto frontier** and states the weighting explicitly. See §7.

## 7. Two design guards

### 7.1 No benchmark-fitted tuning

Every configuration that was measured was justified by the scheduler's own documentation or CLI,
**before** the numbers were seen. Slice lengths were set to values the project itself documents
(including, deliberately, a competitor's value in order to test a causal hypothesis). Nothing was
tuned against the benchmark.

The reason is not purity; it is that a configuration chosen because it wins on this battery has an
unknown out-of-sample effect, and the study is meant to be usable by someone else. Where a
"scheduler at its best" arm exists, the study also reports what the *shipped default* does, so a
reader can tell packaging from design.

### 7.2 No hidden weighting

Given a Pareto frontier, "which is best" is a preference, not a measurement. The scheduler study
therefore does not publish a single score. It publishes the frontier, states which axis each arm
wins, gives the magnitude, and names the fallback behaviour — then states the weighting that was
chosen and why, as a separate, arguable claim rather than as a result.

**A corollary that got its own section in the scheduler study:** an axis can be real and still be
perceptually empty. A 2.5× improvement in wakeup p99 is a large factor; when both arms are below 3 ms,
it is not something anyone can feel, while a 30% energy difference in reclaim *is* felt continuously
as heat and fan noise. Reporting the factor without the absolute values invites the reader to weight
it wrongly, so both are always given.

## 8. The confound catalogue

Each of these was discovered by being fooled, and each has a measured magnitude. They are the most
reusable part of this document.

### 8.1 Display blanking — **~13 W**

The panel backlight stepping `300000 → 120000 → 0` moves package power by roughly **13 W**. That is
larger than any CPU-scheduling, filesystem or sysctl effect measured in this repository, combined. A
display that blanks mid-run will swamp the signal and look exactly like a power win for whichever arm
happened to be running.

*Mitigation:* every phase records the backlight level on **both sides** of the phase, and the
analyser can restrict to a single stable regime (`--bl 0` = panel off, `--bl any` = all rows). The
first idle-power comparison in the scheduler campaign was **voided** by this and had to be re-run.

### 8.2 First-slot contamination

Rotation puts a different arm first each repetition, but the machine's own startup activity — the
harness launching, the tooling loading, whatever the shell was doing — lands in whichever slot is
first. In the deep scheduler runs, `bore` ran first in every repetition and read **88–124 J of idle
energy against a ~29 J baseline**, in *both* independent runs. That artifact had earlier produced a
completely wrong published-sounding claim ("BORE draws 32% more idle power").

*Mitigation:* **repetition 0 is excluded by default** by the analyser (and this is stated in its
`--help`). A discarded warm-up slot belongs at the front of any future run.

### 8.3 Frequency sampled at the end of a slot is useless

`scaling_cur_freq` read once at the end of a slot says nothing about the slot: one arm was observed
moving from 1712 MHz to 4469 MHz *within a single arm*. Frequency must be sampled continuously and
summarised as a distribution.

*Mitigation:* the frequency probe samples all 24 CPUs ~31 times per window and reports mean, min and
max per window. This is what turned "pandemonium uses less energy" into "pandemonium never exceeds
the dense-cluster clock ceiling", which is a mechanism rather than a correlation.

### 8.4 Regime shifts between passes are not comparable

The display-blank event moved *both* anchors by roughly +4.6% between two passes. Consequently
**only within-pass comparisons are valid**; cross-pass deltas silently mix the treatment with the
regime change. The analyser supports explicit pass selection (`--passes`) precisely so this cannot be
done by accident.

### 8.5 A workload tool can silently delete your result

`stress-ng --quiet` suppresses its `metrc:` lines, so the harness parsed zero bogo-ops and the entire
throughput axis read as zero. It looked like a catastrophic regression; it was a flag.

*Mitigation:* the harness records raw tool output and checks for parse success; a metric that fails to
parse is not written as zero. In general: **a metric that cannot parse must be distinguishable from a
metric whose value is zero.**

### 8.6 Flat platform signals are misleading

On this SoC, `cpu_capacity` reads **1024 on all 24 CPUs** despite a 1.57× clock gap between clusters.
Any tool that treats `cpu_capacity` as a capacity signal — including scheduler heuristics — sees a
uniform machine. The real ranking lives in `acpi_cppc/highest_perf` (208 on the fast cluster, 125 on
the dense one) and `cpufreq/cpuinfo_max_freq` (5.16 GHz vs 3.29 GHz).

*Mitigation:* the analysis computes `fast_share` and `fast_excess` directly from per-CPU tick deltas,
rather than trusting platform capacity. `fast_excess = 1.0` means work landed exactly pro-rata.

### 8.7 The invariant control that did not replicate

`cyclictest --policy=fifo` is a useful control: sched_ext only ever schedules `SCHED_NORMAL`,
`SCHED_BATCH` and `SCHED_IDLE`, so a FIFO-priority thread is **outside the scheduler under test** and
should be identical across arms. A separation there is a bug in the setup, not a finding.

In one pass this control separated dramatically (19 µs vs 189 µs max). It did **not** replicate in the
next pass. The separation was therefore discarded rather than reported — a control that fires once
and not again is telling you about the run, not the arm.

## 9. What this methodology cannot establish

Stated here so the studies do not have to repeat it per claim:

- **Anything about sustained thermal steady state.** The longest continuous saturating phase was 8 s.
  That is nowhere near thermal equilibrium, so no sustained-load thermal or long-run clock claim is
  made anywhere in this repository.
- **Out-of-sample behaviour.** Every configuration was validated against the same battery of
  workloads it was screened on. Interactive desktop work that is not represented in the battery —
  games, video calls, virtualisation — is not covered.
- **Non-hybrid machines.** Several conclusions depend on the Zen 5 / Zen 5c split and on sched_ext
  behaviour under `PREEMPT_RT`. They should be expected to transfer to similar laptops and to need
  re-measurement elsewhere.
- **Causality from correlation alone.** Where a mechanism is given, it was measured (frequency
  distributions, per-phase counters). Where it was not, the study says "identified by elimination"
  and names the decisive experiment.

## 10. Tooling map

| Tool | Role |
|---|---|
| `tools/fslat.py` | row recorder: per-run deltas of PSI stalls, RAPL, NVMe queue latency, XFS counters, reclaim stats, plus a self-describing tunable snapshot |
| `tools/wl.sh` | named repeatable workloads (metadata storms, `rm` trees, extraction, cold reads, browser launch under load, memory balloon) |
| `tools/perf-stats.py` | `show` (noise band per label) / `cmp` (median delta vs control) / `test` (Mann-Whitney) |
| `tools/scx_ab.py` | scheduler A/B harness: arm switching, recovery, quiescence gate, workload battery, validity gating |
| `tools/scx_deep.py` | phase-structured scheduler harness (six workload classes × cycles, per-phase metrics, 1 Hz trace) |
| `tools/scx_deep_analyse.py` | per-phase medians + Mann-Whitney vs control, with regime filtering and repetition exclusion |
| `tools/scx_sampler.py` | root-only 1 Hz trace of RAPL, backlight and aggregate CPU |
| `tools/scx_freq_probe.py` | clock-distribution probe per window, the instrument that found the mechanism |
| `tools/verify-adopted-settings.sh` | reads every adopted setting back **from where its consumer reads it** |

Invocations and dependencies are in `tools/README.md`; the data dictionary is in `data/README.md`.
