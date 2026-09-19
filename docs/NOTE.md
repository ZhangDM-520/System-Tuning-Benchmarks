# NOTE — session journal

Running log of work on this repository. Durable, reusable knowledge is mined out of here into
`docs/MEMORY.md`; this file keeps the chronology and the state of in-flight work.

**Convention:** newest entry first. One `## YYYY-MM-DD — topic` section per change or incident, each
saying what changed, why, and how it was verified. This is a public repository: no home paths, no
machine name, no credentials, no private host logs.

---

## 2026-09-19 — docs/ journals created, and the kernel drift recorded

### What changed

`docs/MEMORY.md` and `docs/NOTE.md` were added. The repository had no project memory: the standing
decisions behind it (re-derive every published number; strip personal identity but keep hardware
identity; `data/` as the artefact of record; the source lab left untouched) existed only in the
session that produced the publication, and a reader or a later maintainer had no way to recover them
without re-deriving the whole editorial line.

### The one fact worth recording on the way in

The publication describes a **`PREEMPT_RT` + BORE** test bed — `TEST-BED.md` and study 01 both name
`7.2.5-1-cachyos-rt-bore-lto` — and that remains the correct description *of the campaign*. The
machine has since been moved to a different kernel flavour. Nothing in the published data is
affected and no figure was edited, because the studies are explicitly scoped to the kernel they were
measured on: study 01 declines to claim transfer "to a kernel without `PREEMPT_RT`", and study 02
relies on `CONFIG_TRANSPARENT_HUGEPAGE` being absent. The consequence is for anyone re-running the
harness today — a re-run is a new baseline, not a reproduction. That is now written into
`docs/MEMORY.md` under "facts that are easy to get wrong" rather than left for someone to trip over.

### Verification

- Every path, tool name and claim in `docs/MEMORY.md` was checked against the tree (`TEST-BED.md`,
  `studies/01-scheduler-selection/README.md`, `tools/`, `data/`) rather than written from memory.
- The privacy scan in `docs/MEMORY.md` was run over the repository and returns nothing.
- `docs/` is not matched by `.gitignore`, so both files are tracked.
