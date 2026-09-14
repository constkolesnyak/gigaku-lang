"""Apple TV volume control over the network (pyatv).

The scroll-wheel daemon (scripts/scroll_volume.py) sends volume up/down here instead of
to the Samsung TV. pyatv talks to the Apple TV over the Companion protocol; a
wheel notch is a raw HID VolumeUp/Down key (see volume_up/_hid for why we don't
use atv.audio.volume_up()).

Pairing is one-time and interactive (a PIN appears on the Apple TV screen): run
`python scripts/pair_appletv.py`, which writes Companion credentials into the pyatv
FileStorage at config.APPLETV_STORAGE_PATH. connect() replays those credentials
on every run — no popup, no menu.

Discovery is a *unicast host-scan* of the IP we resolve from the device's stable
mDNS hostname (config.APPLETV_HOSTNAME), NOT pyatv's multicast device scan: on
this network the AP's per-SSID client isolation drops/garbles the multicast scan,
and direct unicast only works because the lan_bypass daemon hairpins the Apple
TV's traffic through the gateway (see scripts/lan_bypass.sh). Resolving the stable
hostname each run keeps us DHCP-proof. We connect Companion-only so the audio
main_instance is deterministically the Companion one (whose .api carries the HID
volume keys); leaving AirPlay/RAOP in can make RAOP win and break _hid.

pyatv is asyncio-based, so every call here is a coroutine; the daemon runs them
on a dedicated event loop in its sender thread.
"""

import asyncio
import socket

import pyatv
from pyatv.const import Protocol
from pyatv.protocols.companion.api import HidCommand
from pyatv.storage.file_storage import FileStorage

from lib.config import (
    APPLETV_HOSTNAME,
    APPLETV_IP_CACHE,
    APPLETV_MAC,
    APPLETV_STORAGE_PATH,
)
from lib.platform.net import ip_for_mac

# How hard to look before calling the device absent. Measured on this network with the
# Apple TV off, 2026-08-14: the old ladder spent 15s on three failed name resolutions and
# another 15s on three empty scans — half a minute to prove an absence, all of it in front
# of whatever was waiting for the connection.
MDNS_TIMEOUT = 2.0  # a healthy answer takes ~0.1s; a wedged resolver takes the OS's 5s
SCANS = 3  # a unicast host-scan can come back empty transiently
SCAN_TIMEOUT = 5.0


class AppleTVError(Exception):
    """Raised when the Apple TV can't be found or connected to."""


def _storage(loop):
    return FileStorage(APPLETV_STORAGE_PATH, loop)


def _companion_settings(storage):
    """The stored Companion (identifier, credentials) written by scripts/pair_appletv.py."""
    for device in storage.settings:
        companion = device.protocols.companion
        if getattr(companion, "credentials", None):
            return companion.identifier, companion.credentials
    return None, None


async def _mdns(host, timeout=MDNS_TIMEOUT):
    """`host` resolved by the OS, or None if it doesn't answer inside `timeout`.

    Bounded because a *failing* getaddrinfo here costs the system resolver's full 5s,
    and this name fails structurally (see the class comment on client isolation) —
    for hours at a time, not transiently. Five seconds to re-learn that, on a path a
    wheel notch is waiting behind, buys nothing that the two exact rungs below don't
    give for free. A healthy answer arrives in ~0.1s. The abandoned thread finishes
    on its own; only its answer is dropped.
    """
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, None, socket.AF_INET), timeout
        )
    except (OSError, asyncio.TimeoutError):
        return None
    return infos[0][4][0] if infos else None


async def _resolve_ip():
    """Current IPv4 of the Apple TV, resolved mDNS-independently.

    Order: (1) the OS resolver on the stable mDNS hostname — canonical and
    DHCP-proof, but it wedges under the AP's client isolation (returns EAI_NONAME
    for hours), so it is asked once and briefly; (2) the ARP table keyed by the
    device's hardware MAC (config.APPLETV_MAC) — reliable whenever the device is in
    the neighbour table; (3) the IP the lan_bypass daemon last published — the steady
    state once that daemon has pinned the IP to the gateway MAC (step 2 can't see the
    device's own MAC then). Raises AppleTVError only if all three come up empty."""
    ip = await _mdns(APPLETV_HOSTNAME)
    if ip:
        return ip

    ip = await asyncio.to_thread(ip_for_mac, APPLETV_MAC)
    if ip:
        return ip

    try:
        with open(APPLETV_IP_CACHE) as f:
            ip = f.read().strip()
        if ip:
            return ip
    except OSError:
        pass

    raise AppleTVError(
        f"Can't resolve {APPLETV_HOSTNAME}, find {APPLETV_MAC} in the ARP table, "
        f"or read a cached IP at {APPLETV_IP_CACHE} "
        "(is the lan_bypass daemon running?)"
    )


