"""Where a wheel notch goes: the Apple TV, or the TV itself when the Apple TV is gone.

scripts/scroll_volume.py turns the mouse wheel into a signed accumulator; this module owns the
other half — which device the resulting step is sent to, and what happens when that
device can't be reached. It is separate from the daemon so the fallback has somewhere to
be tested: the daemon can't be imported without an event tap, and a policy that only
exists inside a `try/except` in a long-running process is a policy nobody ever checks.

The two targets are the same speaker. The Apple TV has no volume of its own — it relays
the key to the TV over HDMI-CEC — so falling back to the TV's own remote is not a
degraded mode, it is the same sound by a shorter path. What it costs is only that the
Apple TV's on-screen volume bar isn't the one that appears.

The rules, in the order they matter:

* **The Apple TV is preferred**, always, because that is what the wheel meant before this
  fallback existed and the Apple TV is the box being watched most of the time.
* **A failure falls over inside the same notch.** The step that discovers the Apple TV is
  unreachable is sent to the TV, not dropped — the wheel must never be silent while a
  working path exists. That one notch pays for the discovery (a few seconds, see the last
  rule); every one after it is instant, because the target has switched.
* **A live connection gets one reconnect; a failed connect gets none.** A send failing on
  a held connection means the socket went stale, and reconnecting is the fix. A connect
  that failed just now will fail again, and retrying it only doubles the stall before the
  fallback gets its turn.
* **Coming back is a background job.** Re-probing the Apple TV takes seconds, so it never
  happens on the send path — a task probes every `PROBE_EVERY` while the fallback is
  active and *parks a live connection*, which the next step adopts for free. The wheel
  keeps working at full speed the whole time.
* **The send path is impatient and the probe is patient**, and that asymmetry is the
  whole reason the fallback feels instant. Proving the Apple TV absent used to take ~30s
  (measured 2026-08-14: three failed name resolutions, then three empty scans), all of it
  in front of a notch. Here the send path asks once, briefly; being wrong costs a step
  played through the TV instead of the Apple TV, which is the same sound. The probe, with
  nobody waiting, asks with the full patience — so a device that answers slowly is back
  within the minute rather than lost.
"""

import asyncio
import sys
from typing import Callable, NamedTuple

# Min seconds between steps, per target. The Apple TV's is the measured ceiling of its
# HDMI-CEC volume pipeline: send faster and it buffers the surplus and keeps moving after
# the wheel stops ("inertia"). The TV applies its own keys and has no such pipeline — the
# old pre-Apple-TV daemon ran this TV at 50ms — so its ramp can be twice as quick.
APPLETV_GAP = 0.20
SAMSUNG_GAP = 0.10

# How often to check whether the Apple TV is back. Long, because the fallback is not a
# degradation worth hurrying out of, and each probe is a real connection attempt.
PROBE_EVERY = 60.0

# What "ask once, briefly" means for the Apple TV when a notch is waiting. A device that
# is there answers a unicast mDNS query in milliseconds; this is the window, not the
# expected wait.
IMPATIENT_SCAN = 1.5


class Target(NamedTuple):
    """One volume device: how to reach it, how to step it, and how fast it can be driven."""

    key: str
    name: str
    connect: Callable  # async (log, patient: bool) -> connection
    send: Callable  # async (connection, direction: +1/-1) -> None
    close: Callable  # async (connection) -> None
    gap: float


def _stdout(message):
    """The daemon's log — launchd sends both streams to /tmp/gigaku-sound.log."""
    print(message, flush=True)


async def _connect_appletv(log, patient):
    from lib import appletv

    del log  # pyatv says nothing worth relaying; failures come back as exceptions
    loop = asyncio.get_running_loop()
    if patient:
        return await appletv.connect(loop)
    return await appletv.connect(loop, scans=1, scan_timeout=IMPATIENT_SCAN)


async def _step_appletv(conn, direction):
    from lib import appletv

    await (appletv.volume_up(conn) if direction > 0 else appletv.volume_down(conn))


async def _close_appletv(conn):
    from lib import appletv

    await appletv.close(conn)


async def _connect_samsung(log, patient):
    from lib import samsung

    del patient  # a token replay is one round trip; there is no slower way to ask
    return await samsung.connect(log)


async def _step_samsung(conn, direction):
    from lib import samsung

    await (samsung.volume_up(conn) if direction > 0 else samsung.volume_down(conn))


async def _close_samsung(conn):
    from lib import samsung

    await samsung.close(conn)


def default_targets():
    """The real chain, preferred first. Imports are deferred to the call — nothing here
    should drag pyatv or aiohttp into a process that only wanted the policy."""
    return [
        Target("appletv", "Apple TV", _connect_appletv, _step_appletv,
               _close_appletv, APPLETV_GAP),
        Target("samsung", "TV", _connect_samsung, _step_samsung,
               _close_samsung, SAMSUNG_GAP),
    ]


