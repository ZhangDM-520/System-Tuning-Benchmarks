#!/bin/bash
# band.sh -- the definitive noise band for this audit.
#
# Every instrument this audit leans on, run at its control setting with enough
# repetitions to state its own spread.  Nothing here is a treatment; this is
# the yardstick that treatments must beat.
#
# Instruments are chosen for a high ratio of device I/O to CPU so that storage
# latency dominates the wall time:
#
#   coldmeta    /usr/share -- 373k files, stable, system-managed, on the same
#               XFS root.  ~27k cold read IOs over ~4s at ~103us per trip.
#   wake        the same walk, but after 20s of device idleness so the NVMe
#               controller has had time to enter whatever power state APST
#               permits.  This is the instrument that can see wake latency.
#   coldrm      24k scattered files, regenerated untimed and then cache-dropped
#               so the unlink walk is genuinely cold.
#   wbig        4 GiB sequential write driven to durability with sync.
#   launchload  real browser launch while an 8 GiB write is in flight.
#   mkstorm     sustained metadata write storm (6 x 30k-file extraction).
#   mempress    deterministic reclaim: anonymous balloon on top of warm cache.
#
# Usage: band.sh [instrument ...]     (default: all)
set -euo pipefail
cd "$(dirname "$0")"

PICK=("$@")
want() { [ ${#PICK[@]} -eq 0 ] && return 0; local i; for i in "${PICK[@]}"; do [ "$i" = "$1" ] && return 0; done; return 1; }

PREFIX=${BAND_PREFIX:-band}

run() { # run LABEL REPS -- extra-fslat-args... -- workload
  local label=$1 reps=$2; shift 2
  local extra=()
  while [ "$1" != "--" ]; do extra+=("$1"); shift; done
  shift
  echo
  echo "############ $label  reps=$reps"
  ./fslat.py run --label "${PREFIX}${label#band}" --note "noise band" --repeat "$reps" \
    "${extra[@]}" -- "$@" 2>&1 | tee -a "${PREFIX}.log" | tail -6
}

want coldmeta   && run band-coldmeta    5 --drop        -- ./wl.sh coldmeta /usr/share
want wake       && run band-wake        4 --drop --sleep 20 -- ./wl.sh coldmeta /usr/share
want coldrm     && run band-coldrm      3 --drop --pre "./wl.sh gen rmtree" -- ./wl.sh rm
want wbig       && run band-wbig        5 --drop        -- ./wl.sh wbig 4096
want launchload && run band-launchload  6               -- ./wl.sh launchunderload 8192 2
want mkstorm    && run band-mkstorm     4 --drop --pre "rm -rf $PWD/extracted" -- ./wl.sh mkstorm 6
want mempress   && run band-mempress    5 --drop        -- ./wl.sh mempress 26 8

echo
echo "=== band complete"
