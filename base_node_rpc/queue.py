# coding: utf-8
import time
import queue

import blinker
import logging
import json_tricks

from typing import Dict, Optional, Union
from datetime import datetime
from threading import Thread

import serial
from nadamq.NadaMq import cPacket, cPacketParser, PacketState, PACKET_TYPES

logger = logging.getLogger(name=__name__)

# Prevent warning about potential future changes to Numpy scalar encoding behaviour.
json_tricks.NumpyEncoder.SHOW_SCALAR_WARNING = False

# Packet types that consist of a header *only*, i.e., no length, payload or
# CRC fields (see NadaMQ packet protocol).
_HEADER_ONLY_PACKET_TYPES = (PACKET_TYPES.ACK, PACKET_TYPES.NACK)

# Name of the queue each packet type is pushed on to.  Module level, since the
# mapping is constant (it was previously rebuilt for every received packet).
_QUEUE_NAME_BY_PACKET_TYPE = {
    PACKET_TYPES.DATA: 'data',
    PACKET_TYPES.ACK: 'ack',
    PACKET_TYPES.STREAM: 'stream',
    PACKET_TYPES.ID_RESPONSE: 'id_response',
}


def _header_only_packet(parser: cPacketParser) -> Optional[cPacket]:
    """
    Complete a header-only (i.e., ``ACK``/``NACK``) packet.

    :meth:`nadamq.NadaMq.cPacketParser.parse` can only complete a header-only
    packet through a special case that requires the whole 6 byte packet to be
    passed in a *single* call.  Data is fed to the parser one byte at a time
    (to avoid discarding data trailing a completed packet), so a header-only
    packet would otherwise a) never be completed, **and** b) corrupt the packet
    that follows it, since the parser would consume the start flag of the next
    packet as the payload length of the ``ACK``.

    Since ``ACK``/``NACK`` packets are *always* header-only, the packet is
    complete as soon as the parser has read the type field, i.e., as soon as
    the parser has moved to the ``LENGTH`` state without having consumed any
    length bytes yet.

    Parameters
    ----------
    parser : cPacketParser
        Parser that has just consumed a byte *without* completing a packet.

    Returns
    -------
    cPacket or None
        Corresponding packet if a header-only packet has just been completed
        (in which case the parser is reset), otherwise ``None``.
    """
    if (parser.state == PacketState.LENGTH and not parser.buffer and
            parser.packet.type_ in _HEADER_ONLY_PACKET_TYPES):
        packet = cPacket(type_=parser.packet.type_, iuid=parser.packet.iuid_)
        parser.reset()
        return packet
    return None


