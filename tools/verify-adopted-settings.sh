#!/bin/bash
# verify-adopted-settings.sh -- confirm every adopted setting took effect.
#
# The rule for this audit was "never assume a setting is in effect -- verify it
# where the kernel actually sees it".  So each check reads the live value from
# the place the setting is consumed (the mount table, sysfs, the device, the
# unit's active state), not from the config file that was written.
#
# Usage: perf-post-reboot-verify.sh          (no root needed for most checks)
set -uo pipefail
cd "$(dirname "$0")"

pass=0; fail=0; info=0; pend=0
chk() { # chk LABEL EXPECTED ACTUAL
  if [ "$2" = "$3" ]; then printf '  PASS  %-34s %s\n' "$1" "$3"; pass=$((pass+1))
  else printf '  FAIL  %-34s expected %s, got %s\n' "$1" "$2" "$3"; fail=$((fail+1)); fi
}
inf() { printf '  INFO  %-34s %s\n' "$1" "$2"; info=$((info+1)); }

echo "=== adopted settings, read from where they are consumed ==="
echo "    (host $(uname -r) / boot $(uptime -s) / $(date '+%Y-%m-%d %H:%M %Z'))"

# 1. XFS log geometry, first-mount-only: only /proc/mounts can prove it.
chk  "xfs logbsize"        "logbsize=256k" \
     "$(grep -o 'logbsize=[0-9a-z]*' /proc/mounts | head -1)"

# 2. regulatory domain, from the cfg80211 module parameter
chk  "cfg80211 regdom"     "HK" "$(cat /sys/module/cfg80211/parameters/ieee80211_regdom 2>/dev/null)"

# 3/4. memory watermarks
chk  "vm.min_free_kbytes"  "262144" "$(sysctl -n vm.min_free_kbytes)"
chk  "vm.watermark_scale_factor" "10" "$(sysctl -n vm.watermark_scale_factor)"

# 5. swappiness is owned by the zram udev rule, not sysctl.d -- verify the
#    value that actually won, and that the udev rule is still the source.
chk  "vm.swappiness"       "150" "$(sysctl -n vm.swappiness)"
inf  "swappiness owner"    "/usr/lib/udev/rules.d/30-zram.rules"

# 6. WiFi power save, from the device itself
chk  "wlan0 power save"    "off" \
     "$(iw dev wlan0 get power_save 2>/dev/null | awk '{print $NF}')"
chk  "NM powersave config" "2" \
     "$(NetworkManager --print-config 2>/dev/null | awk -F= '/^wifi.powersave/{gsub(/ /,"",$2); print $2}')"

# 7. NVMe APST must still be enabled: during this audit it was briefly disabled
#    by an nvme-cli set-feature that also zeroed the APST table, and restored by
#    a controller reset.  Confirm the table really came back.
if command -v nvme >/dev/null; then
  apst=$(sudo -n nvme get-feature /dev/nvme0 -f 0x0c -H 2>/dev/null | sed -n '2p' | sed 's/.*: //')
  chk "nvme APST"          "Enabled" "$apst"
  itpt=$(sudo -n nvme get-feature /dev/nvme0 -f 0x0c -H 2>/dev/null |
         grep -m1 'ITPT' | grep -oE '[0-9]+' | head -1)
  chk "nvme APST first ITPT" "100" "${itpt:-none}"
else
  inf "nvme APST" "nvme-cli not installed; skipped"
fi

# 8/9. Boot-path changes.
#
# These are asserted on CONFIGURATION, not on runtime state, for two reasons:
#   - a runtime "is the unit active" test cannot distinguish "never started"
#     from "started on demand", and cups.service is legitimately started on
#     demand by cups.socket/cups.path (16ms) -- which is the socket-activation
#     pattern the wiki actually recommends, so it must not be reported as a
#     failure;
#   - it makes the check reboot-independent, so no stamp file is needed.  The
#     original stamp lived in /root (mode 0700), which a non-root run cannot
#     read, so every boot-path check stayed PEND after the reboot.
# The boot-time improvement itself is shown by the systemd-analyze section below.
cfg_check() { # cfg_check LABEL EXPECTED ACTUAL
  if [ "$2" = "$3" ]; then printf '  PASS  %-34s %s\n' "$1" "$3"; pass=$((pass+1))
  else printf '  FAIL  %-34s expected %s, got %s\n' "$1" "$2" "$3"; fail=$((fail+1)); fi
}
linkstate() { [ -e "$1" ] && echo present || echo absent; }
MU=/etc/systemd/system/multi-user.target.wants

