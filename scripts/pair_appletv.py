#!/usr/bin/env python3
"""One-time pairing for the Apple TV that the scroll-wheel daemon controls.

Run interactively (a PIN appears on the Apple TV screen, you type it here):

    uv run python scripts/pair_appletv.py

It scans the network, lets you pick the Apple TV (the Samsung TV also advertises
AirPlay, so it picks the tvOS device automatically when there's only one), pairs
the Companion protocol, and saves the credentials into the pyatv FileStorage at
config.APPLETV_STORAGE_PATH. That's all the setup there is — the daemon then
finds the device automatically by its stored identifier, so there's no IP to
configure (it survives DHCP address changes).

Pairing is interactive and cannot be automated: only the person looking at the
Apple TV can read the PIN.
"""

import asyncio

import pyatv
from pyatv.const import OperatingSystem, Protocol
from pyatv.storage.file_storage import FileStorage

from lib.config import APPLETV_STORAGE_PATH

# Companion is all the volume daemon needs (it carries the HID VolumeUp/Down
# keys). AirPlay would only add a second PIN prompt for no benefit here.
_PROTOCOLS = (Protocol.Companion,)


def _is_apple_tv(conf) -> bool:
    return conf.device_info.operating_system == OperatingSystem.TvOS


async def _choose(loop):
    """Scan and return the chosen Apple TV config (or None)."""
    print("Scanning for devices (~5s)...")
    confs = await pyatv.scan(loop)
    if not confs:
        print("No devices found. Is the Apple TV awake and on this network?")
        return None

    for i, conf in enumerate(confs):
        tag = "  <- Apple TV" if _is_apple_tv(conf) else ""
        print(f"  [{i}] {conf.name}  ({conf.address})  {conf.device_info}{tag}")

    apple_tvs = [c for c in confs if _is_apple_tv(c)]
    if len(apple_tvs) == 1:
        chosen = apple_tvs[0]
        print(f"\nAuto-selected Apple TV: {chosen.name} ({chosen.address})")
        return chosen

    raw = input(f"\nSelect device [0-{len(confs) - 1}]: ").strip()
    try:
        return confs[int(raw)]
    except (ValueError, IndexError):
        print("Invalid selection.")
        return None


async def _pair_protocol(loop, conf, protocol, storage):
    """Pair one protocol; returns True on success, False if unavailable/failed."""
    if conf.get_service(protocol) is None:
        print(f"  {protocol.name}: not advertised by this device — skipping.")
        return False

    pairing = await pyatv.pair(conf, protocol, loop, storage=storage)
    try:
        await pairing.begin()
        if pairing.device_provides_pin:
            pin = input(f"  {protocol.name}: enter the PIN shown on the Apple TV: ").strip()
            pairing.pin(pin)
        else:
            # Rare for Apple TV, but handle it: we present a PIN to type on the TV.
            print(f"  {protocol.name}: enter this PIN on the Apple TV: 1234")
            pairing.pin("1234")
        await pairing.finish()
        if pairing.has_paired:
            print(f"  {protocol.name}: paired ✓")
            return True
        print(f"  {protocol.name}: pairing did not complete.")
        return False
    except Exception as e:
        print(f"  {protocol.name}: pairing failed: {e}")
        return False
    finally:
        await pairing.close()


async def _main():
    loop = asyncio.get_running_loop()
    storage = FileStorage(APPLETV_STORAGE_PATH, loop)
    await storage.load()

    conf = await _choose(loop)
    if conf is None:
        return

    print(f"\nPairing with {conf.name} ({conf.address})...")
    paired_any = False
    for protocol in _PROTOCOLS:
        paired_any |= await _pair_protocol(loop, conf, protocol, storage)

    await storage.save()

    if paired_any:
        print(f"\nDone. Credentials saved to {APPLETV_STORAGE_PATH}")
        print("The daemon will find this Apple TV automatically by its stored "
              "identifier — nothing else to configure.")
    else:
        print("\nNothing was paired — credentials not updated.")


if __name__ == "__main__":
    asyncio.run(_main())
