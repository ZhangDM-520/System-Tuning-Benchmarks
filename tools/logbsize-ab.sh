#!/bin/bash
# logbsize-ab.sh -- settle the one item this audit could not settle in-session.
#
# XFS log geometry is honoured only at first mount, and the root filesystem is
# mounted by the initramfs from the kernel cmdline.  A runtime remount silently
# does nothing (proven in the earlier campaign), so an on-metal A/B of
# logbsize=256k vs 32k needs one reboot per arm.
#
# The earlier attempt to reuse historical data failed for a reason worth
# recording: the old 32k row was not cache-dropped (2,070 read IOs) while the
# new 256k row was (32,649 read IOs), so the comparison measured page cache,
# not log geometry.  This script always uses --drop so both arms are cold.
#
# Usage:
#   sudo logbsize-ab.sh arm            # run this boot's arm, append to the log
#   sudo logbsize-ab.sh flip           # switch the cmdline to the other value
#   sudo logbsize-ab.sh report         # compare whatever arms have been run
#
# Procedure: arm -> flip -> reboot -> arm -> report
set -uo pipefail
cd "$(dirname "$0")"
ARM_LOG=$PWD/logbsize-ab.jsonl
LIMINE=/etc/default/limine

current() { grep -o 'logbsize=[0-9a-z]*' /proc/mounts | head -1 | cut -d= -f2; }

case "${1:-}" in
arm)
  kb=$(current)
  echo "arm: measuring with logbsize=$kb (live), cache-dropped, 15x metadata storm"
  ./fslat.py run --label "logbsize-${kb}" --note "on-metal A/B, cold" \
    --drop --repeat 3 --pre "rm -rf $PWD/extracted" -- ./wl.sh mkstorm 15
  echo "appended to results.jsonl"
  ;;
flip)
  [ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
  kb=$(current)
  if [ "$kb" = 256k ]; then new=32k; else new=256k; fi
  cp -a "$LIMINE" "$LIMINE.bak-$(date +%Y%m%d%H%M%S)"
  sed -i "s/rootflags=logbsize=[0-9a-z]*/rootflags=logbsize=$new/" "$LIMINE"
  limine-update >/dev/null
  grep -m1 'KERNEL_CMDLINE\[default\]' "$LIMINE"
  echo "now reboot, then: sudo $0 arm    (value will become $new at mount time)"
  ;;
report)
  ./perf-stats.py show logbsize- 2>&1
  echo
  echo "Compare 'log FORCE', 'dev busy ms' and 'qmean us' between the arms."
  echo "log FORCE must drop several-fold at 256k; that is the mechanism.  If"
  echo "wall time and dev busy do not improve, roll back with the flip command."
  ;;
*)
  sed -n '2,20p' "$0"
  ;;
esac
