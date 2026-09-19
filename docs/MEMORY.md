# Project Memory

Durable rules, facts and decisions for this repository. Everything here was established by
inspection or by a decision; none of it should need to be re-derived.

This is a **public** repository. Nothing in `docs/` — or anywhere else here — may name a home
directory, a machine name, a user, a root UUID, an IP or MAC address, a credential, or a private
host log. Hardware identity is the product; personal identity is not.

Last reviewed: 2026-09-19.

## What this repository is

Three measurement campaigns — scheduler selection, boot and power, filesystem latency — published
together with the raw data that produced them, so the results can be checked and repeated rather
than believed. Prose and data are CC BY 4.0; `tools/` is MIT.

## Standing decisions

| Decision | Rationale |
| --- | --- |
| Every published number is **re-derived from the shipped data**, never transcribed | The prose was written from the output of the analysers re-run *inside this repository* over the migrated data. It makes each table reproducible from what ships, and it doubles as a check that the tooling still works in its new home. Three errors surfaced only because of it |
| Hardware identity stays, personal identity is stripped | CPU model, topology, kernel configuration and tunable snapshots are the product. Home paths, machine name, the root UUID and session framing are not |
| The study READMEs carry an explicit **"explicitly NOT claimed"** section | Stating the boundary is what makes the rest of a claim credible; both the scheduler and filesystem studies have one |
| `data/` is the artefact of record | Anything not in `data/` is either a byproduct or scratch, and both are gitignored |
| The measurement lab outside this repository is the **source**, not a copy | It was deliberately left untouched when this repository was built; do not "sync" it back |
| No CI and no build system | There is nothing to compile. The verification surface is running the analysers over `data/` and comparing against the published tables |

## Facts that are easy to get wrong

- **The test bed is time-stamped, and the host has moved on.** `TEST-BED.md` and study 01 name the
  measurement-time kernel: `7.2.5-1-cachyos-rt-bore-lto`, `#1 SMP PREEMPT_RT`, `sched_bore=1`. That
  is correct **for the campaign**, and the claims are scoped to it — study 01 explicitly declines to
  claim transfer "to a kernel without `PREEMPT_RT`", and study 02 leans on
  `CONFIG_TRANSPARENT_HUGEPAGE` being absent. A re-run on a different kernel is **not** a
  like-for-like reproduction; re-baseline first, and do not edit the historical figures to match a
  newer machine.
- **Several harness subcommands attach a scheduler to the live machine.** `tools/scx_ab.py arms`,
  `tools/scx_ab.py run`, `tools/scx_deep.py run` and `tools/scx_freq_probe.py` are **not read-only**,
  despite some of them printing a table. They restore the entry scheduler on every exit path
  including failure, and recovery resolves through `scxctl restore` to the loader's configured
  default rather than a hardcoded arm — a hardcoded `recover("cake")` once left a machine on the
  wrong scheduler. Audit a subcommand for side effects before running it on a host you care about.
- **The first repetition of a run is contaminated.** Bench activity at launch lands in whichever arm
  runs first, so repetition 0 is excluded and the tool's `--help` says so.
- **The panel blanking mid-run moves package power by more than any arm does** (~13 W). Record the
  ambient state — backlight, EPP, platform profile — into every row, or the comparison is measuring
  the display.
- **The battery is not an energy oracle on an AC-limited machine**, where `power_now` can read 0.
  RAPL is the oracle; it needs root.
- **A metric that fails to parse must be distinguishable from a metric that is zero.**
  `stress-ng --quiet` suppresses its `metrc:` lines and once silently zeroed a whole throughput axis.
- **`cpu_capacity` reads a flat 1024 on every CPU** of this hybrid part despite a ~1.57× clock gap
  between the two clusters. It is not a capacity signal; use `acpi_cppc/highest_perf` or
  `cpufreq/cpuinfo_max_freq` instead.
- **Two writers of one sysfs attribute is a silent race**, and each daemon is correct in isolation —
  the bug is only visible once you look for the second writer.

## Where things live

| Purpose | Path |
| --- | --- |
| Repository overview and headline results | `README.md` |
| Protocol, confound catalogue, what the method will not claim | `METHODOLOGY.md` |
| Machine specification at measurement time | `TEST-BED.md` |
| The three campaigns | `studies/01-scheduler-selection`, `studies/02-boot-and-power`, `studies/03-filesystem-latency` |
| Shared data and its dictionary | `data/README.md`, `data/*.jsonl`, `data/traces/`, `data/probes/`, `data/analysis/`, `data/logs/` |
| The harness | `tools/README.md` and `tools/` |
| Decisions, changes and incidents | `docs/NOTE.md` |

## Verification

There is no test suite. A published figure is verified by re-running the analyser over the shipped
data and comparing it with the table:

```bash
cd studies/01-scheduler-selection
python3 ../../tools/scx_deep_analyse.py ../../data/scx-deep.jsonl
```

A fresh clone needs no configuration: every tool takes repository-relative paths. Before publishing
anything, scan for personal identifiers rather than trusting the last pass:

```bash
grep -rInE '/home/|/Users/|UUID=[0-9a-f]{8}-' --exclude-dir=.git . | head
```

The only expected match is this command's own line in this file. Note that a naive `/root/` pattern is
useless here: the harness reads `/sys/kernel/sched_ext/root/ops`, which is a sysfs path, not a home
directory.
