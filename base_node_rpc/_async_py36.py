from __future__ import absolute_import, unicode_literals, print_function
from concurrent.futures import TimeoutError
import logging
import platform
import threading

from logging_helpers import _L
from nadamq.NadaMq import cPacketParser, PACKET_TYPES, PACKET_NAME_BY_TYPE
import asyncio
import asyncserial
import blinker
import json_tricks
import numpy as np
import pandas as pd
import serial
import serial_device as sd

from ._async_common import ParseError, ID_REQUEST


__all__ = ['read_packet', '_request', '_read_device_id', '_available_devices',
           '_async_serial_keepalive', 'AsyncSerialMonitor',
           'BaseNodeSerialMonitor']


logger = logging.getLogger(__name__)


async def read_packet(serial_):
    '''
    Read a single packet from a serial device.

    .. note::
        Asynchronous co-routine.

    Parameters
    ----------
    serial_ : asyncserial.AsyncSerial
        Asynchronous serial connection

    Returns
    -------
    cPacket or None
        Packet parsed from data received on serial device.  ``None`` is
        returned if no response was received.


    .. versionchanged:: 0.48.4
        If a serial exception occurs, e.g., there was no response before timing
        out, return ``None``.
    '''
    parser = cPacketParser()
    result = None
    while result is None:
        try:
            character = await serial_.read(8 << 10)
        except Exception as exception:
            if 'handle is invalid' not in str(exception):
                logger.debug('error communicating with port `%s`: %s',
                             serial_.ser.port, exception)
            break
        if character:
            result = parser.parse(np.fromstring(character, dtype='uint8'))
        elif parser.error:
            # Error parsing packet.
            raise ParseError('Error parsing packet.')
    return result


async def _request(request, **kwargs):
    '''
    Request device identifier from a serial device.

    .. note::
        Asynchronous co-routine.

    Parameters
    ----------
    request : bytes
        Request to send.
    timeout : float, optional
        Number of seconds to wait for response from serial device.
    **kwargs
        Keyword arguments to pass to :class:`asyncserial.AsyncSerial`
        initialization function.

    Returns
    -------
    dict
        Specified :data:`kwargs` updated with ``device_name`` and
        ``device_version`` items.
    '''
    timeout = kwargs.pop('timeout', None)
    device = kwargs.pop('device', None)
    if device is None:
        async_device = asyncserial.AsyncSerial(**kwargs)
    else:
        async_device = device

    try:
        async_device.write(request)
        done, pending = await asyncio.wait([read_packet(async_device)],
                                           timeout=timeout)
        if not done:
            _L().debug('Timed out waiting for: %s', kwargs)
            return None
        return list(done)[0].result()
    finally:
        if device is None:
            async_device.close()


async def _read_device_id(**kwargs):
    '''
    Request device identifier from a serial device.

    .. note::
        Asynchronous co-routine.

    Parameters
    ----------
    timeout : float, optional
        Number of seconds to wait for response from serial device.
    **kwargs
        Keyword arguments to pass to :class:`asyncserial.AsyncSerial`
        initialization function.

    Returns
    -------
    dict
        Specified :data:`kwargs` updated with ``device_name`` and
        ``device_version`` items.


    .. versionchanged:: 0.48.4
        Return ``None`` if there was no response.
    '''
    response = await _request(ID_REQUEST, **kwargs)
    if response is not None:
        result = kwargs.copy()
        result['device_name'], result['device_version'] = \
            response.data().strip().decode('utf8').split('::')
        return result


