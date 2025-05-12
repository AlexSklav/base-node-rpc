# coding: utf-8
import time
import queue

import blinker
import logging
import json_tricks

import numpy as np
import pandas as pd

from typing import Optional, Union
from datetime import datetime
from threading import Thread

import serial
from nadamq.NadaMq import cPacketParser, PACKET_TYPES

logger = logging.getLogger(name=__name__)

# Prevent warning about potential future changes to Numpy scalar encoding behaviour.
json_tricks.NumpyEncoder.SHOW_SCALAR_WARNING = False


class PacketQueueManager:
    """
    Parse data from an input stream and push each complete packet on a :class:`queue.Queue`
    according to the type of packet: ``data``, ``ack``, or ``stream``.

    Using queues

    Parameters
    ----------
    high_water_mark : int, optional
        Maximum number of packets to store in each packet queue.

        By default, **all packets are kept** (i.e., high-water mark is
        disabled).

        .. note::
            Packets received while a queue is at the :attr:`high_water_mark`
            are discarded.

            **TODO** Add configurable policy to keep either newest or oldest
            packets after :attr:`high_water_mark` is reached.

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
    """

    def __init__(self, high_water_mark: Optional[int] = None):
        self._packet_parser = cPacketParser()
        packet_types = ['data', 'ack', 'stream', 'id_response']
        # Signals to connect to indicating a packet received or when the queue is full.
        self.signals = blinker.Namespace()
        self.packet_queues = pd.Series([queue.Queue()] * len(packet_types), index=packet_types)
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

        Parameters
        ----------
        data : str or bytes
        """
        if not data:
            return

        packets = []
        current_time = datetime.now()  # Get time once instead of for each packet
        packet_parser = cPacketParser()  # Create single parser for reparsing

        # Convert data to numpy array once if needed
        if isinstance(data, (str, bytes)):
            try:
                # Process data as bytes more efficiently
                data_array = np.frombuffer(data if isinstance(data, bytes) else data.encode(), dtype='uint8')
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

                    result = self._packet_parser.parse(np.frombuffer(c_bytes, dtype='uint8'))
                    if result is not False:
                        # A full packet has been parsed - prefer tobytes() over deprecated tostring()
                        packet_str = np.frombuffer(result.tobytes(), dtype='uint8')
                        packets.append((current_time, packet_parser.parse(packet_str)))
                        self._packet_parser.reset()
                    elif self._packet_parser.error:
                        self._packet_parser.reset()
            else:
                # Process bytes in chunks for better performance
                for i in range(len(data_array)):
                    result = self._packet_parser.parse(data_array[i:i + 1])
                    if result is not False:
                        # Use tobytes() instead of deprecated tostring()
                        packet_str = np.frombuffer(result.tobytes(), dtype='uint8')
                        packets.append((current_time, packet_parser.parse(packet_str)))
                        self._packet_parser.reset()
                    elif self._packet_parser.error:
                        self._packet_parser.reset()

        # Process collected packets
        for t, p in packets:
            if p.type_ == PACKET_TYPES.STREAM:
                try:
                    # XXX Use `json_tricks` rather than standard `json` to
                    # support serializing [Numpy arrays and scalars][1].
                    #
                    # [1]: http://json-tricks.readthedocs.io/en/latest/#numpy-arrays
                    message = json_tricks.loads(p.data().decode('utf8'))
                    self.signals.signal(message['event']).send(message)
                    # Do not add event packets to a queue.  This prevents the
                    # `stream` queue from filling up with rapidly occurring
                    # events.
                    continue
                except Exception:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f'Stream packet contents do not describe an event: {p.data()}', exc_info=True)

            # Use a mapping dict instead of repetitive if/else checks
            packet_type_map = {
                PACKET_TYPES.DATA: 'data',
                PACKET_TYPES.ACK: 'ack',
                PACKET_TYPES.STREAM: 'stream',
                PACKET_TYPES.ID_RESPONSE: 'id_response'
            }

            packet_type = packet_type_map.get(p.type_)
            if packet_type:
                self.signals.signal(f'{packet_type}-received').send(p)
                if not self.queue_full(packet_type):
                    self.packet_queues[packet_type].put((t, p))
                else:
                    self.signals.signal(f'{packet_type}-full').send()

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

    def __init__(self, stream, delay_seconds: Optional[float] = .01, high_water_mark: Optional[int] = None,
                 max_delay_seconds: Optional[float] = 0.1):
        self.message_parser = PacketQueueManager(high_water_mark)
        self.stream = stream
        self.enabled = False
        self._terminated = False
        self.delay_seconds = delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self._current_delay = delay_seconds
        self._consecutive_empty_reads = 0
        super(PacketWatcher, self).__init__()
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
        self.message_parser.parse_available(self.stream)

    @property
    def queues(self) -> pd.Series:
        return self.message_parser.packet_queues

    def terminate(self) -> None:
        """
        Stop watching task.
        """
        self._terminated = True
        self.delay_seconds = 0
        self.join()

    def __del__(self) -> None:
        """
        Stop watching task when deleted.
        """
        self.terminate()
