# coding: utf-8
"""
``IUID`` request/response correlation in
:class:`base_node_rpc._async_base.BaseNodeSerialMonitor`.

Before correlation, a request that timed out and was re-sent left a *late*
response in flight.  That late response was then delivered as the answer to the
**next** command, so every subsequent command received the previous command's
result -- a silent, permanent one-packet desynchronization.

Each request now carries a fresh ``IUID`` (``1..65535``, never ``0``) stamped
into the packet header, which the firmware echoes.  A response whose ``IUID``
matches neither the request nor ``0`` is a late response and is discarded.
Firmware that does not echo the ``IUID`` replies with ``0``, which keeps the
previous FIFO semantics exactly.

Hardware-free: every test drives the real coroutines against an in-memory fake
device.  No serial port, no network, no firmware.
"""
import asyncio
import itertools
import threading

import pytest

from nadamq.NadaMq import FLAGS, PACKET_TYPES, cPacket, cPacketParser

from base_node_rpc._async_base import (_IUID_MAX, _IUID_OFFSET, _IUID_SIZE,
                                       BaseNodeSerialMonitor, _stamp_iuid,
                                       _serialized_iuid_offset)


def request_bytes(payload=b'\x01\x00cmd'):
    """Serialize a request exactly as the generated proxy classes do (``IUID`` 0)."""
    return cPacket(data=payload, type_=PACKET_TYPES.DATA, iuid=0).tobytes()


def parse_one(raw):
    packet = cPacketParser().parse(raw)
    assert packet is not False, f'did not parse: {raw!r}'
    return packet


# --------------------------------------------------------------------------
# Stamping
# --------------------------------------------------------------------------
def test_iuid_offset_is_derived_from_the_wire_format():
    """The ``IUID`` offset is derived, not hard coded.

    Guards against a change to the NadaMQ wire format silently turning ``IUID``
    stamping into payload corruption: the offset is recomputed by diffing two
    serialized packets rather than assumed.
    """
    assert _IUID_OFFSET == _serialized_iuid_offset() == len(FLAGS.START) == 3
    assert _IUID_SIZE == 2
    assert _IUID_MAX == 0xFFFF
    raw = cPacket(data=b'zz', type_=PACKET_TYPES.DATA, iuid=0xBEEF).tobytes()
    assert raw[_IUID_OFFSET:_IUID_OFFSET + _IUID_SIZE] == b'\xbe\xef'


@pytest.mark.parametrize('iuid', [1, 2, 255, 256, 0x1234, 0xFFFE, 0xFFFF])
def test_stamping_round_trips_and_leaves_the_payload_intact(iuid):
    """Stamping rewrites only the ``IUID`` field.

    The CRC covers the payload only, so the header can be overwritten in place
    -- but only if nothing *else* moves.  Guards against a stamp that shifts or
    corrupts the payload (which would still parse, with the wrong data).
    """
    base = request_bytes(b'\x2a\x00payload')
    assert parse_one(base).iuid_ == 0

    stamped = _stamp_iuid(base, iuid)
    packet = parse_one(stamped)
    assert packet.iuid_ == iuid
    assert packet.type_ == PACKET_TYPES.DATA
    assert bytes(packet.data()) == b'\x2a\x00payload'
    assert stamped[:_IUID_OFFSET] == base[:_IUID_OFFSET]
    assert (stamped[_IUID_OFFSET + _IUID_SIZE:] ==
            base[_IUID_OFFSET + _IUID_SIZE:])


@pytest.mark.parametrize('junk', [b'', b'ab', b'not-a-packet-at-all',
                                  b'|||' b'\x00'])
def test_stamping_leaves_non_packets_untouched(junk):
    """Input that is not a serialized packet is returned *identically*.

    ``_stamp_request()`` reports ``IUID`` 0 in that case, which makes
    ``arequest()`` fall back to legacy FIFO matching rather than waiting
    forever for an echo that can never come.
    """
    assert _stamp_iuid(junk, 7) is junk
    monitor = BaseNodeSerialMonitor(port='FAKE')
    stamped, iuid = monitor._stamp_request(junk)
    assert iuid == 0 and stamped == junk


def test_counter_starts_at_one_and_wraps_without_ever_returning_zero():
    """``IUID`` 0 is reserved and must never be handed out.

    ``0`` means "not correlated" -- it is what a legacy driver stamps and a
    legacy firmware echoes -- so a counter that returned it (e.g. a bare
    ``% 65536``) would silently disable correlation once per cycle.
    """
    monitor = BaseNodeSerialMonitor(port='FAKE')
    assert [monitor._next_iuid() for _ in range(5)] == [1, 2, 3, 4, 5]

    monitor._iuid_counter = itertools.count(_IUID_MAX - 3)
    assert [monitor._next_iuid() for _ in range(6)] == [
        _IUID_MAX - 2, _IUID_MAX - 1, _IUID_MAX, 1, 2, 3]

    monitor._iuid_counter = itertools.count()
    cycle = {monitor._next_iuid() for _ in range(_IUID_MAX)}
    assert cycle == set(range(1, _IUID_MAX + 1))
    assert 0 not in cycle


