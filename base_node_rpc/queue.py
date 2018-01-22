import logging
import time
from datetime import datetime
from Queue import Queue
from threading import Thread

import blinker
import json_tricks
import pandas as pd
import numpy as np
from nadamq.NadaMq import cPacketParser, PACKET_TYPES

logger = logging.getLogger(name=__name__)

# Prevent warning about potential future changes to Numpy scalar encoding
# behaviour.
json_tricks.NumpyEncoder.SHOW_SCALAR_WARNING = False


class PacketQueueManager(object):
    '''
    Parse data from an input stream and push each complete packet on a
    :class:`Queue.Queue` according to the type of packet: ``data``, ``ack``, or
    ``stream``.

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
    '''
    def __init__(self, high_water_mark=None):
        self._packet_parser = cPacketParser()
        packet_types = ['data', 'ack', 'stream', 'id_response']
        # Signals to connect to indicating packet received or queue is full.
        self.signals = blinker.Namespace()
        self.packet_queues = pd.Series([Queue() for t in packet_types],
                                       index=packet_types)
        self.high_water_mark = high_water_mark

    def parse_available(self, stream):
        '''
        Read and parse available data from :data:`stream`.

        For each complete packet contained in the parsed data (or a packet
        started on previous that is completed), push the packet on a queue
        according to the type of packet: ``data``, ``ack``, or ``stream``.

        Parameters
        ----------
        stream
            Object that **MUST** have a ``read`` method that returns a
            ``str-like`` value.
        '''
        data = stream.read()
        self.parse(data)

    def parse(self, data):
        '''
        Parse data.

        For each complete packet contained in the parsed data (or a packet
        started on previous read that is completed), push the packet on a queue
        according to the type of packet: ``data``, ``ack``, or ``stream``.

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

        Parameters
        ----------
        data : str or bytes
        '''
        packets = []

        for c in data:
            result = self._packet_parser.parse(np.fromstring(c, dtype='uint8'))
            if result is not False:
                # A full packet has been parsed.
                packet_str = np.fromstring(result.tostring(), dtype='uint8')
                # Add parsed packet to list of packets parsed during this
                # method call.
                packets.append((datetime.now(),
                                cPacketParser().parse(packet_str)))
                # Reset the state of the packet parser to prepare for next
                # packet.
                self._packet_parser.reset()
            elif self._packet_parser.error:
                # A parsing error occurred.
                # Reset the state of the packet parser to prepare for next
                # packet.
                self._packet_parser.reset()

        # Filter packets parsed during this method call and queue according to
        # packet type.
        for t, p in packets:
            if p.type_ == PACKET_TYPES.STREAM:
                try:
                    # XXX Use `json_tricks` rather than standard `json` to
                    # support serializing [Numpy arrays and scalars][1].
                    #
                    # [1]: http://json-tricks.readthedocs.io/en/latest/#numpy-arrays
                    message = json_tricks.loads(p.data())
                    self.signals.signal(message['event']).send(message)
                    # Do not add event packets to a queue.  This prevents the
                    # `stream` queue from filling up with rapidly occurring
                    # events.
                    continue
                except Exception:
                    logger.debug('Stream packet contents do not describe an '
                                 'event: %s', p.data())

            for packet_type_i in ('data', 'ack', 'stream', 'id_response'):
                if p.type_ == getattr(PACKET_TYPES, packet_type_i.upper()):
                    self.signals.signal('%s-received' % packet_type_i).send(p)
                    if self.queue_full(packet_type_i):
                        self.signals.signal('%s-full' % packet_type_i).send()
                    else:
                        self.packet_queues[packet_type_i].put((t, p))

    def queue_full(self, name):
        '''
        Parameters
        ----------
        name : str
            Name of queue.

        Returns
        -------
        bool
            ``True`` if :attr:`high_water_mark` is has been reached for the
            specified packet queue.
        '''
        return ((self.high_water_mark is not None) and
                (self.packet_queues[name].qsize() >= self.high_water_mark))


class SerialStream(object):
    '''
    Wrapper around :class:`serial.Serial` device to provide a parameterless
    :meth:`read` method.

    Parameters
    ----------
    serial_device : serial.Serial
        Serial device to wrap.
    '''
    def __init__(self, serial_device):
        self.serial_device = serial_device

    def read(self):
        '''
        Returns
        -------
        str or bytes
            Available data from serial receiving buffer.
        '''
        return self.serial_device.read(self.serial_device.inWaiting())

    def write(self, str):
        '''
        Parameters
        ----------
        str : str or bytes
            Data to write to serial transmission buffer.
        '''
        self.serial_device.write(str)

    def close(self):
        '''
        Close serial stream.
        '''
        self.serial_device.close()


class FakeStream(object):
    '''
    Stream interface which returns a list of message strings, one message at a
    time, from the :meth:`read` method.

    Useful, for example, for testing the :class:`PacketWatcher` class without a
    serial connection.
    '''
    def __init__(self, messages):
        self.messages = messages

    def read(self):
        if self.messages:
            return self.messages.pop(0)
        else:
            return ''


class PacketWatcher(Thread):
    '''
    Thread task to watch for new packets on a stream.

    Parameters
    ----------
    stream : SerialStream
        Object that **MUST** have a ``read`` method that returns a ``str-like``
        value.
    delay_seconds : float, optional
        Number of seconds to wait between polls of stream.
    high_water_mark : int, optional
        Maximum number of packets to store in each packet queue.

        .. see::
            :class:`PacketQueueManager`
    '''
    def __init__(self, stream, delay_seconds=.01, high_water_mark=None):
        self.message_parser = PacketQueueManager(high_water_mark)
        self.stream = stream
        self.enabled = False
        self._terminated = False
        self.delay_seconds = delay_seconds
        super(PacketWatcher, self).__init__()
        self.daemon = True

    def run(self):
        '''
        Start watching stream.
        '''
        while True:
            if self._terminated:
                break
            elif self.enabled:
                self.parse_available()
            time.sleep(self.delay_seconds)

    def parse_available(self):
        '''
        Parse available data from stream.
        '''
        self.message_parser.parse_available(self.stream)

    @property
    def queues(self):
        return self.message_parser.packet_queues

    def terminate(self):
        '''
        Stop watching task.
        '''
        self._terminated = True
        self.delay_seconds = 0
        self.join()

    def __del__(self):
        '''
        Stop watching task when deleted.
        '''
        self.terminate()