@asyncio.coroutine
def _available_devices(ports=None, baudrate=9600, timeout=None):
    '''
    Request list of available serial devices, including device identifier (if
    available).

    .. note::
        Asynchronous co-routine.

    Parameters
    ----------
    ports : pd.DataFrame, optional
        Table of ports to query (in format returned by
        :func:`serial_device.comports`).

        **Default: all available ports**
    baudrate : int, optional
        Baud rate to use for device identifier request.

        **Default: 9600**
    timeout : float, optional
        Maximum number of seconds to wait for a response from each serial
        device.

    Returns
    -------
    pd.DataFrame
        Specified :data:`ports` table updated with ``baudrate``,
        ``device_name``, and ``device_version`` columns.


    .. versionchanged:: 0.47
        Make ports argument optional.
    '''
    if ports is None:
        ports = sd.comports(only_available=True)

    if not ports.shape[0]:
        # No ports
        return ports
    futures = [_read_device_id(port=name_i, baudrate=baudrate, timeout=timeout)
               for name_i in ports.index]
    done, pending = yield from asyncio.wait(futures)
    results = [task_i.result() for task_i in done
               if task_i.result() is not None]
    if results:
        df_results = pd.DataFrame(results).set_index('port')
        df_results = ports.join(df_results)
    else:
        df_results = ports
    return df_results


async def _async_serial_keepalive(parent, *args, **kwargs):
    '''
    Connect to serial port and automatically try to reconnect if disconnected.

    Parameters
    ----------
    parent : AsyncSerialMonitor
        Serial monitor parent with the following attributes:

         - connected_event : threading.Event()
            Set when serial connection is established.  Cleared when serial
            connection is lost.
        - device : AsyncSerial
            Set when serial connection is established.
        - disconnected_event : threading.Event()
            Set when serial connection is lost.  Cleared when serial
            connection is established.
        - stop_event : threading.Event()
            When set, coroutine serial connection is closed and coroutine
            exits.
    *args
        Passed to :class:`asyncserial.AsyncSerial.__init__`.
    **kwargs
        Passed to :class:`asyncserial.AsyncSerial.__init__`.
    '''
    port = None
    parent.connected_event.clear()
    while not parent.stop_event.wait(.01):
        try:
            with asyncserial.AsyncSerial(*args, **kwargs) as async_device:
                _L().info('connected to %s', async_device.ser.port)
                parent.disconnected_event.clear()
                parent.connected_event.set()
                parent.device = async_device
                port = async_device.ser.port
                while async_device.ser.is_open:
                    try:
                        async_device.ser.in_waiting
                    except serial.SerialException:
                        break
                    else:
                        await asyncio.sleep(.01)
            _L().info('disconnected from %s', port)
        except serial.SerialException as e:
            pass
        parent.disconnected_event.set()
    parent.connected_event.clear()
    parent.disconnected_event.set()
    _L().info('stopped monitoring %s', port)


class AsyncSerialMonitor(threading.Thread):
    '''
    Thread connects to serial port and automatically tries to
    reconnect if disconnected.

    Can be used as a context manager to automatically release
    the serial port on exit.

    For example:

    >>> with BaseNodeSerialMonitor(port='COM8') as monitor:
    >>>     # Wait for serial device to connect.
    >>>     monitor.connected_event.wait()
    >>>     print(asyncio.run_coroutine_threadsafe(monitor.device.write('hello, world'), monitor.loop).result())

    Otherwise, the :meth:`stop` method must *explicitly* be called
    to release the serial connection before it can be connected to
    by other code.  For example:

    >>> monitor = BaseNodeSerialMonitor(port='COM8')
    >>> # Wait for serial device to connect.
    >>> monitor.connected_event.wait()
    >>> print(asyncio.run_coroutine_threadsafe(monitor.device.write('hello, world'), monitor.loop).result())
    >>> monitor.stop()

    Attributes
    ----------
    loop : asyncio event loop
        Event loop serial monitor is running under.
    device : asyncserial.AsyncSerial
        Reference to *active* serial device reference.

        Note that this reference *MAY* change if serial connection
        is interrupted and reconnected.
    connected_event : threading.Event
        Set when serial connection is established.
    disconnected_event : threading.Event
        Set when serial connection is lost.
    '''
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.connected_event = threading.Event()
        self.disconnected_event = threading.Event()
        self.disconnected_event.set()
        self.stop_event = threading.Event()
        self.loop = None
        self.device = None
        super(AsyncSerialMonitor, self).__init__()
        self.daemon = True

    def run(self):
        if platform.system() == 'Windows':
            loop = asyncio.ProactorEventLoop()
            asyncio.set_event_loop(loop)
        else:
            loop = asyncio.new_event_loop()
        self.loop = loop
        self.listen()

    def listen(self):
        return self.loop\
            .run_until_complete(_async_serial_keepalive(self, *self.args,
                                                        **self.kwargs))

    def stop(self):
        self.stop_event.set()
        try:
            self.device.close()
        except Exception:
            pass
        self.disconnected_event.wait()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()


