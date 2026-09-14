"""Finding a device on the LAN without mDNS — the ARP table and the local /24.

Both TVs this project drives are found the same way, and for the same reason: mDNS is
not dependable here. The AP's per-SSID client isolation wedges multicast for hours at a
time (see scripts/lan_bypass.README.md), and a DHCP lease moves an IP out from under any
constant we could write down — the Samsung's hardcoded 192.168.0.198 was stale by the
time anything asked for it again. A hardware MAC changes for neither reason, so the
ARP/neighbour table keyed by MAC is the robust answer, and a port scan of the local /24
is the last resort that also survives the MAC itself changing (a replaced device).

Pure stdlib and domain-agnostic: nothing here knows what an Apple TV or a Samsung is.
"""

import re
import socket
import subprocess

_ARP_LINE = re.compile(r"\(([\d.]+)\) at ([0-9a-fA-F:]+)")


def norm_mac(mac):
    """A MAC as macOS's `arp` prints it: lowercase, per-octet leading zeros stripped.

    `arp -an` shows 00:7d:3b:… as 0:7d:3b:…, so a config constant written the canonical
    way never compares equal literally. Normalise both sides instead.
    """
    return ":".join(part.lstrip("0") or "0" for part in mac.lower().split(":"))


def ips_by_mac(arp_output):
    """{normalised MAC: IP} parsed from `arp -an` output. Pure, so it can be tested."""
    found = {}
    for line in arp_output.splitlines():
        m = _ARP_LINE.search(line)
        if m:
            found.setdefault(norm_mac(m.group(2)), m.group(1))
    return found


def ip_for_mac(mac):
    """The IPv4 whose ARP/neighbour entry has hardware address `mac`, or None.

    mDNS-free — works whenever the device is in the neighbour table, and stays correct
    across a DHCP IP change (the MAC turns up at its new IP). Returns None once
    lan_bypass has pinned that IP to the gateway MAC, since the device's own MAC no
    longer appears; callers fall back to their published IP or a scan.
    """
    try:
        out = subprocess.run(
            ["arp", "-an"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return ips_by_mac(out).get(norm_mac(mac))


def local_ip():
    """This machine's IPv4 on the route to the LAN, or None.

    A UDP socket that is only *connected* — no packet is sent, nothing is contacted —
    makes the kernel pick the outbound interface and tell us its address.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1: routable-looking, never answered
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def subnet_hosts(ip):
    """Every host address of `ip`'s /24, ourselves excluded. [] if ip is None.

    A /24 is an assumption, and the right one here: it's what the router hands out, and
    a scan is already the fallback of a fallback.
    """
    if not ip:
        return []
    prefix = ip.rsplit(".", 1)[0]
    return [f"{prefix}.{n}" for n in range(1, 255) if f"{prefix}.{n}" != ip]
