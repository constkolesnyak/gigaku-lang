"""Samsung Tizen TV remote — the wheel's volume fallback when the Apple TV is gone.

The Apple TV has no volume of its own: it relays the key to this TV over HDMI-CEC. So
when it can't be reached (off, asleep, off the network) the honest fallback is to send
the same key to the TV directly, and the user hears exactly what they would have heard.
lib/volume.py owns *when* that happens; this module is only how.

Protocol (2016+ Tizen, here a GQ55Q80DATXZG): a token-authenticated WebSocket on port
8002. The first connection pops an "Allow this device?" dialog on screen; accept it once
and the TV returns a token in its ms.channel.connect frame, which we persist and replay
forever after. A key is one JSON ms.remote.control / SendRemoteKey message. None of the
old H-series encryption applies (no Rijndael, no PIN crypto).

Measured on this TV, 2026-08-14, and shaping the code:

* **It never answers a WebSocket ping.** With aiohttp's `heartbeat=10` the connection is
  killed as unanswered after ~30s (close code 1006) every time. So the socket carries no
  heartbeat, and liveness is discovered the only other way there is — a send that fails.
* **The handshake with a stored token is instant** (0.04s) and silent: no dialog, no
  on-screen trace. Which is what makes reconnect-on-failure cheap enough to be the whole
  recovery strategy.
* **The TV is wired**, so it is reachable without the lan_bypass ARP pin the Apple TV
  needs; that isolation is per-SSID and this TV is not on the Wi-Fi.

Finding it is a ladder, not a constant: ARP by hardware MAC → the IP we last published →
a scan of the local /24 for port 8001, each candidate confirmed by asking the device to
describe itself. The constant this replaced (192.168.0.198) was already stale.
"""

import asyncio
import base64
import json
import ssl

import aiohttp

from lib.config import SAMSUNG_IP_CACHE, SAMSUNG_MAC, SAMSUNG_TOKEN_PATH
from lib.platform import net

# Shown on the TV's "Allow this device?" dialog and in its connected-clients list. It is
# also half the identity the token is bound to, so renaming it means accepting again.
REMOTE_NAME = "gigaku"

_INFO_PORT = 8001  # plain HTTP device description; also what a scan probes for
_REMOTE_PORT = 8002  # wss remote control channel

_HTTP_TIMEOUT = 1.5  # a LAN round trip is ~10ms; anything slower is a host that isn't there
_CONNECT_TIMEOUT = 5.0
_PAIR_WAIT = 30.0  # with no token the first frame waits on a human accepting the dialog
_SCAN_TIMEOUT = 1.0
_SCAN_CONCURRENCY = 48  # keep the fd count well under any inherited soft limit

VOLUME_UP = "KEY_VOLUP"
VOLUME_DOWN = "KEY_VOLDOWN"

# The TV serves a self-signed certificate for its own LAN IP; there is no name to verify
# and no CA that would have signed it.
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


class SamsungTVError(Exception):
    """Raised when the TV can't be found, refuses the remote channel, or drops it."""


class Remote:
    """A live remote channel. Hold it open; a send is what discovers it has died."""

    def __init__(self, session, ws, ip):
        self.session = session
        self.ws = ws
        self.ip = ip

    def __repr__(self):
        return f"<Samsung TV at {self.ip}>"


# ── finding the TV ────────────────────────────────────────────────────────────────────


def load_token():
    """The saved remote token, or None if the TV has never allowed us."""
    try:
        with open(SAMSUNG_TOKEN_PATH) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _save_token(token):
    with open(SAMSUNG_TOKEN_PATH, "w") as f:
        f.write(token)


def _publish_ip(ip):
    """Record where the TV was found, for the next run's second-guess."""
    try:
        with open(SAMSUNG_IP_CACHE, "w") as f:
            f.write(ip)
    except OSError:
        pass  # a cache that can't be written just means the ladder starts a rung lower


def _cached_ip():
    try:
        with open(SAMSUNG_IP_CACHE) as f:
            return f.read().strip() or None
    except OSError:
        return None


