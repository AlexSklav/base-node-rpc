# coding: utf-8
import time
import queue

import blinker
import logging
import functools
import json_tricks

from typing import Callable, Dict, Optional, Union
from datetime import datetime
from threading import Thread

import serial
from nadamq.NadaMq import cPacket, cPacketParser, PacketState, PACKET_TYPES

logger = logging.getLogger(name=__name__)

# Prevent warning about potential future changes to Numpy scalar encoding behaviour.
json_tricks.NumpyEncoder.SHOW_SCALAR_WARNING = False


def _memoize_by_argument(func: Callable, maxsize: int = 256) -> Callable:
    """
    Memoize a **pure**, single-argument function of a callable.

    Falls back to the uncached implementation for an unhashable argument, so
    that behaviour (including any exception raised by :data:`func`) is
    unchanged in every case.
    """
    cached = functools.lru_cache(maxsize=maxsize)(func)

    @functools.wraps(func)
    def wrapper(arg):
        try:
            hash(arg)
        except TypeError:
            # Unhashable argument cannot be used as an `lru_cache` key.
            return func(arg)
        return cached(arg)

    wrapper.cache_info = cached.cache_info
    wrapper.cache_clear = cached.cache_clear
    # Marker used to make installation idempotent (see
    # :func:`_install_json_tricks_hook_cache`).
    wrapper._memoized_func = func
    return wrapper


#: Whether the ``json_tricks`` decoder-hook signature cache has been installed.
_JSON_TRICKS_HOOK_CACHE_INSTALLED = False


def _install_json_tricks_hook_cache() -> bool:
    """
    Cache ``json_tricks`` decoder/encoder hook argument-name discovery.

    ``json_tricks.decoders.TricksPairHook.__init__()`` -- which is constructed
    **once per** :func:`json_tricks.loads` call -- wraps *every* object-pairs
    hook in :func:`json_tricks.utils.filtered_wrapper`, which in turn calls
    :func:`json_tricks.utils.get_arg_names`, which runs
    :func:`inspect.signature` on the hook.  With the 11 default hooks, that is
    11 signature inspections **per decoded packet**, which dominates decoding
    of the small JSON payloads carried by ``STREAM`` event packets (~70 us per
    event, versus ~1 us for :func:`json.loads`).

    Both functions are pure functions of the hook they are passed, so their
    results are memoized here.  ``json_tricks.decoders`` and
    ``json_tricks.encoders`` bind ``filtered_wrapper`` by name at *import*
    time, so each module reference must be replaced separately.

    Decoded output is unaffected: the same wrapper (closing over the same hook
    and the same set of argument names) is returned, rather than an equivalent
    one rebuilt from scratch.

    Returns
    -------
    bool
        ``True`` if the cache is installed.

    .. versionadded:: 0.52.12
    """
    global _JSON_TRICKS_HOOK_CACHE_INSTALLED

    if _JSON_TRICKS_HOOK_CACHE_INSTALLED:
        return True

    try:
        from json_tricks import decoders as _jt_decoders
        from json_tricks import encoders as _jt_encoders
        from json_tricks import utils as _jt_utils

        get_arg_names = getattr(_jt_utils, 'get_arg_names', None)
        filtered_wrapper = getattr(_jt_utils, 'filtered_wrapper', None)
        if not callable(get_arg_names) or not callable(filtered_wrapper):
            # `json_tricks` internals do not match expectations; leave
            # untouched.
            logger.debug('`json_tricks` does not expose the expected '
                         '`get_arg_names`/`filtered_wrapper` helpers; '
                         'decoder hook cache not installed.')
            return False

        originals = {'utils.get_arg_names': (_jt_utils, 'get_arg_names',
                                             get_arg_names)}
        for module in (_jt_utils, _jt_decoders, _jt_encoders):
            if getattr(module, 'filtered_wrapper', None) is filtered_wrapper:
                originals[f'{module.__name__}.filtered_wrapper'] = (
                    module, 'filtered_wrapper', filtered_wrapper)

        if getattr(get_arg_names, '_memoized_func', None) is None:
            _jt_utils.get_arg_names = _memoize_by_argument(get_arg_names)
        cached_filtered_wrapper = _memoize_by_argument(filtered_wrapper)
        for module, name, _ in list(originals.values()):
            if name == 'filtered_wrapper':
                setattr(module, name, cached_filtered_wrapper)

        # Smoke test the patched decoder.  If anything is amiss, restore every
        # original attribute and leave `json_tricks` exactly as it was found.
        try:
            if json_tricks.loads('{"__test__": [1, 2.5, null, true]}') != {
                    '__test__': [1, 2.5, None, True]}:
                raise RuntimeError('unexpected decode result')
        except Exception:
            for module, name, original in originals.values():
                setattr(module, name, original)
            logger.debug('`json_tricks` decoder hook cache failed its smoke '
                         'test; reverted.', exc_info=True)
            return False
    except Exception:
        logger.debug('Could not install `json_tricks` decoder hook cache.',
                     exc_info=True)
        return False

    _JSON_TRICKS_HOOK_CACHE_INSTALLED = True
    logger.debug('Installed `json_tricks` decoder hook signature cache.')
    return True


