"""The wheel's fallback policy (lib/volume.py) against fake devices.

This is the whole reason the policy left scripts/scroll_volume.py: the daemon needs an event tap
to exist, so "what happens when the Apple TV is off" could only ever be checked by
turning the Apple TV off. Here the device is a counter.
"""

import asyncio

import pytest

from lib import volume


class Fake:
    """A stand-in volume device: fails on demand, records what reached it."""

    def __init__(self, name, gap=0.1):
        self.name = name
        self.gap = gap
        self.up = True  # can it be connected to at all
        self.fail_sends = 0  # how many of the next sends blow up (a stale socket)
        self.steps = []
        self.connects = 0
        self.closes = 0
        self.patience = []  # how each connect was asked for: the send path or the probe

    def target(self, key):
        return volume.Target(
            key, self.name, self._connect, self._send, self._close, self.gap
        )

    async def _connect(self, _log, patient):
        self.connects += 1
        self.patience.append(patient)
        if not self.up:
            raise RuntimeError(f"{self.name} is off")
        return f"{self.name}-conn-{self.connects}"

    async def _send(self, _conn, direction):
        if self.fail_sends:
            self.fail_sends -= 1
            raise RuntimeError(f"{self.name} send failed")
        self.steps.append(direction)

    async def _close(self, _conn):
        self.closes += 1


@pytest.fixture
def devices():
    return Fake("Apple TV", gap=0.2), Fake("TV", gap=0.1)


def make(devices, probe_every=60.0):
    atv, tv = devices
    return volume.Volume(
        targets=[atv.target("appletv"), tv.target("samsung")],
        log=lambda _msg: None,
        probe_every=probe_every,
    )


def run(coro):
    return asyncio.run(coro)


def test_steps_go_to_the_preferred_target(devices):
    atv, tv = devices

    async def scenario():
        vol = make(devices)
        assert (await vol.step(1)).key == "appletv"
        assert (await vol.step(-1)).key == "appletv"
        await vol.close()

    run(scenario())
    assert atv.steps == [1, -1]
    assert atv.connects == 1  # one connection, held
    assert tv.connects == 0


def test_falls_over_to_the_tv_inside_the_failing_notch(devices):
    """The step that discovers the Apple TV is gone is not the step that's lost."""
    atv, tv = devices
    atv.up = False

    async def scenario():
        vol = make(devices)
        assert (await vol.step(1)).key == "samsung"
        assert vol.active.key == "samsung"
        assert (await vol.step(1)).key == "samsung"
        await vol.close()

    run(scenario())
    assert tv.steps == [1, 1]
    assert atv.connects == 1  # the second notch didn't pay for the Apple TV again
    assert tv.connects == 1


def test_a_failed_connect_is_not_retried_before_falling_over(devices):
    """One doomed attempt, not two — the second only doubles the stall."""
    atv, tv = devices
    atv.up = False

    async def scenario():
        vol = make(devices)
        await vol.step(1)
        await vol.close()

    run(scenario())
    assert atv.connects == 1


def test_a_stale_connection_reconnects_on_the_same_target(devices):
    """A send failing on a held connection is a dead socket, not a dead device."""
    atv, tv = devices

    async def scenario():
        vol = make(devices)
        await vol.step(1)
        atv.fail_sends = 1
        assert (await vol.step(-1)).key == "appletv"
        await vol.close()

    run(scenario())
    assert atv.steps == [1, -1]
    assert atv.connects == 2  # reconnected once
    assert tv.connects == 0  # and never fell over


def test_a_dead_target_that_wont_reconnect_falls_over(devices):
    atv, tv = devices

    async def scenario():
        vol = make(devices)
        await vol.step(1)
        atv.fail_sends, atv.up = 1, False  # socket died and the box went with it
        assert (await vol.step(1)).key == "samsung"
        await vol.close()

    run(scenario())
    assert tv.steps == [1]


def test_both_down_drops_the_step(devices):
    """A notch that can't be delivered is a log line, never an exception."""
    atv, tv = devices
    atv.up = tv.up = False

    async def scenario():
        vol = make(devices)
        assert await vol.step(1) is None
        await vol.close()

    run(scenario())
    assert atv.steps == tv.steps == []


def test_the_gap_follows_the_active_target(devices):
    """The pacing is the device's, so it has to move with the fallback."""
    atv, tv = devices
    atv.up = False

    async def scenario():
        vol = make(devices)
        assert vol.gap == 0.2
        await vol.step(1)
        assert vol.gap == 0.1
        await vol.close()

    run(scenario())


def test_the_probe_brings_the_apple_tv_back_without_costing_a_step(devices):
    """Coming back is a background job: the next notch adopts a connection already open."""
    atv, tv = devices
    atv.up = False

    async def scenario():
        vol = make(devices, probe_every=0.01)
        assert (await vol.step(1)).key == "samsung"
        atv.up = True
        for _ in range(50):  # let the probe task get its turn
            await asyncio.sleep(0.01)
            if vol._parked:
                break
        assert (await vol.step(1)).key == "appletv"
        assert vol.active.key == "appletv"
        await vol.close()

    run(scenario())
    assert atv.steps == [1]
    assert atv.connects == 2  # the failed one and the probe's — the step paid for neither
    assert tv.closes == 1  # and the TV's channel was let go on the way back
    # Impatient in front of a notch, patient when nothing is waiting.
    assert atv.patience == [False, True]


def test_the_real_targets_take_the_arguments_this_module_passes():
    """The fakes above can't catch a real adapter whose parameters were renamed — and one
    was, which showed up only as the fallback failing against the live TV."""
    import inspect

    for target in volume.default_targets():
        connect = inspect.signature(target.connect)
        connect.bind(object(), patient=True)  # raises TypeError if it wouldn't take it
        inspect.signature(target.send).bind(object(), 1)
        inspect.signature(target.close).bind(object())


def test_no_probe_runs_while_the_apple_tv_is_the_one_answering(devices):
    atv, tv = devices

    async def scenario():
        vol = make(devices, probe_every=0.01)
        await vol.step(1)
        await asyncio.sleep(0.05)
        assert vol._probe_task is None
        await vol.close()

    run(scenario())
    assert atv.connects == 1
