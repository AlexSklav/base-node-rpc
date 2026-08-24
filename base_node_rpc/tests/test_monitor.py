# coding: utf-8
"""
Serial-monitor lifecycle: :func:`base_node_rpc._async_base._async_serial_keepalive`,
:class:`base_node_rpc._async_base.AsyncSerialMonitor` and
:class:`base_node_rpc._async_base.BaseNodeSerialMonitor`.

The defects guarded here all come from the same family -- a coroutine that
never yields, an event that is set on every loop iteration, or a reference left
dangling across a reconnect:

* the keepalive loop starving the rest of the event loop (so packets were never
  read) whenever no serial device was available, or when the connected device
  died;
* ``monitor.device`` still pointing at a dead ``AsyncSerial`` for the whole gap
  between a disconnect and the next reconnect, so a consumer could not tell "no
  device" from "device about to be replaced";
* ``connected``/``disconnected`` signals firing on every iteration of the
  reconnect loop (~100/s) rather than on an actual transition;
* ``_submit_request()`` on a stopped monitor leaking a ``coroutine ... was
  never awaited`` :class:`RuntimeWarning` instead of raising a clear error.

Hardware-free: :class:`asyncserial.AsyncSerial` is replaced with an in-memory
fake for every test.  No serial port, no network, no firmware.
"""
import asyncio
import gc
import threading
import time
import warnings

import asyncserial
import pytest
import serial

import base_node_rpc._async_base as ab
from base_node_rpc._async_base import (AsyncSerialMonitor,
                                       BaseNodeSerialMonitor,
                                       _async_serial_keepalive)


class FakeAsyncSerial:
    """In-memory stand-in with ``asyncserial.AsyncSerial`` semantics.

    In particular it reproduces the *error latch*: once the device is
    unplugged, ``in_waiting`` raises and ``is_open`` goes ``False`` and stays
    there, exactly as the real class behaves after a hot-unplug.
    """

    #: Every instance ever constructed, in construction order.
    instances = []
    #: When set, ``__init__`` raises instead of connecting.
    fail_to_open = False

    def __init__(self, *args, **kwargs):
        type(self).instances.append(self)
        if type(self).fail_to_open:
            raise serial.SerialException('no such device')
        self.port = kwargs.get('port', args[0] if args else 'FAKE0')
        self.unplugged = False
        self._closed = False
        self.written = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        self.close()
        return False

    @property
    def is_open(self):
        return not (self._closed or self.unplugged)

    @property
    def in_waiting(self):
        if self.unplugged:
            raise OSError('device gone')
        return 0

    async def read(self, n_bytes):
        while not (self.unplugged or self._closed):
            await asyncio.sleep(0.005)
        raise IOError('Serial port is not usable')

    async def write(self, data):
        self.written.append(bytes(data))
        return len(data)

    def close(self):
        self._closed = True


@pytest.fixture
def fake_serial(monkeypatch):
    """Install :class:`FakeAsyncSerial` in place of ``asyncserial.AsyncSerial``."""
    FakeAsyncSerial.instances = []
    FakeAsyncSerial.fail_to_open = False
    monkeypatch.setattr(ab.asyncserial, 'AsyncSerial', FakeAsyncSerial)
    monkeypatch.setattr(asyncserial, 'AsyncSerial', FakeAsyncSerial,
                        raising=False)
    yield FakeAsyncSerial
    FakeAsyncSerial.instances = []
    FakeAsyncSerial.fail_to_open = False


class _KeepaliveParent:
    """Minimal object exposing exactly what ``_async_serial_keepalive`` touches."""

    def __init__(self):
        self.connected_event = threading.Event()
        self.disconnected_event = threading.Event()
        self.disconnected_event.set()
        self.stop_event = threading.Event()
        self.device = None
        self.device_history = []

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        if name == 'device' and hasattr(self, 'device_history'):
            self.device_history.append(value)


