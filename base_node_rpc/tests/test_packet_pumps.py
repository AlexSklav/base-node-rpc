# coding: utf-8
"""
Behavioural tests for the two packet "pumps" in ``base_node_rpc``:

* :meth:`base_node_rpc.queue.PacketQueueManager.parse` (synchronous, used by
  :class:`base_node_rpc.proxy.SerialProxyMixin` through
  ``PacketProtocol.data_received``), and
* :meth:`base_node_rpc._async_base.BaseNodeSerialMonitor.read_packets`
  (asynchronous, used by the serial monitor thread).

Both were reworked to feed the NadaMQ parser whole **chunks** (resuming at
:attr:`cPacketParser.bytes_consumed`) rather than a byte at a time, and to
dispatch the packet returned by ``parse()`` directly instead of re-serializing
and re-parsing it.  The regressions that rework could introduce are all
*boundary* effects, so every pump test runs at chunk sizes that straddle the
start flag, the header, and whole/multiple packets per call.

Hardware-free: streams are built in memory and fed to the pumps directly.  No
serial port, no network, no firmware.
"""
import asyncio
import json
import logging
import threading

import blinker
import pytest

from nadamq.NadaMq import PACKET_TYPES, cPacket

from base_node_rpc._async_base import BaseNodeSerialMonitor
from base_node_rpc.queue import PacketQueueManager, _header_only_packet

from ._streams import (CHUNK_SIZES, chunks, connect_queue_signals,
                       corrupted_stream, event_payload, iter_parse, mixed_stream,
                       mk)


# --------------------------------------------------------------------------
# `PacketQueueManager.parse`
# --------------------------------------------------------------------------
def test_five_distinct_packet_queues():
    """Each packet type gets its **own** queue object.

    Guards against the queues being aliased to a single shared
    :class:`queue.Queue` (an earlier check only proved the *names* were
    distinct), and against the ``nack`` queue -- added so that a rejected
    request is distinguishable from an unanswered one -- going missing again.
    """
    manager = PacketQueueManager()
    assert set(manager.packet_queues) == {'data', 'ack', 'nack', 'stream',
                                          'id_response'}
    assert len({id(q) for q in manager.packet_queues.values()}) == 5


@pytest.mark.parametrize('chunk_size', CHUNK_SIZES)
def test_parse_mixed_stream_routes_every_type(chunk_size):
    """Every packet type is parsed and routed, independent of chunk size.

    Guards the chunked-feed rework: a packet completing part-way through a
    chunk must not swallow the remainder of the chunk, and a packet split
    across chunk boundaries must still complete.
    """
    stream, expected, _ = mixed_stream(n_packets=12, stream_events=False)
    manager = PacketQueueManager(high_water_mark=None)
    received = []
    connect_queue_signals(manager.signals, received)

    iter_parse(manager, stream, chunk_size)

    assert received == expected
    for name in ('data', 'ack', 'nack', 'stream', 'id_response'):
        queued = [packet.iuid_
                  for _, packet in list(manager.packet_queues[name].queue)]
        assert queued == [iuid for n, iuid in expected if n == name]


@pytest.mark.parametrize('chunk_size', CHUNK_SIZES)
def test_parse_recovers_from_mid_chunk_crc_corruption(chunk_size):
    """A corrupted packet is dropped; the packets behind it survive.

    Guards the decision *not* to call ``parser.reset()`` on ``parser.error``:
    ``error`` describes the whole call rather than its last byte, so an
    unconditional reset would discard a packet left partially parsed at the end
    of a chunk.
    """
    stream, expected = corrupted_stream()
    manager = PacketQueueManager(high_water_mark=None)
    received = []
    connect_queue_signals(manager.signals, received)

    iter_parse(manager, stream, chunk_size)

    assert received == expected