class PacketQueueManager:
    """
    Parse data from an input stream and push each complete packet on a :class:`queue.Queue`
    according to the type of packet: ``data``, ``ack``, or ``stream``.

    Using queues

    Parameters
    ----------
    high_water_mark : int, optional
        Maximum number of packets to store in each packet queue.

        Default: 10.

        .. note::
            When a queue is at the :attr:`high_water_mark`, the **oldest**
            packet is discarded to make room for each newly received packet
            (i.e., ring buffer semantics).

    Version log
    -----------
    .. versionchanged:: 0.30
        Add queue for :attr:`nadamq.NadaMq.PACKET_TYPES.ID_RESPONSE` packets.

        See :module:`nadamq` release notes for version 0.13.

    .. versionchanged:: 0.41
        Add :attr:`signals` namespace to register handlers for **packet
        received**, **queue full**, or **event** (i.e., a JSON encoded message
        containing an ``"event"`` key received in a
        :attr:`nadamq.NadaMq.PACKET_TYPES.STREAM` packet) signals.

        Callbacks can be connected to signals, e.g.:

        .. highlight:: python

            my_manager.signals.signal('data-received').connect(foo)
            my_manager.signals.signal('data-full').connect(bar)
            my_manager.signals.signal('stream-received').connect(foobar)
            my_manager.signals.signal(<event>).connect(barfoo)

    .. versionchanged:: 0.41.1
        Do not add event packets to a queue.  This prevents the ``stream``
        queue from filling up with rapidly occurring events.

    .. versionchanged:: 0.53
        Enable :attr:`high_water_mark` by default (10 packets) and discard the
        **oldest** packet(s) rather than the newly received packet once a queue
        is full.  Only the ``data`` queue has a consumer, so the remaining
        queues would otherwise either grow without bound or stay full forever
        (dropping every new packet).
    """

    def __init__(self, high_water_mark: Optional[int] = 10):
        self._packet_parser = cPacketParser()
        packet_types = ['data', 'ack', 'stream', 'id_response']
        # Signals to connect to indicating a packet received or when the queue is full.
        self.signals = blinker.Namespace()
        # Note: a *separate* queue is required for each packet type (a single
        # queue instance must **not** be shared between packet types).
        # N.B. a plain `dict` (rather than a `pd.Series`) -- this is looked up
        # on the per-packet hot path, where `dict` lookup is orders of
        # magnitude cheaper.  Every access site indexes by queue name
        # (`queues['data']`, `queues['stream']`, ...), which is unchanged.
        self.packet_queues = {name: queue.Queue() for name in packet_types}
        self.high_water_mark = high_water_mark

    def parse_available(self, stream) -> None:
        """
        Read and parse available data from :data:`stream`.

        For each complete packet contained in the parsed data (or a packet
        started on previous that is completed), push the packet on a queue
        according to the type of packet: ``data``, ``ack``, or ``stream``.

        Parameters
        ----------
        stream
            Object that **MUST** have a ``read`` method that returns a
            ``str-like`` value.
        """
        data = stream.read()
        self.parse(data)

    def parse(self, data: Union[str, bytes]):
        """
        Parse data.

        For each complete packet contained in the parsed data (or a packet
        started on previous read that is completed), push the packet on a queue
        according to the type of packet: ``data``, ``ack``, or ``stream``.

        Version log
        -----------
        .. versionchanged:: 0.30
            Add handling for :attr:`nadamq.NadaMq.PACKET_TYPES.ID_RESPONSE`
            packets.

        .. versionchanged:: 0.41
            Send signal when packet is received, queue is full, or whenever a
            JSON encoded message containing an ``"event"`` key received in a
            :attr:`nadamq.NadaMq.PACKET_TYPES.STREAM` packet.  See
            :attr:`signals`.

        .. versionchanged:: 0.41.1
            Do not add event packets to a queue.  This prevents the ``stream``
            queue from filling up with rapidly occurring events.

        .. versionchanged:: 0.52
            Improved performance by processing data in chunks and reducing object creation.

        .. versionchanged:: 0.54.1
            Feed the parser 1-byte ``bytes`` slices rather than 1-element
            ``numpy`` slices, and push the packet returned by
            :meth:`nadamq.NadaMq.cPacketParser.parse` directly instead of
            serializing it and parsing it a second time.  Both are pure
            overhead: the round trip re-ran the CRC over every payload and
            allocated a second parser *per packet*, and the ``numpy`` slice
            allocated an array *per byte*.  Parsed packets are unchanged.

        Parameters
        ----------
        data : str or bytes
        """
        if not data:
            return

        packets = []
        current_time = datetime.now()  # Get time once instead of for each packet

        if isinstance(data, (str, bytes)):
            try:
                # Process data as bytes (`str` is encoded as UTF-8 first).
                data_bytes = data if isinstance(data, bytes) else data.encode()
            except (TypeError, AttributeError):
                # Handle individual characters if needed
                for c in data:
                    # Handle different input types appropriately
                    if isinstance(c, int):
                        # Integer value
                        c_bytes = c.to_bytes(1, byteorder='little')
                    elif isinstance(c, bytes):
                        # Already bytes
                        c_bytes = c
                    elif isinstance(c, str):
                        # Single character string
                        c_bytes = c.encode('utf-8')
                    else:
                        # Try bytes conversion as last resort
                        try:
                            c_bytes = bytes([c])
                        except (TypeError, ValueError):
                            logger.warning(f"Skipping unparseable input of type {type(c)}")
                            continue

                    result = self._packet_parser.parse(c_bytes)
                    if result is not False:
                        # A full packet has been parsed.
                        packets.append((current_time, result))
                        self._packet_parser.reset()
                    elif self._packet_parser.error:
                        self._packet_parser.reset()
                    else:
                        # Header-only packets (i.e., ``ACK``/``NACK``) are not
                        # completed by the parser when fed one byte at a time.
                        header_only_packet = _header_only_packet(self._packet_parser)
                        if header_only_packet is not None:
                            packets.append((current_time, header_only_packet))
            else:
                # Feed the parser one byte at a time.  N.B. a whole chunk
                # **must not** be passed in a single `parse()` call: that call
                # returns as soon as a packet completes, discarding the rest of
                # the chunk.
                parse = self._packet_parser.parse
                for i in range(len(data_bytes)):
                    result = parse(data_bytes[i:i + 1])
                    if result is not False:
                        packets.append((current_time, result))
                        self._packet_parser.reset()
                    elif self._packet_parser.error:
                        self._packet_parser.reset()
                    else:
                        # Header-only packets (i.e., ``ACK``/``NACK``) are not
                        # completed by the parser when fed one byte at a time.
                        header_only_packet = _header_only_packet(self._packet_parser)
                        if header_only_packet is not None:
                            packets.append((current_time, header_only_packet))

        # Process collected packets
        for t, p in packets:
            if p.type_ == PACKET_TYPES.STREAM:
                try:
                    # XXX Use `json_tricks` rather than standard `json` to
                    # support serializing [Numpy arrays and scalars][1].
                    #
                    # [1]: http://json-tricks.readthedocs.io/en/latest/#numpy-arrays
                    message = json_tricks.loads(p.data().decode('utf8'))
                    event = message['event']
                except Exception:
                    event = None
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f'Stream packet contents do not describe an event: {p.data()}', exc_info=True)
                if event is not None:
                    # Dispatch the signal *outside* of the `try` above so that
                    # an exception raised by a subscriber is neither swallowed
                    # nor misreported as a malformed event packet.
                    try:
                        self.signals.signal(event).send(message)
                    except Exception:
                        logger.warning(f'Error handling `{event}` event signal', exc_info=True)
                    # Do not add event packets to a queue.  This prevents the
                    # `stream` queue from filling up with rapidly occurring
                    # events.
                    continue

            # Use a mapping dict instead of repetitive if/else checks
            packet_type = _QUEUE_NAME_BY_PACKET_TYPE.get(p.type_)
            if packet_type:
                self.signals.signal(f'{packet_type}-received').send(p)
                packet_queue = self.packet_queues[packet_type]
                if self.queue_full(packet_type):
                    # Queue is at the high-water mark.  Evict the *oldest*
                    # packet(s) to make room for the new one (i.e., ring buffer
                    # semantics), since a stale packet is less useful than a
                    # freshly received one.  N.B. the `<type>-full` signal is
                    # still sent so subscribers can detect a queue that is not
                    # being drained.
                    self.signals.signal(f'{packet_type}-full').send()
                    while self.queue_full(packet_type):
                        try:
                            packet_queue.get_nowait()
                        except queue.Empty:
                            break
                packet_queue.put((t, p))

    def queue_full(self, name: str) -> bool:
        """
        Parameters
        ----------
        name : str
            Name of queue.

        Returns
        -------
        bool
            ``True`` if :attr:`high_water_mark` has been reached for the specified packet queue.
        """
        return ((self.high_water_mark is not None) and
                (self.packet_queues[name].qsize() >= self.high_water_mark))