async def _tick_until(parent, ticks, n_ticks, timeout_s):
    """Sibling task: count scheduler turns, then ask the keepalive to stop."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ticks.append(1)
        if len(ticks) >= n_ticks:
            break
        await asyncio.sleep(0.002)
    parent.stop_event.set()


def _run_keepalive(parent, n_ticks=25, timeout_s=10, **kwargs):
    ticks = []

    async def main():
        await asyncio.wait_for(
            asyncio.gather(_async_serial_keepalive(parent, port='FAKE0',
                                                   **kwargs),
                           _tick_until(parent, ticks, n_ticks, timeout_s / 2)),
            timeout=timeout_s)

    asyncio.run(main())
    return ticks


# --------------------------------------------------------------------------
# Event-loop starvation
# --------------------------------------------------------------------------
def test_keepalive_does_not_starve_siblings_when_no_device_is_present(
        fake_serial):
    """The reconnect loop yields even when opening the port fails synchronously.

    ``AsyncSerial(...)`` raises *synchronously* when no device is connected, so
    nothing in the loop body awaits along that path.  Without the trailing
    ``await asyncio.sleep()`` the keepalive coroutine spins forever and the
    packet reader never runs.
    """
    fake_serial.fail_to_open = True
    parent = _KeepaliveParent()

    ticks = _run_keepalive(parent)

    assert len(ticks) >= 25, 'sibling task was starved by the keepalive loop'
    assert len(fake_serial.instances) > 1, 'no reconnect was attempted'
    assert parent.device is None


def test_keepalive_does_not_starve_siblings_when_the_device_dies(fake_serial):
    """A hot-unplugged device is detected and the loop keeps yielding.

    Reading ``in_waiting`` *is* the disconnect detector.  It must not be
    written as an ``assert``: assertions are stripped under ``python -O``,
    which would silently disable disconnect detection (and hence reconnection)
    entirely.
    """
    parent = _KeepaliveParent()

    async def unplug_soon():
        while not parent.connected_event.is_set():
            await asyncio.sleep(0.002)
        fake_serial.instances[0].unplugged = True

    ticks = []

    async def main():
        await asyncio.wait_for(
            asyncio.gather(_async_serial_keepalive(parent, port='FAKE0'),
                           unplug_soon(),
                           _tick_until(parent, ticks, 25, 5)),
            timeout=10)

    asyncio.run(main())

    assert len(ticks) >= 25
    assert len(fake_serial.instances) > 1, 'device was never replaced'


# --------------------------------------------------------------------------
# Reconnect bookkeeping
# --------------------------------------------------------------------------
def test_device_is_nulled_in_the_gap_and_replaced_on_reconnect(fake_serial):
    """``parent.device`` is ``None`` between a disconnect and the reconnect.

    Otherwise a consumer polling ``monitor.device`` from a ``disconnected``
    handler could not tell "no device" from "device that is about to be
    replaced", and would happily write to a dead port.
    """
    parent = _KeepaliveParent()

    async def unplug_then_stop():
        while not parent.connected_event.is_set():
            await asyncio.sleep(0.002)
        first = parent.device
        assert first is fake_serial.instances[0]
        first.unplugged = True
        # Wait for a *different* device object to be published.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if parent.device is not None and parent.device is not first:
                break
            await asyncio.sleep(0.002)
        second = parent.device
        parent.stop_event.set()
        return first, second

    async def main():
        _, pair = await asyncio.wait_for(
            asyncio.gather(_async_serial_keepalive(parent, port='FAKE0'),
                           unplug_then_stop()),
            timeout=15)
        return pair

    first, second = asyncio.run(main())

    assert second is not None and second is not first, 'never reconnected'
    # `device` was explicitly cleared between the two live devices ...
    history = parent.device_history
    assert None in history[history.index(first):history.index(second)]
    # ... and is cleared again once the loop exits.
    assert parent.device is None
    assert parent.disconnected_event.is_set()
    assert not parent.connected_event.is_set()


def test_connected_signal_carries_the_device(fake_serial):
    """The ``connected`` signal payload includes the *live* device.

    The device is assigned **before** ``connected_event.set()``, which is what
    fires the signal; assigning afterwards would hand subscribers ``None``.
    """
    monitor = AsyncSerialMonitor(port='FAKE0')
    payloads = []
    monitor.serial_signals.signal('connected').connect(
        lambda message, **kwargs: payloads.append(message), weak=False)

    monitor.start()
    try:
        assert monitor.connected_event.wait(10), 'never connected'
        deadline = time.monotonic() + 5
        while not payloads and time.monotonic() < deadline:
            time.sleep(0.01)
        assert payloads, 'no `connected` signal was sent'
        assert payloads[0]['event'] == 'connected'
        assert payloads[0]['device'] is not None
        assert payloads[0]['device'] is fake_serial.instances[0]
    finally:
        monitor.stop()
        monitor.join(10)


def test_signals_are_transition_gated_not_sent_per_iteration(fake_serial):
    """No ``disconnected`` storm while no device is available.

    ``disconnected_event.set()`` runs on *every* iteration of the reconnect
    loop (~100 times a second).  Only an actual set/clear **transition** may
    send a signal.
    """
    fake_serial.fail_to_open = True
    monitor = AsyncSerialMonitor(port='FAKE0')
    disconnects = []
    monitor.serial_signals.signal('disconnected').connect(
        lambda message, **kwargs: disconnects.append(message), weak=False)

    monitor.start()
    try:
        time.sleep(0.5)         # ~50 reconnect attempts
        assert len(fake_serial.instances) > 10, 'loop did not iterate'
        assert disconnects == [], (
            f'{len(disconnects)} `disconnected` signals for zero transitions')
    finally:
        monitor.stop()
        monitor.join(10)


def test_connect_unplug_reconnect_sends_one_signal_per_transition(fake_serial):
    """Each connect/disconnect transition sends exactly one signal."""
    monitor = AsyncSerialMonitor(port='FAKE0')
    events = []
    for name in ('connected', 'disconnected'):
        monitor.serial_signals.signal(name).connect(
            lambda message, _n=name, **kwargs: events.append(_n), weak=False)

    monitor.start()
    try:
        assert monitor.connected_event.wait(10)
        deadline = time.monotonic() + 5
        while 'connected' not in events and time.monotonic() < deadline:
            time.sleep(0.01)
        first = fake_serial.instances[0]
        first.unplugged = True
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if events.count('connected') >= 2:
                break
            time.sleep(0.01)
    finally:
        monitor.stop()
        monitor.join(10)

    assert events.count('connected') >= 2, events
    assert events.count('disconnected') >= 1, events
    # Signals strictly alternate: no repeat of the same transition in a row.
    assert all(a != b for a, b in zip(events, events[1:])), events


def test_stop_returns_promptly_and_leaves_the_thread_dead(fake_serial):
    """``stop()`` is bounded even though the reader is blocked on a read."""
    monitor = BaseNodeSerialMonitor(port='FAKE0')
    monitor.start()
    try:
        assert monitor.connected_event.wait(10)
        started = time.monotonic()
        monitor.stop()
        monitor.join(10)
        elapsed = time.monotonic() - started
    finally:
        monitor.stop_event.set()

    assert elapsed < 5, f'stop() took {elapsed:.1f}s'
    assert not monitor.is_alive()
    assert monitor.disconnected_event.is_set()
    assert monitor.device is None


# --------------------------------------------------------------------------
# `_submit_request()` on a monitor that is not running
# --------------------------------------------------------------------------
@pytest.mark.parametrize('loop_state', ['never-created', 'closed'])
def test_submit_request_on_a_dead_loop_raises_without_warning(loop_state):
    """A request issued after ``stop()`` raises cleanly and awaits its coroutine.

    A ``__del__`` issuing a final RPC during interpreter shutdown used to leave
    a ``coroutine ... was never awaited`` :class:`RuntimeWarning` behind (and
    an opaque ``AttributeError``).  The never-scheduled coroutine is now closed
    explicitly and a named :class:`RuntimeError` is raised instead.
    """
    monitor = BaseNodeSerialMonitor(port='FAKE0')
    if loop_state == 'never-created':
        monitor.loop = None
    else:
        loop = asyncio.new_event_loop()
        loop.close()
        monitor.loop = loop

    request = b'|||\x00\x00d\x00\x02\x01\x00\x00\x00'
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        with pytest.raises(RuntimeError, match='event loop is not running'):
            monitor._submit_request(request)
        # The warning is emitted when the orphaned coroutine is collected.
        gc.collect()

    never_awaited = [w for w in caught
                     if issubclass(w.category, RuntimeWarning)
                     and 'never awaited' in str(w.message)]
    assert not never_awaited, [str(w.message) for w in never_awaited]


def test_request_max_retries_raises_timeout_error():
    """``request()`` gives up after ``max_retries`` rather than looping forever."""
    monitor = BaseNodeSerialMonitor(port='FAKE0')
    attempts = []

    async def never_answers(request, iuid=None, **kwargs):
        attempts.append(iuid)
        await asyncio.sleep(30)

    monitor.arequest = never_answers

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(10)
    monitor.loop = loop
    try:
        with pytest.raises(TimeoutError):
            monitor.request(b'|||\x00\x00d\x00\x02\x01\x00\x00\x00',
                            timeout=0.1, max_retries=3)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        loop.close()

    assert len(attempts) == 3, attempts
    assert len(set(attempts)) == 3, 'retries must not reuse an IUID'