def test_concurrent_stamping_hands_out_unique_iuids():
    """Eight threads stamping at once never collide.

    ``itertools.count()`` is not atomic across threads on every
    implementation, and ``request()``/``_submit_request()`` may be called from
    any thread; a duplicate ``IUID`` would make two in-flight requests
    indistinguishable.
    """
    monitor = BaseNodeSerialMonitor(port='FAKE')
    n_threads, per_thread = 8, 2000
    results = [[] for _ in range(n_threads)]
    barrier = threading.Barrier(n_threads)

    def worker(index):
        barrier.wait()
        out = results[index]
        for _ in range(per_thread):
            out.append(monitor._next_iuid())

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(n_threads)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    everything = [value for out in results for value in out]
    assert len(everything) == n_threads * per_thread
    assert len(set(everything)) == len(everything), 'duplicate IUID handed out'
    assert 0 not in everything
    assert min(everything) >= 1 and max(everything) <= _IUID_MAX


def test_stamp_request_is_fresh_per_call():
    """Every ``_stamp_request()`` call produces a *new* ``IUID``.

    This is what makes the re-send issued by ``request()`` after a timeout
    distinguishable from the attempt that timed out.
    """
    monitor = BaseNodeSerialMonitor(port='FAKE')
    base = request_bytes()
    seen = []
    for _ in range(4):
        stamped, iuid = monitor._stamp_request(base)
        assert parse_one(stamped).iuid_ == iuid
        seen.append(iuid)
    assert seen == [1, 2, 3, 4]
    assert parse_one(base).iuid_ == 0, 'the caller\'s bytes were mutated'


# --------------------------------------------------------------------------
# Fake device / monitor plumbing
# --------------------------------------------------------------------------
class _FakeDevice:
    """Serial device that answers ``DATA`` requests under a configurable policy.

    Parameters
    ----------
    echo_iuid : bool
        ``True`` -> firmware that echoes the request ``IUID``.
        ``False`` -> legacy firmware that always answers with ``IUID`` 0.
    hold : set of int, optional
        Request ``IUID``s whose response is *withheld* (simulating a request
        that times out).
    release_with : dict, optional
        ``{trigger_iuid: [held_iuid, ...]}`` -- writing ``trigger_iuid`` first
        emits the responses held for ``held_iuid``.
    stream_events : int
        Number of ``STREAM`` event packets to interleave before each response.
    """

    is_open = True

    def __init__(self, echo_iuid=True, hold=None, release_with=None,
                 stream_events=0):
        self.echo_iuid = echo_iuid
        self.hold = set(hold or ())
        self.release_with = dict(release_with or {})
        self.stream_events = stream_events
        self.out = bytearray()
        self.held = {}
        self.written = []

    def _response(self, iuid, payload):
        return cPacket(data=payload, type_=PACKET_TYPES.DATA,
                       iuid=iuid if self.echo_iuid else 0).tobytes()

    async def write(self, data):
        packet = parse_one(bytes(data))
        iuid = packet.iuid_
        self.written.append((iuid, bytes(packet.data())))
        payload = b'resp:' + bytes(packet.data())

        for released in self.release_with.get(iuid, ()):
            if released in self.held:
                self.out += self.held.pop(released)

        if iuid in self.hold:
            self.held[iuid] = self._response(iuid, payload)
            return

        for i in range(self.stream_events):
            self.out += cPacket(
                data=b'{"event": "capacitance-updated", "n": %d}' % i,
                type_=PACKET_TYPES.STREAM).tobytes()
        self.out += self._response(iuid, payload)

    async def read(self, n_bytes):
        if not self.out:
            await asyncio.sleep(0.001)
            return b''
        chunk = bytes(self.out[:n_bytes])
        del self.out[:n_bytes]
        return chunk


def _make_monitor(device):
    monitor = BaseNodeSerialMonitor(port='FAKE')
    monitor._request_queue = asyncio.Queue()
    monitor._request_lock = asyncio.Lock()
    monitor.device = device
    return monitor


def _run_with_reader(monitor, body, timeout=20):
    """Run ``body()`` with ``read_packets()`` pumping alongside it."""

    async def main():
        task = asyncio.get_running_loop().create_task(monitor.read_packets())
        try:
            return await asyncio.wait_for(body(), timeout=timeout)
        finally:
            monitor.stop_event.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):     # noqa: BLE001
                pass

    return asyncio.run(main())