async def connect(loop, scans=SCANS, scan_timeout=SCAN_TIMEOUT):
    """Open a Companion connection to the paired Apple TV using stored credentials.

    Resolves config.APPLETV_HOSTNAME to the current IP (OS mDNS — reliable even
    under client isolation), unicast host-scans that IP, keeps only the Companion
    service, attaches the stored credentials, and connects. Returns a connected
    pyatv AppleTV. Raises AppleTVError if nothing is paired, the name can't be
    resolved, the device is unreachable, or the connection fails.

    `scans`/`scan_timeout` are how long to keep asking before calling the device
    absent. The defaults are patient, which is right for pairing and for a background
    retry; a caller with something else to try (lib/volume.py, whose fallback is the
    same speaker by a shorter path) passes a single short scan instead — see the note
    there on why impatience is free on the send path and wrong everywhere else.
    """
    storage = _storage(loop)
    await storage.load()
    identifier, credentials = _companion_settings(storage)
    if not credentials:
        raise AppleTVError(
            "No Apple TV paired yet — run `python scripts/pair_appletv.py` first."
        )

    ip = await _resolve_ip()

    conf = None
    for _ in range(scans):  # unicast host-scan can come back empty transiently
        confs = await pyatv.scan(loop, hosts=[ip], storage=storage, timeout=scan_timeout)
        if confs:
            conf = confs[0]
            break
    if conf is None:
        raise AppleTVError(
            "Paired Apple TV not found on the network (off, asleep, or off-network?)"
        )

    # Companion-only: it carries the HID volume keys, and being the sole service
    # makes it the audio main_instance deterministically (otherwise RAOP can win
    # and main_instance.api won't exist — see _hid).
    for proto in list(conf._services):
        if proto != Protocol.Companion:
            del conf._services[proto]
    service = conf.get_service(Protocol.Companion)
    if service is None:
        raise AppleTVError("Apple TV is not exposing its Companion service")
    if not service.credentials:
        service.credentials = credentials
    if not service.identifier and identifier:
        service.identifier = identifier

    try:
        return await pyatv.connect(conf, loop, storage=storage)
    except Exception as e:
        raise AppleTVError(f"Failed to connect to Apple TV: {e}") from e


async def close(atv):
    """Close a connection, draining the cleanup tasks pyatv hands back. Safe to
    call on a half-dead handle — it's used on the error/reconnect path."""
    try:
        tasks = atv.close()
    except Exception:
        return
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _hid(atv, command):
    """Send one HID key (press+release) fire-and-forget over Companion.

    We deliberately bypass atv.audio.volume_up()/volume_down(): those send the
    key and then block up to 5s waiting for the Apple TV to report a new volume
    LEVEL. This Apple TV outputs to the TV over HDMI-CEC and reports no level
    (audio.volume reads 0.0), so that wait always times out and raises — even
    though the key itself was already delivered and the TV's volume did change.
    Sending the raw HID command instead is instant and never raises on success.
    """
    api = atv.audio.main_instance.api
    await api.hid_command(True, command)
    await api.hid_command(False, command)


async def volume_up(atv):
    await _hid(atv, HidCommand.VolumeUp)


async def volume_down(atv):
    await _hid(atv, HidCommand.VolumeDown)


async def _selftest():
    """Standalone check: connect, report the volume feature, bump volume once."""
    from pyatv.const import FeatureName

    loop = asyncio.get_running_loop()
    atv = await connect(loop)
    try:
        feat = atv.features.get_feature(FeatureName.VolumeUp)
        print(f"VolumeUp feature: {feat.state}")
        # Often 0.0: the Apple TV doesn't track a level when it relays volume to
        # the TV over HDMI-CEC. That's fine — the HID key still drives the TV.
        print(f"Reported volume level: {atv.audio.volume}")
        await volume_up(atv)
        print("Sent one volume_up — watch the TV's volume bar.")
    finally:
        await close(atv)


if __name__ == "__main__":
    asyncio.run(_selftest())