# Install at import time, so that both `STREAM` event dispatch sites
# (:meth:`PacketQueueManager.parse` and
# :meth:`base_node_rpc._async_base.BaseNodeSerialMonitor.read_packets`) benefit.
_install_json_tricks_hook_cache()

# Packet types that consist of a header *only*, i.e., no length, payload or
# CRC fields (see NadaMQ packet protocol).
_HEADER_ONLY_PACKET_TYPES = (PACKET_TYPES.ACK, PACKET_TYPES.NACK)

# Name of the queue each packet type is pushed on to.  Module level, since the
# mapping is constant (it was previously rebuilt for every received packet).
_QUEUE_NAME_BY_PACKET_TYPE = {
    PACKET_TYPES.DATA: 'data',
    PACKET_TYPES.ACK: 'ack',
    PACKET_TYPES.NACK: 'nack',
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

    .. versionchanged:: 0.52.11
        Enable :attr:`high_water_mark` by default (10 packets) and discard the
        **oldest** packet(s) rather than the newly received packet once a queue
        is full.  Only the ``data`` queue has a consumer, so the remaining
        queues would otherwise either grow without bound or stay full forever
        (dropping every new packet).

    .. versionchanged:: 0.52.12
        Add a queue for :attr:`nadamq.NadaMq.PACKET_TYPES.NACK` packets (with
        the same ring-buffer semantics as the other queues) and send the
        corresponding ``nack-received``/``nack-full`` signals.  ``NACK``
        packets were previously parsed and then silently discarded, so a device
        rejecting a request was indistinguishable from a device not responding
        at all.
    """

    def __init__(self, high_water_mark: Optional[int] = 10):
        self._packet_parser = cPacketParser()
        # N.B. keep in sync with `_QUEUE_NAME_BY_PACKET_TYPE`, which maps a
        # received packet on to the queue it is pushed to.
        packet_types = ['data', 'ack', 'nack', 'stream', 'id_response']
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

        .. versionchanged:: 0.52.11
            Feed the parser 1-byte ``bytes`` slices rather than 1-element
            ``numpy`` slices, and push the packet returned by
            :meth:`nadamq.NadaMq.cPacketParser.parse` directly instead of
            serializing it and parsing it a second time.  Both are pure
            overhead: the round trip re-ran the CRC over every payload and
            allocated a second parser *per packet*, and the ``numpy`` slice
            allocated an array *per byte*.  Parsed packets are unchanged.

        .. versionchanged:: 0.52.12
            Feed the parser whole **chunks** and resume at
            :attr:`nadamq.NadaMq.cPacketParser.bytes_consumed`, rather than
            one byte at a time.  ``parse()`` returns as soon as a packet
            completes and now reports exactly how much of the chunk it
            consumed, so the remainder can simply be fed back in -- at a
            fraction of the per-byte call overhead.  Parsed packets, their
            order, and every signal are unchanged.

        Parameters
        ----------
        data : str or bytes
        """
        if not data:
            return

        packets = []
        current_time = datetime.now()  # Get time once instead of for each packet

        data_bytes = None
        if isinstance(data, (str, bytes)):
            try:
                # Process data as bytes (`str` is encoded as UTF-8 first).
                data_bytes = data if isinstance(data, bytes) else data.encode()
            except (TypeError, AttributeError):
                # Handle individual characters if needed.  Each element is
                # normalized to bytes and the result is concatenated, so that
                # the parser is fed a single chunk below (exactly as for the
                # `bytes`/`str` case).
                converted = []
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
                    converted.append(c_bytes)
                data_bytes = b''.join(converted)

        if data_bytes:
            # Feed the parser whole chunks, resuming at the byte after the one
            # that completed a packet.  N.B. `parse()` returns as soon as a
            # packet completes, so the *remainder* of the chunk must be fed
            # back in -- `bytes_consumed` says exactly where to resume.
            #
            # N.B. `reset()` **must not** be called here (neither after a
            # completed packet nor on `parser.error`): the state machine
            # already resets itself internally in both cases, and `error`
            # describes the whole call rather than its last byte -- so an
            # unconditional reset would discard a packet that is partially
            # parsed at the end of the chunk.
            parser = self._packet_parser
            parse = parser.parse
            debug = logger.isEnabledFor(logging.DEBUG)
            while data_bytes:
                result = parse(data_bytes)
                consumed = parser.bytes_consumed
                if not consumed:
                    # Defensive: `parse()` consumes at least one byte of any
                    # non-empty input, so this cannot normally happen.  Break
                    # rather than spin forever on a parser that stalls.
                    logger.warning('Packet parser consumed no input; '
                                   'discarding %d byte(s).', len(data_bytes))
                    break
                data_bytes = data_bytes[consumed:]
                if result is not False:
                    packets.append((current_time, result))
                elif parser.error and debug:
                    logger.debug('Parse error within chunk; parser recovered '
                                 'and continued with the remaining bytes.')

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
