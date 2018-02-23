from __future__ import absolute_import
from concurrent.futures import TimeoutError
import logging
import platform

from functools import wraps
from nadamq.NadaMq import cPacket, cPacketParser, PACKET_TYPES
import asyncio
import asyncserial
import numpy as np
import pandas as pd
import serial_device as sd
import threading


logger = logging.getLogger(__name__)

ID_REQUEST = cPacket(type_=PACKET_TYPES.ID_REQUEST).tostring()


class ParseError(Exception):
    pass


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
    cPacket
        Packet parsed from data received on serial device.
    '''
    parser = cPacketParser()
    result = False
    while result is False:
        character = await serial_.read(8 << 10)
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
            logger.debug('Timed out waiting for: %s', kwargs)
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
    '''
    response = await _request(ID_REQUEST, **kwargs)
    result = kwargs.copy()
    result['device_name'], result['device_version'] = \
        response.data().strip().decode('utf8').split('::')
    return result


@asyncio.coroutine
def _available_devices(ports, baudrate=9600, timeout=None):
    '''
    Request list of available serial devices, including device identifier (if
    available).

    .. note::
        Asynchronous co-routine.

    Parameters
    ----------
    ports : pd.DataFrame
        Table of ports to query (in format returned by
        :func:`serial_device.comports`).
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
    '''
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


def with_loop(func):
    '''
    Decorator to run function within an asyncio event loop.

    .. notes::
        Uses :class:`asyncio.ProactorEventLoop` on Windows to support, e.g.,
        serial device events.
    '''
    @wraps(func)
    def wrapped(*args, **kwargs):
        loop = kwargs.pop('loop', None)
        if loop is None:
            if platform.system() == 'Windows':
                loop = asyncio.ProactorEventLoop()
                asyncio.set_event_loop(loop)
            else:
                loop = asyncio.get_event_loop()
        return loop.run_until_complete(func(*args, **kwargs))
    return wrapped


@with_loop
def available_devices(baudrate=9600, ports=None, timeout=None):
    '''
    Request list of available serial devices, including device identifier (if
    available).

    .. note::
        Synchronous wrapper for :func:`_available_devices`.

    Parameters
    ----------
    baudrate : int, optional
        Baud rate to use for device identifier request.

        **Default: 9600**
    ports : pd.DataFrame
        Table of ports to query (in format returned by
        :func:`serial_device.comports`).

        **Default: all available ports**
    timeout : float, optional
        Maximum number of seconds to wait for a response from each serial
        device.

    Returns
    -------
    pd.DataFrame
        Specified :data:`ports` table updated with ``baudrate``,
        ``device_name``, and ``device_version`` columns.
    '''
    if ports is None:
        ports = sd.comports(only_available=True)

    return _available_devices(ports, baudrate=baudrate, timeout=timeout)


@with_loop
def read_device_id(**kwargs):
    '''
    Request device identifier from a serial device.

    .. note::
        Synchronous wrapper for :func:`_read_device_id`.

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
    '''
    return _read_device_id(**kwargs)


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
                logging.info('connected to %s', async_device.ser.port)
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
            logging.info('disconnected from %s', port)
        except serial.SerialException as e:
            pass
        parent.disconnected_event.set()
    parent.connected_event.clear()
    parent.disconnected_event.set()
    logging.info('stopped monitoring %s', port)


@with_loop
def async_serial_monitor(parent, *args, **kwargs):
    return _async_serial_keepalive(parent, *args, **kwargs)


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
        self.kwargs['loop'] = loop
        async_serial_monitor(self, *self.args, **self.kwargs)

    def stop(self):
        self.stop_event.set()
        try:
            self.device.close()
        except Exception:
            pass
        self.disconnected_event.set()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()


class BaseNodeSerialMonitor(AsyncSerialMonitor):
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
                logging.debug('retry after timeout: %s, %s', args, kwargs)

    async def arequest(self, request):
        '''
        Submit request to serial device.

        Returns
        -------
        awaitable
            Awaitable
        '''
        return await _request(request, device=self.device)