async def describe(session, ip, timeout=_HTTP_TIMEOUT):
    """The TV's own description (`GET /api/v2/`), or None if nothing answers at `ip`.

    Cheap, unauthenticated and unmissable — it is how a candidate IP is confirmed to be a
    TV at all, and `device.wifiMac` in the answer is how it is confirmed to be *this* one.
    """
    try:
        async with session.get(
            f"http://{ip}:{_INFO_PORT}/api/v2/",
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                return None
            # The TV serves this as text/plain, so don't let aiohttp police the type.
            return json.loads(await resp.text())
    except (OSError, asyncio.TimeoutError, aiohttp.ClientError, ValueError):
        return None


def is_ours(info):
    """True if this description is the TV named by config.SAMSUNG_MAC."""
    mac = ((info or {}).get("device") or {}).get("wifiMac")
    return bool(mac) and net.norm_mac(mac) == net.norm_mac(SAMSUNG_MAC)


def is_a_samsung_tv(info):
    """True for any Samsung TV — the looser test a scan falls back to.

    A replaced TV has a new MAC, and refusing to find it would be a worse failure than
    naming the one Samsung TV that answered. The caller only accepts this when exactly
    one host qualifies, and says so in the log.
    """
    return ((info or {}).get("device") or {}).get("type") == "Samsung SmartTV"


async def _scan(session, log):
    """Every host on the local /24 that answers as a Samsung TV: [(ip, info)].

    The last rung of the ladder, reached only when the MAC isn't in the ARP table and the
    cached IP is wrong — i.e. a lease change while the TV was quiet, or a new TV.
    """
    hosts = net.subnet_hosts(net.local_ip())
    if not hosts:
        return []
    log(f"  TV: scanning {len(hosts)} hosts for port {_INFO_PORT}…")
    gate = asyncio.Semaphore(_SCAN_CONCURRENCY)

    async def probe(ip):
        async with gate:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(ip, _INFO_PORT), _SCAN_TIMEOUT
                )
            except (OSError, asyncio.TimeoutError):
                return None
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            info = await describe(session, ip)
            return (ip, info) if is_a_samsung_tv(info) else None

    return [hit for hit in await asyncio.gather(*(probe(h) for h in hosts)) if hit]


async def find_tv(session, log=print):
    """The TV's current IP. Raises SamsungTVError if it isn't on the network.

    ARP by MAC first (instant, DHCP-proof, and the entry is there whenever the TV has
    spoken recently), then the IP we last published, then a scan. Every candidate is
    confirmed by the device's own description before it is believed — a stale cache
    pointing at somebody else's laptop must not become "the TV".
    """
    ip = await asyncio.to_thread(net.ip_for_mac, SAMSUNG_MAC)
    if ip and await describe(session, ip):
        _publish_ip(ip)
        return ip

    cached = _cached_ip()
    if cached and cached != ip and is_ours(await describe(session, cached)):
        return cached

    hits = await _scan(session, log)
    ours = [hit for hit in hits if is_ours(hit[1])]
    if not ours and len(hits) == 1:
        ours = hits
        name = (hits[0][1].get("device") or {}).get("modelName")
        log(f"  TV: {hits[0][0]} is the only Samsung TV here, but it is a {name} — "
            f"not {SAMSUNG_MAC}. Using it; update config.SAMSUNG_MAC if the TV changed.")
    if ours:
        _publish_ip(ours[0][0])
        return ours[0][0]

    raise SamsungTVError(
        f"No Samsung TV on the LAN — {SAMSUNG_MAC} isn't in the ARP table, "
        f"{cached or 'no cached IP'} doesn't answer, and the scan found "
        f"{len(hits)} Samsung TVs (is it unplugged?)"
    )


# ── talking to it ─────────────────────────────────────────────────────────────────────


def remote_url(ip, token=None):
    """The remote-control channel URL. The name is base64 and part of the token's identity."""
    name = base64.b64encode(REMOTE_NAME.encode()).decode()
    url = (
        f"wss://{ip}:{_REMOTE_PORT}/api/v2/channels/samsung.remote.control?name={name}"
    )
    return f"{url}&token={token}" if token else url