class Volume:
    """The wheel's volume sink: holds the live connection and picks the device.

    Every method is a coroutine on the sender's own event loop and none of them are safe
    to call concurrently with each other — the daemon drives steps one at a time, which
    is what lets the background probe hand a connection over by parking it rather than
    by touching anything the send path is using.
    """

    def __init__(self, targets=None, log=_stdout, probe_every=PROBE_EVERY):
        self._targets = list(targets if targets is not None else default_targets())
        self._log = log
        self._probe_every = probe_every
        self._conns = {}  # target key -> live connection
        self._active = self._targets[0]
        self._probe_task = None
        self._parked = None  # (target, connection) the probe found, awaiting adoption

    @property
    def preferred(self):
        return self._targets[0]

    @property
    def active(self):
        """The target the next step will be tried on first."""
        return self._active

    @property
    def gap(self):
        """Min seconds between steps for whatever is currently being driven."""
        return self._active.gap

    async def step(self, direction):
        """Send one volume step. Returns the target that took it, or None if none could.

        Never raises: a wheel notch that can't be delivered is a logged line, not a dead
        sender thread.
        """
        await self._adopt_parked()
        for target in [self._active] + [t for t in self._targets if t is not self._active]:
            try:
                await self._send(target, direction)
            except Exception as e:
                self._log(f"  {target.name} unreachable: {e}")
                continue
            if target is not self._active:
                self._log(f"  volume → {target.name}")
                await self._switch(target)
            return target
        return None

    async def close(self):
        """Drop every connection and stop probing."""
        self._cancel_probe()
        await self._park(None)
        for target in self._targets:
            await self._drop(target)

    # ── one target ────────────────────────────────────────────────────────────────────

    async def _send(self, target, direction):
        """One step to `target`, reconnecting once if the connection we held went stale.

        Raises whatever the target raises once it has had that one retry."""
        conn = self._conns.get(target.key)
        if conn is not None:
            try:
                await target.send(conn, direction)
                return
            except Exception as e:
                self._log(f"  {target.name}: reconnecting ({e})")
                await self._drop(target)
        # A failed connect is not retried here, and it is asked impatiently: something
        # else is about to be tried, and it plays through the same speaker.
        conn = await target.connect(self._log, patient=False)
        self._conns[target.key] = conn
        self._log(f"Connected to {target.name}")
        try:
            await target.send(conn, direction)
        except Exception:
            await self._drop(target)
            raise

    async def _drop(self, target):
        conn = self._conns.pop(target.key, None)
        if conn is not None:
            try:
                await target.close(conn)
            except Exception:
                pass  # closing a connection that already died is not news

    # ── which target ──────────────────────────────────────────────────────────────────

    async def _switch(self, target):
        """Make `target` the active one, releasing the others and re-aiming the probe."""
        self._active = target
        for other in self._targets:
            if other is not target:
                await self._drop(other)
        if target is self.preferred:
            self._cancel_probe()
        else:
            self._start_probe()

    async def _adopt_parked(self):
        """Take over a connection the probe opened, if there is one."""
        parked, self._parked = self._parked, None
        if parked is None:
            return
        target, conn = parked
        self._conns[target.key] = conn
        self._log(f"  {target.name} is back — volume → {target.name}")
        await self._switch(target)

    async def _park(self, found):
        """Hold (or discard) a connection the probe opened, replacing any earlier one."""
        previous, self._parked = self._parked, found
        if previous is not None:
            target, conn = previous
            try:
                await target.close(conn)
            except Exception:
                pass

    def _start_probe(self):
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.create_task(self._probe_loop())

    def _cancel_probe(self):
        if self._probe_task is not None and not self._probe_task.done():
            self._probe_task.cancel()
        self._probe_task = None

    async def _probe_loop(self):
        """Wait for the preferred target to come back, then park a live connection.

        Ends as soon as it succeeds (or the preferred target became active some other
        way); `_switch` starts a new one the next time we fall back.
        """
        target = self.preferred
        while True:
            await asyncio.sleep(self._probe_every)
            if self._active is target:
                return
            try:
                conn = await target.connect(self._log, patient=True)
            except Exception:
                continue
            await self._park((target, conn))
            return


async def _selftest():
    """Send one step up and one down through the real chain, naming what took them."""
    vol = Volume()
    try:
        for direction in (1, -1):
            target = await vol.step(direction)
            print(f"step {direction:+d} → {target.name if target else 'nowhere'}")
            await asyncio.sleep(0.5)
    finally:
        await vol.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_selftest()))