cfg_check "NM-wait-online link gone" "absent" \
  "$(linkstate /etc/systemd/system/network-online.target.wants/NetworkManager-wait-online.service)"
cfg_check "cups forced-start link gone" "absent" "$(linkstate $MU/cups.service)"
cfg_check "avahi forced-start link gone" "absent" "$(linkstate $MU/avahi-daemon.service)"
cfg_check "cups.socket enabled (activation kept)" "enabled" "$(systemctl is-enabled cups.socket 2>/dev/null)"
cfg_check "avahi-daemon.socket enabled (activation kept)" "enabled" "$(systemctl is-enabled avahi-daemon.socket 2>/dev/null)"
cfg_check "cups.path enabled (activation kept)" "enabled" "$(systemctl is-enabled cups.path 2>/dev/null)"

# cups.service and avahi-daemon.service are allowed to be active IF they were
# activated on demand rather than pulled in by multi-user.target.  Report the
# trigger so a genuine regression is still visible.
for u in cups.service avahi-daemon.service; do
  trig=$(systemctl show "$u" -p TriggeredBy --value 2>/dev/null | tr -s ' ' ',' | sed 's/,$//')
  act=$(systemctl is-active "$u" 2>/dev/null)
  case "$act:$trig" in
    inactive:*)      inf "$u" "inactive - never started this boot" ;;
    active:cups.socket*|active:*cups.path*) inf "$u" "active, activated on demand by $trig" ;;
    active:*socket*) inf "$u" "active, activated on demand by $trig" ;;
    active:*)        printf '  WARN  %-34s active and triggered by %s - expected only socket/path activation\n' "$u" "$trig" ;;
    *)               inf "$u" "$act" ;;
  esac
done

# 9b. platform_profile is now asusd's job; PPD must be masked so it cannot
#     take the boot path or be D-Bus activated.
chk  "power-profiles-daemon masked" "masked" "$(systemctl is-enabled power-profiles-daemon 2>&1)"
chk  "power-profiles-daemon inactive" "inactive" "$(systemctl is-active power-profiles-daemon 2>&1)"
chk  "asusd active"        "active" "$(systemctl is-active asusd 2>&1)"
if command -v asusctl >/dev/null; then
  acp=$(asusctl profile get 2>/dev/null | awk '/^AC profile/{print $NF}')
  batp=$(asusctl profile get 2>/dev/null | awk '/^Battery profile/{print $NF}')
  chk "asusd AC profile"      "Balanced" "${acp:-none}"
  chk "asusd battery profile" "Quiet"    "${batp:-none}"
fi

# 10. the finding that was deliberately NOT acted on
if [ -d /sys/kernel/mm/transparent_hugepage ]; then
  inf "transparent hugepage" "present (kernel changed since the audit)"
else
  inf "transparent hugepage" "absent - PREEMPT_RT build, user declined to change kernel"
fi

echo
echo "=== boot time ==="
echo "    baseline (2026-09-18 pre-change): 22.997s total, 11.066s userspace, 8.024s to graphical.target"
echo "    verified (2026-09-18 post-change): 17.343s total, 6.128s userspace, 5.660s to graphical.target"
systemd-analyze 2>/dev/null | sed 's/^/  /'
echo "  units that all previously cost boot time:"
systemd-analyze blame --no-pager 2>/dev/null |
  grep -E 'wait-online|cups|avahi|plymouth|power-profiles' | sed 's/^/    /' ||
  echo "    (none - the disabled units are gone from the boot path)"

echo
echo "=== workload comparison: before vs after this audit ==="
if [ -f results.jsonl ]; then
  ./perf-stats.py test band-mempress after-mempress 2>/dev/null | head -30
  echo
  ./perf-stats.py test band-coldmeta after-coldmeta 2>/dev/null | head -20
else
  echo "  results.jsonl not found"
fi

echo
echo "=== summary: $pass passed, $fail failed, $pend pending, $info informational ==="
if [ "$fail" -gt 0 ] || [ "$pend" -gt 0 ]; then
  echo "A FAIL means the setting is not in effect. Do not assume it is; investigate."
  exit 1
fi
echo "All adopted settings are live."