@pytest.mark.parametrize('chunk_size', CHUNK_SIZES)
@pytest.mark.parametrize('packet_type', [PACKET_TYPES.ACK, PACKET_TYPES.NACK])
def test_parse_completes_header_only_packets(chunk_size, packet_type):
    """``ACK``/``NACK`` packets complete even when fed byte-at-a-time.

    A header-only packet carries no length/payload/CRC, so it used to be
    completed only when the whole 6-byte packet arrived in a single
    ``parse()`` call -- otherwise it never completed *and* corrupted the packet
    behind it (the start flag of the next packet was consumed as the ``ACK``
    payload length).
    """
    name = 'ack' if packet_type == PACKET_TYPES.ACK else 'nack'
    stream = (mk(packet_type, 11) +
              mk(PACKET_TYPES.DATA, 12, b'behind-the-header-only-packet'))
    manager = PacketQueueManager(high_water_mark=None)
    received = []
    connect_queue_signals(manager.signals, received)

    iter_parse(manager, stream, chunk_size)

    assert received == [(name, 11), ('data', 12)]
    _, data_packet = manager.packet_queues['data'].get_nowait()
    assert bytes(data_packet.data()) == b'behind-the-header-only-packet'


def test_header_only_packet_helper_ignores_other_types():
    """:func:`_header_only_packet` only ever completes ``ACK``/``NACK``.

    Guards against the helper synthesizing a bogus packet from the partially
    parsed header of a ``DATA``/``STREAM`` packet.
    """
    from nadamq.NadaMq import cPacketParser

    parser = cPacketParser()
    for byte in mk(PACKET_TYPES.DATA, 5, b'x')[:6]:
        parser.parse(bytes([byte]))
        assert _header_only_packet(parser) is None


def test_parse_evicts_oldest_packet_at_high_water_mark():
    """A full queue drops the **oldest** packet, keeping the newest.

    Only the ``data`` queue has a consumer, so the others would otherwise grow
    without bound or stay full forever (dropping every *new* packet).  The
    ``<type>-full`` signal must still fire so a subscriber can notice a queue
    that is not being drained.
    """
    manager = PacketQueueManager(high_water_mark=3)
    full_signals = []
    manager.signals.signal('data-full').connect(
        lambda *args, **kwargs: full_signals.append(1), weak=False)

    manager.parse(b''.join(mk(PACKET_TYPES.DATA, i, bytes([i]))
                           for i in range(1, 7)))

    queued = [packet.iuid_
              for _, packet in list(manager.packet_queues['data'].queue)]
    assert queued == [4, 5, 6], 'newest packets must be the survivors'
    assert manager.packet_queues['data'].qsize() == 3
    assert len(full_signals) == 3


def test_parse_unbounded_queue_when_high_water_mark_is_none():
    """``high_water_mark=None`` disables eviction entirely."""
    manager = PacketQueueManager(high_water_mark=None)
    manager.parse(b''.join(mk(PACKET_TYPES.DATA, i, b'x') for i in range(1, 21)))
    assert manager.packet_queues['data'].qsize() == 20
    assert manager.queue_full('data') is False


@pytest.mark.parametrize('chunk_size', CHUNK_SIZES)
def test_parse_dispatches_events_without_queueing_them(chunk_size):
    """A ``STREAM`` event is signalled, not queued; a non-event is queued.

    Guards the fix that keeps the ``stream`` queue from filling up with rapidly
    occurring events (which would then evict, or be evicted by, real stream
    payloads).
    """
    manager = PacketQueueManager(high_water_mark=None)
    events = []
    manager.signals.signal('capacitance-updated').connect(
        lambda message, **kwargs: events.append(message['n']), weak=False)

    stream = b''.join([mk(PACKET_TYPES.STREAM, 1, event_payload(0)),
                       mk(PACKET_TYPES.STREAM, 2, b'plain text, not JSON'),
                       mk(PACKET_TYPES.STREAM, 3,
                          json.dumps({'no': 'event key'}).encode('utf8')),
                       mk(PACKET_TYPES.STREAM, 4, event_payload(1))])
    iter_parse(manager, stream, chunk_size)

    assert events == [0, 1]
    queued = [packet.iuid_
              for _, packet in list(manager.packet_queues['stream'].queue)]
    assert queued == [2, 3], 'only non-event STREAM packets are queued'


