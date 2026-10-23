#!/bin/bash
# lynx-net: the ROCK's LAN address, for Lynx-Tx's Network card.
# Installed as /usr/local/sbin/lynx-net (root's copy, so the sudoers rule
# that lets Lynx-Tx run it cannot be widened by editing the repository).
#
#   lynx-net dhcp
#   lynx-net static 192.168.0.50/24 192.168.0.1 192.168.0.1
#
# Works on the port that holds the default route (never the SDR's own
# port). A fixed address keeps the ROCK reachable when it starts before
# the router's DHCP server, after a power cut. The change is applied in the
# background so the page gets its answer first, and undone by itself if the
# gateway does not answer within 30 s (a typing slip cannot lock you out).
set -euo pipefail

ip4='([0-9]{1,3}\.){3}[0-9]{1,3}'
case "${1:-}" in
  dhcp) ;;
  static)
    [[ "${2:-}" =~ ^$ip4/[0-9]{1,2}$ && "${3:-}" =~ ^$ip4$ && "${4:-}" =~ ^$ip4$ ]] \
      || { echo "usage: lynx-net static ADDRESS/PREFIX GATEWAY DNS" >&2; exit 2; } ;;
  *) echo "usage: lynx-net dhcp | static ADDRESS/PREFIX GATEWAY DNS" >&2; exit 2 ;;
esac

dev=$(ip route show default | awk '{for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit }}')
[ -n "$dev" ] || { echo "no default route: cannot tell which port is the LAN" >&2; exit 1; }
con=$(nmcli -g GENERAL.CONNECTION device show "$dev")
[ -n "$con" ] && [ "$con" != lynx-sdr ] || { echo "no LAN connection on $dev" >&2; exit 1; }

old_m=$(nmcli -g ipv4.method connection show "$con")
old_a=$(nmcli -g ipv4.addresses connection show "$con")
old_g=$(nmcli -g ipv4.gateway connection show "$con")
old_d=$(nmcli -g ipv4.dns connection show "$con" | tr '|' ',')

if [ "$1" = dhcp ]; then
  set -- ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns ""
  gw=""
else
  gw=$3
  set -- ipv4.method manual ipv4.addresses "$2" ipv4.gateway "$3" ipv4.dns "$4"
fi
nmcli connection modify "$con" "$@"
echo "$con on $dev: $*"

(
  set +e
  sleep 1
  nmcli device reapply "$dev" || nmcli connection up "$con"
  ok=0
  for _ in $(seq 30); do
    sleep 1
    g=${gw:-$(ip route show default dev "$dev" | awk '{print $3; exit}')}
    if [ -n "$g" ] && ping -c 1 -W 1 "$g" >/dev/null 2>&1; then ok=1; break; fi
  done
  if [ "$ok" = 1 ]; then
    logger -t lynx-net "$con on $dev: $* (gateway answers)"
  else
    nmcli connection modify "$con" ipv4.method "$old_m" ipv4.addresses "$old_a" \
      ipv4.gateway "$old_g" ipv4.dns "$old_d"
    nmcli device reapply "$dev" || nmcli connection up "$con"
    logger -t lynx-net "$con on $dev: gateway did not answer, put back as it was ($old_m $old_a)"
  fi
) </dev/null >/dev/null 2>&1 &
disown
