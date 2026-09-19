#!/bin/bash
# wl.sh -- named, repeatable filesystem workloads for the fs-latency harness.
#
# All generated data lives under $BENCH_SCRATCH (default ~/.cache/bench-scratch) and every
# destructive path is guarded, so a mistyped argument cannot delete anything else.
set -euo pipefail

WS="${BENCH_SCRATCH:-$HOME/.cache/bench-scratch}"
mkdir -p "$WS"
TREE="$WS/tree"          # cold read / metadata source
RMTREE="$WS/rmtree"      # regenerated before each timed unlink
EXTRACTED="$WS/extracted"
TARBALL="$WS/tree.tar"

NFILES_TREE=${NFILES_TREE:-30000}
NDIRS_TREE=${NDIRS_TREE:-300}
NFILES_RM=${NFILES_RM:-24000}
NDIRS_RM=${NDIRS_RM:-160}

guard() {
  case "$1" in
    "$WS"/*) ;;
    *) echo "wl.sh: refusing to operate on '$1' (outside $WS)" >&2; exit 1 ;;
  esac
}

# Create NFILES 4 KiB files spread round-robin over NDIRS directories, so
# consecutive allocations land in different directories/groups ("scattered").
gen_tree() {
  local dir=$1 nfiles=$2 ndirs=$3
  guard "$dir"
  rm -rf -- "$dir"
  mkdir -p -- "$dir"
  local d
  for ((d = 1; d <= ndirs; d++)); do mkdir -p -- "$dir/d$d"; done
  head -c 4096 /dev/urandom > "$dir/.seed.bin"
  local per=$(( (nfiles + ndirs - 1) / ndirs )) i
  for ((i = 1; i <= per; i++)); do
    for ((d = 1; d <= ndirs; d++)); do
      cp -- "$dir/.seed.bin" "$dir/d$d/f$i.bin"
    done
  done
  rm -f -- "$dir/.seed.bin"
  echo "gen_tree: $(find "$dir" -type f | wc -l) files in $ndirs dirs under $dir"
}

case "${1:-}" in

gen)
  case "${2:-}" in
    tree)   gen_tree "$TREE" "$NFILES_TREE" "$NDIRS_TREE" ;;
    rmtree) gen_tree "$RMTREE" "$NFILES_RM" "$NDIRS_RM" ;;
    all)    gen_tree "$TREE" "$NFILES_TREE" "$NDIRS_TREE"
            gen_tree "$RMTREE" "$NFILES_RM" "$NDIRS_RM" ;;
    *) echo "gen: expected tree|rmtree|all" >&2; exit 2 ;;
  esac
  ;;

mktar)
  guard "$TARBALL"
  rm -f -- "$TARBALL"
  tar -cf "$TARBALL" -C "$WS" tree
  ls -l --block-size=M "$TARBALL"
  ;;

# --- timed workloads -------------------------------------------------------

rm)
  guard "$RMTREE"
  rm -rf -- "$RMTREE"
  ;;

# Parallel deletion across top-level directories.  GNU rm is single-threaded,
# and measurement showed the cold walk runs at queue depth ~1, ~95us per round
# trip, with the device only ~55% busy -- so the serial walk, not the device,
# is the limit.  Same total I/O, less wall time, no extra energy.
rmpar)
  guard "$RMTREE"
  find "$RMTREE" -mindepth 1 -maxdepth 1 -type d -print0 |
    xargs -0 -P "${2:-8}" -n 1 rm -rf
  rm -rf -- "$RMTREE"
  ;;

extract)
  guard "$EXTRACTED"
  mkdir -p -- "$EXTRACTED"
  tar -xf "$TARBALL" -C "$EXTRACTED"
  sync
  ;;

coldmeta)
  dir=${2:-$TREE}
  find "$dir" -type f -exec stat -c %s {} + > /dev/null
  ;;

coldread)
  tar -cf /dev/null -C "$WS" tree
  ;;

# Large sequential write driven to durability.  The final sync is inside the
# timed window on purpose: that is what exposes bursty writeback, which is what
# a desktop actually feels as a stall.
wbig)
  dd if=/dev/zero of="$WS/big.bin" bs=1M count="${2:-4096}" status=none
  sync
  ;;

# Real browser launch: full start + render + exit, reusing one warm profile so
# only the program files go cold -- exactly the "launch after heavy work" case.
launchzen)
  prof="$WS/zenprof"; shot="$WS/zenprof-shot.png"
  guard "$prof"
  if [ ! -f "$prof/.warm" ]; then
    mkdir -p -- "$prof"
    timeout 120 zen-browser --headless --no-remote -profile "$prof" \
      --screenshot "$shot" about:blank > /dev/null 2>&1 || true
    : > "$prof/.warm"
  fi
  timeout 120 zen-browser --headless --no-remote -profile "$prof" \
    --screenshot "$shot" about:blank > /dev/null 2>&1 || true
  ;;

# Cold read of a real application's file set: same syscall mix as an app
# launch (many small openat/read/statx) without opening a window.
coldapp)
  dir=${2:?coldapp needs a directory}
  tar -cf /dev/null -C "$(dirname "$dir")" "$(basename "$dir")"
  ;;

# Fill page cache with unrelated data until ~$1 GiB has been read, evicting
# whatever was cached before -- this is the "after heavy work" state.
# The headline test: does a browser launch stay snappy while a large write is
# in flight?  Starts a big write in the background, then times a real cold
# browser launch against it.  PROBE line on stderr carries the launch latency.
launchunderload)
  guard "$WS/zenprof"
  guard "$WS/big.bin"
  dd if=/dev/zero of="$WS/big.bin" bs=1M count="${2:-8192}" status=none &
  wpid=$!
  sleep "${3:-2}"
  t0=$(date +%s%N)
  timeout 120 zen-browser --headless --no-remote -profile "$WS/zenprof" \
    --screenshot "$WS/zenprof-shot.png" about:blank > /dev/null 2>&1 || true
  t1=$(date +%s%N)
  printf 'PROBE launch_under_load_ms=%s\n' "$(( (t1 - t0) / 1000000 ))" >&2
  wait "$wpid"
  sync
  ;;

# Sustained metadata write storm: N extractions of the 30k-file tarball.  Long
# enough for the periodic XFS log-force interval to matter, which the ~1s
# single-extract workload is too short to show.
mkstorm)
  n=${2:-25}
  guard "$EXTRACTED"
  mkdir -p -- "$EXTRACTED"
  for ((i = 1; i <= n; i++)); do
    mkdir -p -- "$EXTRACTED/$i"
    tar -xf "$TARBALL" -C "$EXTRACTED/$i"
  done
  sync
  ;;

# Create real memory pressure with an anonymous-memory balloon, then time a
# cold browser launch.  swappiness decides whether reclaim takes from that
# anonymous memory (compress into zram, file cache survives) or from page
# cache (browser libs evicted, launch must re-read from disk).  This is the
# only scenario in which the swappiness setting has a mechanism to act.
launchafteranon)
  guard "$WS/zenprof"
  stress-ng --vm 1 --vm-bytes "${2:-8G}" --vm-keep --vm-hang 0 \
            -t "${3:-6}s" --quiet || true
  t0=$(date +%s%N)
  timeout 120 zen-browser --headless --no-remote -profile "$WS/zenprof" \
    --screenshot "$WS/zenprof-shot.png" about:blank > /dev/null 2>&1 || true
  t1=$(date +%s%N)
  printf 'PROBE launch_after_anon_ms=%s\n' "$(( (t1 - t0) / 1000000 ))" >&2
  ;;

# Deterministic memory pressure.  Warms a known amount of page cache, then
# inflates an anonymous balloon large enough to force reclaim on a 29 GiB box.
# With 74.9 G of zram swap the kernel has a real choice between evicting file
# pages and compressing anonymous pages -- which is exactly what swappiness and
# the watermark knobs govern.  Self-contained, so repeats are comparable.
mempress)
  gib=${2:-26}
  secs=${3:-8}
  guard "$WS/big.bin"
  dd if=/dev/zero of="$WS/big.bin" bs=1M count=2048 status=none
  dd if="$WS/big.bin" of=/dev/null bs=1M status=none
  stress-ng --vm 1 --vm-bytes "${gib}G" --vm-keep --vm-hang 0 \
            -t "${secs}s" --quiet || true
  sync
  ;;

# A frozen list of real files, used by the wake instruments below.  Generated
# once so every arm stats the exact same paths in the exact same order;
# anything less and the arms would not be comparable.
mklist)
  guard "$WS/filelist.txt"
  # sed rather than head: with `set -o pipefail`, head closing the pipe makes
  # sort die of SIGPIPE (141) and takes the whole script with it.
  find /usr/share -type f 2>/dev/null | LC_ALL=C sort \
    | sed -n "1,${2:-5000}p" > "$WS/filelist.txt"
  echo "mklist: $(wc -l < "$WS/filelist.txt") paths in $WS/filelist.txt"
  ;;

# A minimal cold metadata burst.  Deliberately tiny: with only N requests, a
# single NVMe power-state wake is a large fraction of the average, which is the
# only way to see APST wake latency at all.  Averaged over the 27k requests of
# a full coldmeta walk, a 6ms wake disappears into the noise.
wakeburst)
  n=${2:-16}
  list="$WS/filelist.txt"
  guard "$list"
  head -n "$n" "$list" | tr '\n' '\0' | xargs -0 -r stat -c %s > /dev/null
  ;;

# The case APST could actually hurt: a bursty-but-sparse pattern with gaps
# shorter than the drive's idle-to-transition threshold.  ITPT is 100ms by
# default, so a 150ms gap lets the drive drop to PS3 and every cycle pays the
# wake; a 20ms gap does not.  Comparing the two isolates the transition cost
# from the cost of the I/O itself.
sparse)
  n=${2:-100}
  gap=${3:-0.15}
  list="$WS/filelist.txt"
  guard "$list"
  for ((i = 1; i <= n; i++)); do
    sed -n "${i}p" "$list" | tr '\n' '\0' | xargs -0 -r stat -c %s > /dev/null
    sleep "$gap"
  done
  ;;

pressure)
  budget_gib=${2:-20}
  guard "$WS/big.bin"
  find "$HOME/Workspace" "$HOME/Documents" "$HOME/Downloads" \
       -type f -size +4M -print0 2>/dev/null |
  while IFS= read -r -d '' f; do
    sz=$(stat -c %s "$f" 2>/dev/null) || continue
    cat "$f" > /dev/null 2>&1 || continue
    bytes=$(( ${bytes:-0} + sz ))
    [ "$bytes" -ge $(( budget_gib * 1073741824 )) ] && break
  done
  ;;

*)
  echo "usage: wl.sh {gen|mktar|rm|extract|mkstorm|coldmeta|coldread|coldapp|launchzen|launchunderload|launchafteranon|rmpar|wbig|mklist|wakeburst|sparse|mempress|pressure}" >&2
  exit 2
  ;;
esac
