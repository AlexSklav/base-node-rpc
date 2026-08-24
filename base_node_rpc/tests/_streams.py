# coding: utf-8
"""
Shared, hardware-free helpers for the ``base_node_rpc`` packet-pump tests.

Every helper here builds NadaMQ byte streams in memory -- nothing in this
module (or in any test that uses it) opens a serial port, touches the network,
or talks to firmware.

.. note::
    The module name deliberately starts with an underscore so that ``pytest``
    does not try to collect it as a test module.
"""
import json
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from nadamq.NadaMq import PACKET_TYPES, cPacket

__all__ = ['CHUNK_SIZES', 'QUEUE_NAME_BY_TYPE', 'chunks', 'event_payload',
           'mk', 'mixed_stream']

#: Chunk sizes every pump test is exercised at.  1 and 3 straddle the 3-byte
#: start flag, 7 straddles the 6-byte header, and 64/512/8192 deliver whole
#: (and multiple) packets per call.
CHUNK_SIZES = (1, 3, 7, 64, 512, 8192)

#: Queue (and signal) name each packet type is routed to.  ``ID_REQUEST`` is
#: intentionally absent: it is parsed but not routed anywhere.
QUEUE_NAME_BY_TYPE: Dict[int, str] = {
    PACKET_TYPES.DATA: 'data',
    PACKET_TYPES.ACK: 'ack',
    PACKET_TYPES.NACK: 'nack',
    PACKET_TYPES.STREAM: 'stream',
    PACKET_TYPES.ID_RESPONSE: 'id_response',
}


def mk(type_: int, iuid: int, data: Optional[bytes] = None) -> bytes:
    """Serialize a single NadaMQ packet.

    ``data=None`` builds a *header-only* packet (i.e., ``ACK``/``NACK``), which
    carries no length, payload or CRC field.
    """
    if data is None:
        return cPacket(type_=type_, iuid=iuid).tobytes()
    return cPacket(type_=type_, iuid=iuid, data=data).tobytes()


def event_payload(index: int, event: str = 'capacitance-updated') -> bytes:
    """JSON payload that :meth:`PacketQueueManager.parse` recognizes as an event."""
    return json.dumps({'event': event, 'n': index}).encode('utf8')


def chunks(data: bytes, size: int) -> List[bytes]:
    """Split :data:`data` into ``size``-byte chunks (never an empty list)."""
    if not data:
        return [b'']
    return [data[i:i + size] for i in range(0, len(data), size)]


#: One full cycle of the packet types a device can emit.  ``ID_REQUEST`` is
#: included precisely because it must be parsed *and* routed nowhere.
_CYCLE: Sequence[Tuple[int, Optional[bytes]]] = (
    (PACKET_TYPES.ACK, None),
    (PACKET_TYPES.DATA, b'\x01\x02'),
    (PACKET_TYPES.NACK, None),
    (PACKET_TYPES.STREAM, None),          # payload filled in per-packet
    (PACKET_TYPES.ID_RESPONSE, b'base-node::1.2.3'),
    (PACKET_TYPES.ID_REQUEST, None),
)


def mixed_stream(n_packets: int = 12, stream_events: bool = True
                 ) -> Tuple[bytes, List[Tuple[str, int]], int]:
    """
    Build a stream cycling through every packet type.

    Parameters
    ----------
    n_packets : int
        Total number of packets to emit.
    stream_events : bool
        ``True`` gives every ``STREAM`` packet a JSON *event* payload;
        ``False`` gives it a payload that is not an event.

    Returns
    -------
    (bytes, list, int)
        The serialized stream, the expected ``(queue_name, iuid)`` sequence in
        arrival order, and the number of ``STREAM`` packets in the stream.
    """
    parts: List[bytes] = []
    expected: List[Tuple[str, int]] = []
    n_stream = 0
    for i in range(n_packets):
        type_, payload = _CYCLE[i % len(_CYCLE)]
        iuid = i + 1
        if type_ == PACKET_TYPES.STREAM:
            payload = (event_payload(n_stream) if stream_events
                       else b'not an event payload')
            n_stream += 1
        parts.append(mk(type_, iuid, payload))
        if type_ in QUEUE_NAME_BY_TYPE:
            expected.append((QUEUE_NAME_BY_TYPE[type_], iuid))
    return b''.join(parts), expected, n_stream


def corrupted_stream(n_pairs: int = 6, corrupt_every: int = 3
                     ) -> Tuple[bytes, List[Tuple[str, int]]]:
    """
    Build ``DATA``/``ACK`` pairs where every ``corrupt_every``-th ``DATA``
    packet has a flipped CRC byte.

    Returns the stream and the ``(queue_name, iuid)`` sequence that must
    survive: the corrupted ``DATA`` packets are dropped, and *everything else*
    -- in particular the ``ACK`` immediately behind a corrupted packet -- must
    still be delivered.
    """
    parts: List[bytes] = []
    expected: List[Tuple[str, int]] = []
    for i in range(n_pairs):
        packet = bytearray(mk(PACKET_TYPES.DATA, i + 1, bytes(range(10))))
        if i % corrupt_every == 0:
            packet[-1] ^= 0xFF          # flip the low CRC byte
        else:
            expected.append(('data', i + 1))
        parts.append(bytes(packet))
        parts.append(mk(PACKET_TYPES.ACK, 100 + i))
        expected.append(('ack', 100 + i))
    return b''.join(parts), expected


def iter_parse(manager, data: bytes, chunk_size: int) -> None:
    """Feed :data:`data` to ``manager.parse`` in ``chunk_size``-byte chunks."""
    for chunk in chunks(data, chunk_size):
        manager.parse(chunk)


def connect_queue_signals(namespace, sink: List[Tuple[str, int]],
                          names: Iterable[str] = ('data', 'ack', 'nack',
                                                  'stream', 'id_response')
                          ) -> None:
    """Append ``(name, iuid)`` to :data:`sink` for every ``*-received`` signal."""
    for name in names:
        namespace.signal(f'{name}-received').connect(
            lambda packet, _name=name, **kwargs: sink.append(
                (_name, packet.iuid_)), weak=False)