class SerialStream:
    """
    Wrapper around :class:`serial.Serial` device to provide a parameterless
    :meth:`read` method.

    Parameters
    ----------
    serial_device : serial.Serial
        Serial device to wrap.
    """

    def __init__(self, serial_device: serial.Serial):
        self.serial_device = serial_device

    def read(self) -> bytes:
        """
        Returns
        -------
        str or bytes
            Available data from serial receiving buffer.

        .. versionchanged:: 0.52
            Improved error handling and performance
        """
        try:
            in_waiting = self.serial_device.in_waiting  # Preferred over inWaiting which is deprecated
            if in_waiting > 0:
                return self.serial_device.read(in_waiting)
            return b''
        except (OSError, serial.SerialException) as e:
            logger.debug(f"Error reading from serial device: {e}")
            return b''

    def write(self, msg: Union[str, bytes]) -> None:
        """
        Parameters
        ----------
        msg : str or bytes
            Data to write to serial transmission buffer.
        """
        self.serial_device.write(msg)

    def close(self) -> None:
        """
        Close serial stream.
        """
        self.serial_device.close()


class FakeStream:
    """
    Stream interface which returns a list of message strings, one message at a
    time, from the :meth:`read` method.

    Useful, for example, for testing the :class:`PacketWatcher` class without a
    serial connection.
    """

    def __init__(self, messages):
        self.messages = messages

    def read(self):
        if self.messages:
            return self.messages.pop(0)
        else:
            return ''