class BaseNodeSerialMonitor(AsyncSerialMonitor):
    def __init__(self, *args, **kwargs):
        super(BaseNodeSerialMonitor, self).__init__(*args, **kwargs)
        self._request_queue = None
        self.signals = blinker.Namespace()

    def listen(self):
        _L().info('listening')
        self._request_queue = asyncio.Queue()
        tasks = [asyncio.ensure_future(f)
                 for f in (self.read_packets(),
                           _async_serial_keepalive(self, *self.args,
                                                   **self.kwargs))]
        self.loop.run_until_complete(asyncio.wait(tasks))
        self.loop.close()

    def request(self, request, *args, **kwargs):
        '''
        Submit request to serial device and wait for response packet.

        See :meth:`arequest` for async coroutine variant of this method.

        Returns
        -------
        nadamq.NadaMq.cPacket
            Response packet.
        '''
        future = asyncio \
            .run_coroutine_threadsafe(self.arequest(request),
                                      loop=self.loop)
        while True:
            try:
                return future.result(*args, **kwargs)
            except TimeoutError:
                _L().debug('retry after timeout: %s, %s', args, kwargs)

    async def arequest(self, request, **kwargs):
        '''
        Request device identifier from a serial device.

        .. note::
            Asynchronous co-routine.

        Parameters
        ----------
        request : bytes
            Request to send.
        timeout : float, optional
            Number of seconds to wait for response from serial device.
        **kwargs
            Keyword arguments to pass to :class:`asyncserial.AsyncSerial`
            initialization function.

        Returns
        -------
        dict
            Specified :data:`kwargs` updated with ``device_name`` and
            ``device_version`` items.
        '''
        await self.device.write(request)
        return await self._request_queue.get()

    async def read_packets(self):
        _L().debug('start listening for packets')
        while not self.stop_event.is_set():
            _L().debug('waiting for packet')
            try:
                packet = await self.read_packet()
            except Exception:
                if self.stop_event.is_set():
                    break
                _L().debug('error reading packet', exc_info=True)
                await asyncio.sleep(.01)
                continue

            if packet.type_ == PACKET_TYPES.STREAM:
                try:
                    # XXX Use `json_tricks` rather than standard `json` to
                    # support serializing [Numpy arrays and scalars][1].
                    #
                    # [1]: http://json-tricks.readthedocs.io/en/latest/#numpy-arrays
                    message = json_tricks.loads(packet.data().decode('utf8'))
                    self.signals.signal(message['event']).send(message)
                    # Do not add event packets to a queue.  This prevents the
                    # `stream` queue from filling up with rapidly occurring
                    # events.
                    continue
                except Exception:
                    _L().debug('Stream packet contents do not describe an '
                               'event: %s', packet.data().decode('utf8'),
                               exc_info=True)

            if packet.type_ == PACKET_TYPES.DATA:
                await self._request_queue.put(packet)

            for packet_type_i in ('data', 'ack', 'stream', 'id_response'):
                if packet.type_ == getattr(PACKET_TYPES, packet_type_i.upper()):
                    self.signals.signal('%s-received' %
                                        packet_type_i).send(packet)
        _L().debug('stop listening for packets')

    async def read_packet(self):
        '''
        Read a single packet from a serial device.

        .. note::
            Asynchronous co-routine.

        Returns
        -------
        cPacket
            Packet parsed from data received on serial device.
        '''
        parser = cPacketParser()
        result = False
        while result is False:
            try:
                character = await self.device.read(8 << 10)
            except (AttributeError, serial.SerialException):
                await asyncio.sleep(.01)
                continue

            if character:
                result = parser.parse(np.fromstring(character, dtype='uint8'))
            elif parser.error:
                # Error parsing packet.
                raise ParseError('Error parsing packet.')
        _L().debug('packet received: %s', PACKET_NAME_BY_TYPE[result.type_])
        return result