def test_parse_event_subscriber_exception_does_not_stop_the_pump():
    """An exception raised by an event subscriber is logged, not propagated.

    The signal is dispatched *outside* the ``try`` that decodes the payload, so
    a failing subscriber must not be misreported as a malformed event packet --
    and must not abandon the rest of the chunk.
    """
    manager = PacketQueueManager(high_water_mark=None)

    def boom(message, **kwargs):
        raise RuntimeError('subscriber exploded')

    manager.signals.signal('capacitance-updated').connect(boom, weak=False)
    received = []
    connect_queue_signals(manager.signals, received)

    manager.parse(mk(PACKET_TYPES.STREAM, 1, event_payload(0)) +
                  mk(PACKET_TYPES.DATA, 2, b'still parsed'))

    assert received == [('data', 2)]


def test_parse_non_utf8_stream_payload_is_survivable():
    """A ``STREAM`` payload that is not valid UTF-8 does not abandon the chunk.

    The ``except`` handler used to re-run ``.decode('utf8')`` on the payload --
    i.e. re-raise the very error it was meant to catch -- dropping the packet
    *and* the remainder of the read chunk.
    """
    manager = PacketQueueManager(high_water_mark=None)
    received = []
    connect_queue_signals(manager.signals, received)

    manager.parse(mk(PACKET_TYPES.STREAM, 1, b'\xff\xfe\x00\x80') +
                  mk(PACKET_TYPES.DATA, 2, b'behind the bad stream packet'))

    assert received == [('stream', 1), ('data', 2)]


@pytest.mark.parametrize('data', [b'', '', None, []])
def test_parse_ignores_empty_input(data):
    """Empty input is a no-op rather than an error or an infinite loop."""
    manager = PacketQueueManager()
    manager.parse(data)
    assert all(q.empty() for q in manager.packet_queues.values())


def test_parse_consumes_bytes_and_never_raises_on_other_shapes():
    """``bytes`` is the parsed input shape; nothing else raises.

    ``parse()`` is called from ``PacketProtocol.data_received`` and from the
    monitor read loop, both of which always hand it ``bytes``.  Other shapes
    (``bytearray``, ``str``, sequences of ints/bytes) are currently *not*
    parsed -- they are dropped silently rather than raising -- and that
    tolerance is what keeps a stray call from killing the reader thread.  This
    pins both halves of that contract.
    """
    packet = mk(PACKET_TYPES.DATA, 9, b'abc')

    manager = PacketQueueManager(high_water_mark=None)
    manager.parse(packet)
    assert manager.packet_queues['data'].qsize() == 1
    _, parsed = manager.packet_queues['data'].get_nowait()
    assert bytes(parsed.data()) == b'abc'

    for payload in (bytearray(packet), packet.decode('latin1'), list(packet),
                    [bytes([b]) for b in packet]):
        other = PacketQueueManager(high_water_mark=None)
        other.parse(payload)          # must not raise
        assert all(q.empty() for q in other.packet_queues.values()), payload


# --------------------------------------------------------------------------
# `BaseNodeSerialMonitor.read_packets`
# --------------------------------------------------------------------------
class _DrainingDevice:
    """Fake serial device that yields fixed chunks and then reports EOF.

    Mirrors the ``asyncserial.AsyncSerial`` error latch: once the chunks are
    exhausted, ``is_open`` goes ``False`` and ``read()`` raises -- exactly what
    a hot-unplugged device does.
    """

    def __init__(self, chunk_list, monitor):
        self.chunks = list(chunk_list)
        self.monitor = monitor
        self.is_open = True
        self.written = []

    async def read(self, n_bytes):
        if self.chunks:
            await asyncio.sleep(0)
            return self.chunks.pop(0)
        self.is_open = False
        self.monitor.stop_event.set()
        raise OSError('drained')

    async def write(self, data):
        self.written.append(bytes(data))
        return len(data)


