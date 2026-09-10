#!/bin/bash
# Bypass per-SSID Wi-Fi client isolation so the Mac can reach LAN peers (the
# Apple TV) that the access point refuses to bridge station-to-station.
#
# The AP forwards Mac<->gateway but drops Mac<->peer unicast (ICMP passes, TCP
# gets EHOSTUNREACH). The fix: pin the peer's ARP entry to the GATEWAY's MAC, so
# frames to the peer go to the router, which hairpins (L3-forwards) them on to the
# peer — a path the AP does allow. Verified: 10/10 TCP to the Apple TV's Companion
# port through it (2026-06-25). The runbook is lan_bypass.README.md beside this file.
#
# Run as root by launchd (com.gigaku.lanbypass) at boot and every 30s. Idempotent:
# it only rewrites an entry that isn't already the gateway MAC, so it never churns
# a live connection.
#
# FINDING THE APPLE TV'S IP MUST NOT DEPEND ON mDNS. The same client isolation
# that breaks unicast also wedges multicast, so `Living-Room-….local` stops
# resolving for hours at a time (2026-07-03: dscacheutil returned nothing while
# the device sat happily in the ARP table). The device's MAC is baked into that
# hostname (…02ADBE0DEE01) and never changes, so the robust path is to grep the
# ARP/neighbour table for the MAC. mDNS is kept only as a first, best-effort try.
set -u

ATV_MAC="02:ad:be:0d:ee:01"                 # == the hex in the hostname; hardware-stable (config.APPLETV_MAC)
ATV_HOST="Living-Room-02ADBE0DEE01.local"   # mDNS name (config.APPLETV_HOSTNAME) — best-effort only
ATV_IP_CACHE="/tmp/gigaku-atv-ip"           # we publish the resolved IP here; lib/appletv.py reads it
ATV_PORT=49153                              # Apple TV Companion port, for the negative-route self-heal probe

log() { echo "$(date '+%F %T') $*"; }
mac_of() { arp -n "$1" 2>/dev/null | grep -oiE '([0-9a-f]{1,2}:){5}[0-9a-f]{1,2}' | head -1; }

# IP whose ARP/neighbour entry has MAC $1. macOS strips per-octet leading zeros
# (04 prints as 4), so both sides are normalised before comparing. Empty if the
# MAC isn't in the table (e.g. after we've pinned that IP to the gateway MAC).
ip_for_mac() {
  arp -an 2>/dev/null | awk -v want="$1" '
    function norm(m,  a,i,o,n){ n=split(tolower(m),a,":"); o=""
      for(i=1;i<=n;i++){ sub(/^0/,"",a[i]); o=o (i>1?":":"") a[i] } return o }
    BEGIN{ want=norm(want) }
    { ip=""; mac=""
      for(i=1;i<=NF;i++){
        if($i ~ /^\([0-9.]+\)$/){ ip=$i; gsub(/[()]/,"",ip) }
        else if($i ~ /^([0-9a-f]{1,2}:){5}[0-9a-f]{1,2}$/){ mac=$i } }
      if(mac!="" && norm(mac)==want){ print ip; exit } }'
}

GW=$(route -n get default 2>/dev/null | awk '/gateway:/{print $2}')
[ -n "${GW:-}" ] || { log "no default gateway; skip"; exit 0; }
BROADCAST="${GW%.*}.255"   # /24 subnet broadcast, to repopulate a cold ARP table

GWMAC=$(mac_of "$GW")
if [ -z "${GWMAC:-}" ]; then
  ping -c1 -t1 "$GW" >/dev/null 2>&1   # populate the gateway's ARP entry
  GWMAC=$(mac_of "$GW")
fi
[ -n "${GWMAC:-}" ] || { log "no gateway MAC for $GW; skip"; exit 0; }

repin() {  # arp -d then -s flushes the kernel's stale "host unreachable" entry
  arp -d "$1" 2>/dev/null
  arp -s "$1" "$GWMAC"
}

# Resolve the Apple TV's current IP, mDNS-independent. Order:
#   1. mDNS via dscacheutil — canonical + DHCP-proof, but frequently dead here.
#   2. ARP by hardware MAC — the reliable path. If the entry aged out, a single
#      subnet-broadcast ping makes every host ARP-announce, then we re-check.
#      (Also self-correcting for a DHCP IP change: the MAC turns up at its new IP.)
#   3. Last IP we published — covers the steady state after we've pinned this IP
#      to the gateway MAC, when step 2 no longer sees the device's own MAC.
resolve_atv_ip() {
  local ip
  ip=$(dscacheutil -q host -a name "$ATV_HOST" 2>/dev/null | awk '/^ip_address:/{print $2; exit}')
  [ -n "$ip" ] && { echo "$ip"; return; }
  ip=$(ip_for_mac "$ATV_MAC")
  if [ -z "$ip" ]; then
    ping -c1 -t1 "$BROADCAST" >/dev/null 2>&1 || true
    ip=$(ip_for_mac "$ATV_MAC")
  fi
  [ -n "$ip" ] && { echo "$ip"; return; }
  [ -f "$ATV_IP_CACHE" ] && cat "$ATV_IP_CACHE"
}

ATV_IP=$(resolve_atv_ip)
[ -n "${ATV_IP:-}" ] || { log "can't find Apple TV by mDNS, MAC ($ATV_MAC), or cache; skip"; exit 0; }
printf '%s' "$ATV_IP" > "$ATV_IP_CACHE"

if [ "$(mac_of "$ATV_IP")" != "$GWMAC" ]; then
  repin "$ATV_IP" && log "pinned Apple TV ($ATV_IP) -> gateway $GWMAC" || log "FAILED to pin Apple TV ($ATV_IP)"
fi

# Self-heal: a correct pin isn't enough if the kernel already cached a negative
# route (the daemon's own failed connects poison it while the Apple TV is asleep).
# If the Companion port is unreachable despite the pin, re-pin to flush that state.
if ! nc -z -G 2 "$ATV_IP" "$ATV_PORT" 2>/dev/null; then
  repin "$ATV_IP" && log "flushed stale negative route for Apple TV ($ATV_IP)"
fi