def key_message(key):
    """The SendRemoteKey payload for one key click."""
    return json.dumps(
        {
            "method": "ms.remote.control",
            "params": {
                "Cmd": "Click",
                "DataOfCmd": key,
                "Option": "false",
                "TypeOfRemote": "SendRemoteKey",
            },
        }
    )


async def connect(log=print):
    """Open an authenticated remote channel to the TV. Returns a live Remote.

    With a stored token this is one round trip and silent. Without one the TV shows its
    "Allow this device?" dialog and the first frame doesn't arrive until somebody accepts
    — so that wait is long on purpose, and announced, because a wheel that does nothing
    for half a minute needs to say why.
    """
    session = aiohttp.ClientSession()
    try:
        ip = await find_tv(session, log)
        token = load_token()
        if not token:
            log(f"  TV: no saved token — accept the '{REMOTE_NAME}' dialog on the TV "
                f"(waiting up to {_PAIR_WAIT:.0f}s)")
        ws = await asyncio.wait_for(
            session.ws_connect(remote_url(ip, token), ssl=_SSL, heartbeat=None),
            _CONNECT_TIMEOUT,
        )
    except SamsungTVError:
        await session.close()
        raise
    except Exception as e:
        await session.close()
        raise SamsungTVError(f"Can't open the TV's remote channel: {e}") from e

    try:
        msg = await asyncio.wait_for(
            ws.receive(), _CONNECT_TIMEOUT if token else _PAIR_WAIT
        )
        if msg.type is not aiohttp.WSMsgType.TEXT:
            raise SamsungTVError(f"TV refused the remote channel ({msg.type.name})")
        frame = json.loads(msg.data)
        if frame.get("event") != "ms.channel.connect":
            raise SamsungTVError(f"Unexpected handshake from TV: {frame}")
        new = (frame.get("data") or {}).get("token")
        if new and new != token:
            _save_token(new)
            log("  TV: paired — token saved")
    except SamsungTVError:
        await _shutdown(session, ws)
        raise
    except Exception as e:
        await _shutdown(session, ws)
        raise SamsungTVError(f"TV never confirmed the remote channel: {e}") from e

    return Remote(session, ws, ip)


async def _shutdown(session, ws):
    try:
        await ws.close()
    except Exception:
        pass
    await session.close()


async def close(remote):
    """Close a channel. Safe on a half-dead one — it is used on the failure path."""
    if remote is not None:
        await _shutdown(remote.session, remote.ws)


async def send_key(remote, key):
    """Send one remote key. Raises SamsungTVError once the channel has died.

    The TV answers no ping (measured), so a dead channel is only ever discovered here —
    which is fine, because reconnecting costs 0.04s and the caller does it on the spot.
    """
    if remote.ws.closed:
        raise SamsungTVError(f"remote channel closed (code {remote.ws.close_code})")
    try:
        await remote.ws.send_str(key_message(key))
    except Exception as e:
        raise SamsungTVError(f"send failed: {e}") from e


async def volume_up(remote):
    await send_key(remote, VOLUME_UP)


async def volume_down(remote):
    await send_key(remote, VOLUME_DOWN)


async def _selftest():
    """Standalone check: find the TV, open the channel, nudge the volume and put it back.

    Run it once by hand to accept the TV's dialog, so the wheel's fallback never has to:
        uv run python -m lib.samsung.control
    """
    remote = await connect()
    try:
        info = await describe(remote.session, remote.ip)
        device = (info or {}).get("device") or {}
        print(f"Connected to {device.get('name')} ({device.get('modelName')}) "
              f"at {remote.ip} — power {device.get('PowerState')}")
        await volume_up(remote)
        await asyncio.sleep(0.5)
        await volume_down(remote)
        print("Sent volume up then down — the bar should have blipped and come back.")
    finally:
        await close(remote)


if __name__ == "__main__":
    asyncio.run(_selftest())