def _pump_read_packets(stream, chunk_size):
    """Drive ``read_packets`` over :data:`stream` and return what it produced."""
    monitor = BaseNodeSerialMonitor.__new__(BaseNodeSerialMonitor)
    monitor.stop_event = threading.Event()
    monitor.signals = blinker.Namespace()
    received = []
    connect_queue_signals(monitor.signals, received)
    events = []
    monitor.signals.signal('capacitance-updated').connect(
        lambda message, **kwargs: events.append(message['n']), weak=False)
    monitor.device = _DrainingDevice(chunks(stream, chunk_size), monitor)

    async def run():
        monitor._request_queue = asyncio.Queue()
        await asyncio.wait_for(monitor.read_packets(), timeout=20)
        return monitor._request_queue

    request_queue = asyncio.run(run())
    return received, events, request_queue


@pytest.mark.parametrize('chunk_size', CHUNK_SIZES)
def test_read_packets_signals_every_type_in_order(chunk_size):
    """``read_packets`` parses the same sequence as the synchronous pump.

    Guards the chunked-feed rework in the monitor read loop, at every chunk
    boundary.
    """
    stream, expected, _ = mixed_stream(n_packets=12, stream_events=False)
    received, events, _ = _pump_read_packets(stream, chunk_size)
    assert received == expected
    assert events == []


@pytest.mark.parametrize('chunk_size', CHUNK_SIZES)
def test_read_packets_recovers_from_mid_chunk_crc_corruption(chunk_size):
    """A corrupted packet inside a read chunk does not take its neighbours."""
    stream, expected = corrupted_stream()
    received, _, _ = _pump_read_packets(stream, chunk_size)
    assert received == expected


@pytest.mark.parametrize('chunk_size', CHUNK_SIZES)
def test_read_packets_queues_data_but_not_nack_or_stream(chunk_size):
    """Only ``DATA`` packets reach the request queue.

    A ``NACK`` is *not* a response: putting it on the request queue would
    desynchronize the request/response pairing in ``arequest()``.  It is
    surfaced through the ``nack-received`` signal instead.  ``STREAM`` events
    are likewise dispatched as signals only.
    """
    stream, expected, n_stream = mixed_stream(n_packets=12, stream_events=True)
    received, events, request_queue = _pump_read_packets(stream, chunk_size)

    assert received == expected
    assert len(events) == n_stream
    assert request_queue.qsize() == sum(1 for name, _ in expected
                                        if name == 'data')
    queued_types = {request_queue.get_nowait().type_
                    for _ in range(request_queue.qsize())}
    assert queued_types == {PACKET_TYPES.DATA}


def test_read_packets_stops_on_stop_event_without_spinning():
    """A latched-closed device does not starve the event loop.

    ``read()`` raises *synchronously* on a dead device, so breaking straight
    out to the outer loop would re-enter ``read()`` with no ``await`` anywhere
    along the path -- starving the keepalive task, so the device is never torn
    down or reconnected.  The read loop therefore yields before breaking; this
    pins that a sibling task keeps getting scheduled.
    """
    monitor = BaseNodeSerialMonitor.__new__(BaseNodeSerialMonitor)
    monitor.stop_event = threading.Event()
    monitor.signals = blinker.Namespace()
    monitor._request_queue = None

    class _LatchedDevice:
        is_open = False
        port = 'FAKE'

        async def read(self, n_bytes):
            raise IOError('Serial port is not usable: latched')

    monitor.device = _LatchedDevice()
    ticks = []

    async def sibling():
        while not monitor.stop_event.is_set():
            ticks.append(1)
            if len(ticks) >= 20:
                monitor.stop_event.set()
            await asyncio.sleep(0.005)

    async def run():
        monitor._request_queue = asyncio.Queue()
        await asyncio.wait_for(
            asyncio.gather(sibling(), monitor.read_packets()), timeout=20)

    asyncio.run(run())
    assert len(ticks) >= 20, 'sibling task was starved by the read loop'


def test_read_packets_debug_logging_does_not_change_parsing(caplog):
    """Debug-guarded logging is a no-op for the parsed packet sequence.

    The ``numpy`` conversion and 8 KiB ``repr`` in the read loop are guarded by
    ``isEnabledFor(DEBUG)``; enabling debug must not alter what is parsed.
    """
    stream, expected, _ = mixed_stream(n_packets=6, stream_events=False)
    with caplog.at_level(logging.DEBUG, logger='base_node_rpc'):
        received, _, _ = _pump_read_packets(stream, 7)
    assert received == expected
