#!/usr/bin/env python3
"""scx-relabel.py -- normalise result labels to "<pass>:<arm>[<args>]".

perf-stats.py selects rows by label, so every arm (and every distinct configuration) needs its
own label or the rank test cannot tell them apart. An early Pass A run was started before the
harness set per-arm labels, so its rows all carry the bare pass name; this fixes them in place
without touching any measurement.

Idempotent: rows already in the "<pass>:<arm>" form are left alone.
"""

import json
import sys


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        print("Usage: scx-relabel.py <path-to-row-file.jsonl>\n"
              "The path is required: this rewrites the file in place, so it will not\n"
              "guess a target.")
        return 0 if len(sys.argv) > 1 else 2
    path = sys.argv[1]
    rows, changed = [], 0
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            arm, args = r.get("arm", "?"), r.get("args") or ""
            lab = str(r.get("label", ""))
            want = lab if lab.split(":")[-1].split("[")[0] == arm else \
                f"{lab}:{arm}" + (f"[{args}]" if args else "")
            if want != lab:
                r["label"] = want
                changed += 1
            rows.append(r)

    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    seen = {}
    for r in rows:
        seen[r["label"]] = seen.get(r["label"], 0) + 1
    print(f"relabelled {changed}/{len(rows)} rows in {path}")
    for lab, n in sorted(seen.items()):
        print(f"  {lab:<34} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