class PacketWatcher(Thread):
    """
    Thread task to watch for new packets on a stream.

    Parameters
    ----------
    stream : SerialStream
        Object that **MUST** have a ``read`` method that returns a ``str-like`` value.
    delay_seconds : float, optional
        Number of seconds to wait between polls of stream.
    high_water_mark : int, optional
        Maximum number of packets to store in each packet queue.

        .. see::
            :class:`PacketQueueManager`

    .. versionchanged:: 0.52
        Improved error handling and performance with adaptive polling
    """

    def __init__(self, stream, delay_seconds: Optional[float] = .01, high_water_mark: Optional[int] = 10,
                 max_delay_seconds: Optional[float] = 0.1):
        self.message_parser = PacketQueueManager(high_water_mark)
        self.stream = stream
        self.enabled = False
        self._terminated = False
        self.delay_seconds = delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self._current_delay = delay_seconds
        self._consecutive_empty_reads = 0
        super().__init__()
        self.daemon = True

    def run(self) -> None:
        """
        Start watching stream.

        Uses adaptive polling - increases delay when no data is received
        to reduce CPU usage, and decreases delay when data is flowing.
        """
        while not self._terminated:
            try:
                if self.enabled:
                    data_received = self.parse_available()

                    # Adaptive polling - adjust delay based on activity
                    if data_received:
                        self._consecutive_empty_reads = 0
                        self._current_delay = self.delay_seconds
                    else:
                        self._consecutive_empty_reads += 1
                        # Gradually increase delay up to max_delay_seconds
                        if self._consecutive_empty_reads > 5:
                            self._current_delay = min(self._current_delay * 1.5, self.max_delay_seconds)

                time.sleep(self._current_delay)
            except Exception as e:
                logger.error(f"Error in PacketWatcher: {e}")
                time.sleep(self.delay_seconds)

    def parse_available(self) -> bool:
        """
        Parse available data from stream.

        Returns
        -------
        bool
            Whether any data was read from the stream
        """
        try:
            data = self.stream.read()
            if data:
                self.message_parser.parse(data)
                return True
            return False
        except Exception as e:
            logger.debug(f"Error reading from stream: {e}")
            return False

    @property
    def queues(self) -> Dict[str, queue.Queue]:
        return self.message_parser.packet_queues

    def terminate(self) -> None:
        """
        Stop watching task.
        """
        self._terminated = True
        self.delay_seconds = 0
        # Only join if the thread was actually started, since joining a thread
        # that was never started raises a `RuntimeError`.
        if self.is_alive():
            self.join()

    def __del__(self) -> None:
        """
        Stop watching task when deleted.
        """
        self.terminate()
