#!/bin/bash
# loopback-logbsize.sh -- prove how XFS treats logbsize, without touching the
# running root filesystem.  Run as root (uses sudo -n internally).
#
# Question under test: /etc/fstab requests logbsize=256k for /, but the live
# mount reports logbsize=32k.  Two candidate explanations:
#   (a) a remount cannot change logbsize, so the option set at first mount wins;
#   (b) XFS refined/rejected the requested value outright.
# This settles it on a throwaway image with the same feature set as /.
set -euo pipefail

IMG=/tmp/logbsize-probe.img
MNT=/tmp/logbsize-probe.mnt
# Mirrors the live root filesystem's mkfs feature set exactly.
MKFS_ARGS=(-f -q
  -m crc=1,finobt=1,rmapbt=1,reflink=1,bigtime=1,inobtcount=1
  -i sparse=1,nrext64=1,exchange=1
  -n ftype=1,parent=1
  -d agcount=4)
S='sudo -n'

cleanup() { $S umount "$MNT" 2>/dev/null || true; rm -rf -- "$MNT" "$IMG"; }
trap cleanup EXIT

rm -f -- "$IMG"; rm -rf -- "$MNT"; mkdir -p -- "$MNT"
truncate -s 2G "$IMG"
mkfs.xfs "${MKFS_ARGS[@]}" "$IMG"
echo "== probe image mirrors /'s feature set"

probe() { # probe <label> <mount options>
  local label=$1 opts=$2 got
  $S umount "$MNT" 2>/dev/null || true
  if ! $S mount -o loop,$opts "$IMG" "$MNT" 2>/dev/null; then
    echo "  $label: MOUNT FAILED for '$opts'"; return
  fi
  got=$(grep " $MNT " /proc/mounts | awk '{print $4}')
  printf '  %-34s -> %s\n' "$label" "$got"
  $S umount "$MNT"
}

echo "== A. what does each mount option produce at first mount?"
probe "no log options"                 "defaults"
probe "logbsize=256k"                  "logbsize=256k"
probe "logbufs=8,logbsize=256k"        "logbufs=8,logbsize=256k"
probe "logbsize=256k,logbufs=8"        "logbsize=256k,logbufs=8"

echo "== B. can a remount change logbsize afterwards?"
$S mount -o loop,logbsize=256k "$IMG" "$MNT"
echo "  at first mount : $(grep " $MNT " /proc/mounts | awk '{print $4}')"
if $S mount -o remount,logbsize=64k "$MNT" 2>/tmp/remount.err; then
  echo "  remount rc=0   : $(grep " $MNT " /proc/mounts | awk '{print $4}')"
else
  echo "  remount rc!=0  : $(cat /tmp/remount.err)"
  echo "  still          : $(grep " $MNT " /proc/mounts | awk '{print $4}')"
fi
$S umount "$MNT"

echo "== C. does rootflags= reach the first mount? (kernel cmdline path)"
echo "   live / currently reports: $(grep ' / ' /proc/mounts | awk '{print $4}')"