class _LoopThread:
    """Run an asyncio loop in a background thread, as the monitor does."""

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.call_soon(self.ready.set)
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert self.ready.wait(10)
        return self.loop

    def __exit__(self, *exc_info):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


# --------------------------------------------------------------------------
# Firmware scenarios
# --------------------------------------------------------------------------
def test_legacy_firmware_keeps_fifo_semantics():
    """Firmware that always answers ``IUID`` 0 behaves exactly as before.

    Guards against the correlation change breaking every existing firmware
    build (none of which echo the request ``IUID``).
    """
    device = _FakeDevice(echo_iuid=False)
    monitor = _make_monitor(device)

    async def body():
        out = []
        for i in range(5):
            stamped, iuid = monitor._stamp_request(request_bytes(b'cmd%d' % i))
            packet = await monitor.arequest(stamped, iuid=iuid)
            out.append((packet.iuid_, bytes(packet.data())))
        return out

    assert _run_with_reader(monitor, body) == [
        (0, b'resp:cmd%d' % i) for i in range(5)]
    # A fresh, non-zero IUID was still written on every request.
    assert [iuid for iuid, _ in device.written] == [1, 2, 3, 4, 5]


def test_new_firmware_responses_are_correlated():
    """Firmware that echoes the request ``IUID`` gets correlated responses."""
    device = _FakeDevice(echo_iuid=True)
    monitor = _make_monitor(device)

    async def body():
        out = []
        for i in range(5):
            stamped, iuid = monitor._stamp_request(request_bytes(b'cmd%d' % i))
            packet = await monitor.arequest(stamped, iuid=iuid)
            out.append((iuid, packet.iuid_, bytes(packet.data())))
        return out

    assert _run_with_reader(monitor, body) == [
        (i + 1, i + 1, b'resp:cmd%d' % i) for i in range(5)]


def test_arequest_stamps_when_iuid_is_omitted():
    """Calling ``arequest()`` directly is correlated too.

    ``iuid=None`` (the default) stamps a fresh ``IUID`` rather than silently
    disabling correlation for direct callers.
    """
    device = _FakeDevice(echo_iuid=True)
    monitor = _make_monitor(device)

    async def body():
        packet = await monitor.arequest(request_bytes(b'direct'))
        return packet.iuid_, bytes(packet.data())

    assert _run_with_reader(monitor, body) == (1, b'resp:direct')


def test_late_response_to_a_timed_out_request_is_discarded():
    """THE defect: request A times out, A's answer arrives after B was sent.

    B's ``arequest()`` must discard the ``IUID``-A packet and return the
    ``IUID``-B response.  A's payload must never surface as B's result.  The
    device releases A's response only *after* B has been written, i.e. after
    the stale-response drain has already run -- so only ``IUID`` correlation
    can catch it.
    """
    device = _FakeDevice(echo_iuid=True, hold={1}, release_with={2: [1]})
    monitor = _make_monitor(device)

    async def body():
        stamped_a, iuid_a = monitor._stamp_request(request_bytes(b'AAA'))
        assert iuid_a == 1
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(monitor.arequest(stamped_a, iuid=iuid_a),
                                   timeout=0.2)
        stamped_b, iuid_b = monitor._stamp_request(request_bytes(b'BBB'))
        assert iuid_b == 2
        packet = await monitor.arequest(stamped_b, iuid=iuid_b)
        return packet.iuid_, bytes(packet.data())

    response_iuid, payload = _run_with_reader(monitor, body)
    assert response_iuid == 2
    assert payload == b'resp:BBB'
    # Both requests really were written, and A's response really was released
    # after B -- otherwise the test would prove nothing.
    assert [iuid for iuid, _ in device.written] == [1, 2]
    assert not device.held, "request A's response was never released"


def test_legacy_firmware_still_misdelivers_the_late_response():
    """Contrast case: with ``IUID``-0 firmware the same scenario mis-delivers.

    Documented deliberately: correlation changes the *correlated* path only,
    and this is the pre-existing behaviour it cannot fix without firmware
    support.  If this ever starts passing, the legacy path changed too.
    """
    device = _FakeDevice(echo_iuid=False, hold={1}, release_with={2: [1]})
    monitor = _make_monitor(device)

    async def body():
        stamped_a, iuid_a = monitor._stamp_request(request_bytes(b'AAA'))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(monitor.arequest(stamped_a, iuid=iuid_a),
                                   timeout=0.2)
        stamped_b, iuid_b = monitor._stamp_request(request_bytes(b'BBB'))
        packet = await monitor.arequest(stamped_b, iuid=iuid_b)
        return bytes(packet.data())

    assert _run_with_reader(monitor, body) == b'resp:AAA'


def test_stream_events_do_not_leak_on_to_the_request_queue():
    """Interleaved ``STREAM`` events never desynchronize correlated responses."""
    device = _FakeDevice(echo_iuid=True, stream_events=3)
    monitor = _make_monitor(device)
    events = []
    monitor.signals.signal('capacitance-updated').connect(
        lambda message, **kwargs: events.append(message['n']), weak=False)

    async def body():
        out = []
        for i in range(6):
            stamped, iuid = monitor._stamp_request(request_bytes(b'c%d' % i))
            packet = await monitor.arequest(stamped, iuid=iuid)
            out.append((packet.iuid_, bytes(packet.data())))
        await asyncio.sleep(0.05)       # let the reader drain trailing events
        return out

    assert _run_with_reader(monitor, body) == [
        (i + 1, b'resp:c%d' % i) for i in range(6)]
    assert events == [0, 1, 2] * 6
    assert monitor._request_queue.empty()


def test_arequest_raises_serial_exception_when_no_device_is_connected():
    """A request issued while disconnected names the cause.

    ``self.device`` is ``None`` in the gap between a disconnect and the next
    reconnect; a bare ``AttributeError: 'NoneType' object has no attribute
    'write'`` used to escape instead.
    """
    import serial

    monitor = _make_monitor(device=None)

    async def body():
        with pytest.raises(serial.SerialException, match='No serial device'):
            await monitor.arequest(request_bytes(b'nope'))
        return True

    async def main():
        return await asyncio.wait_for(body(), timeout=10)

    assert asyncio.run(main()) is True


def test_arequest_drains_stale_responses_before_sending():
    """Responses already queued when a request starts are discarded.

    Anything sitting on the request queue at that point is an answer nobody is
    waiting for; leaving it there would desynchronize the queue by one packet
    so that every later command received the previous command's response.
    """
    device = _FakeDevice(echo_iuid=True)
    monitor = _make_monitor(device)

    async def body():
        # Plant two orphaned responses, as a pair of timed-out requests would.
        for stale_iuid in (900, 901):
            await monitor._request_queue.put(
                cPacketParser().parse(
                    cPacket(data=b'stale', type_=PACKET_TYPES.DATA,
                            iuid=stale_iuid).tobytes()))
        stamped, iuid = monitor._stamp_request(request_bytes(b'fresh'))
        packet = await monitor.arequest(stamped, iuid=iuid)
        return packet.iuid_, bytes(packet.data()), monitor._request_queue.qsize()

    response_iuid, payload, remaining = _run_with_reader(monitor, body)
    assert payload == b'resp:fresh'
    assert response_iuid == 1
    assert remaining == 0


# --------------------------------------------------------------------------
# Threaded `request()` / `_submit_request()`
# --------------------------------------------------------------------------
def test_submit_request_stamps_and_plumbs_the_iuid():
    """``_submit_request()`` stamps the bytes and passes the ``IUID`` on.

    If the stamp and the ``IUID`` handed to ``arequest()`` ever diverge, every
    response looks late and the caller hangs until its timeout.
    """
    monitor = BaseNodeSerialMonitor(port='FAKE')
    seen = []

    async def fake_arequest(request, iuid=None, **kwargs):
        seen.append((request, iuid))
        return cPacket(data=b'ok', type_=PACKET_TYPES.DATA, iuid=iuid or 0)

    monitor.arequest = fake_arequest
    base = request_bytes(b'\x07\x00go')
    with _LoopThread() as loop:
        monitor.loop = loop
        for _ in range(3):
            monitor._submit_request(base).result(10)

    assert [iuid for _, iuid in seen] == [1, 2, 3]
    for request, iuid in seen:
        assert parse_one(request).iuid_ == iuid
        assert parse_one(base).iuid_ == 0, 'caller bytes were mutated in place'
        assert (request[:_IUID_OFFSET] + request[_IUID_OFFSET + _IUID_SIZE:] ==
                base[:_IUID_OFFSET] + base[_IUID_OFFSET + _IUID_SIZE:])


def test_request_retry_uses_a_fresh_iuid():
    """A timed-out ``request()`` re-send carries a **new** ``IUID``.

    That is precisely what makes the late response to the timed-out attempt
    recognizable -- and therefore discardable -- by ``arequest()``.
    """
    monitor = BaseNodeSerialMonitor(port='FAKE')
    seen = []

    async def fake_arequest(request, iuid=None, **kwargs):
        seen.append(iuid)
        if len(seen) == 1:
            await asyncio.sleep(30)     # first attempt never answers
        return cPacket(data=b'ok', type_=PACKET_TYPES.DATA, iuid=iuid or 0)

    monitor.arequest = fake_arequest
    with _LoopThread() as loop:
        monitor.loop = loop
        packet = monitor.request(request_bytes(b'retry'), timeout=0.2,
                                 max_retries=3)

    assert seen == [1, 2]
    assert packet.iuid_ == 2
