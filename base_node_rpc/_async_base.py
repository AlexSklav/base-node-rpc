# coding: utf-8
import serial
import blinker
import asyncio
import logging
import threading
import asyncserial
import json_tricks

import numpy as np
import pandas as pd
import functools as ft
import serial_device as sd

from typing import Union, Optional

from logging_helpers import _L
from nadamq.NadaMq import cPacket, cPacketParser, PACKET_TYPES, PACKET_NAME_BY_TYPE

__all__ = ['read_packet', '_read_device_id', '_available_devices',
           '_async_serial_keepalive', 'AsyncSerialMonitor',
           'BaseNodeSerialMonitor']

logger = logging.getLogger(__name__)

ID_REQUEST = cPacket(type_=PACKET_TYPES.ID_REQUEST).tobytes()


class ParseError(Exception):
    pass


async def read_packet(serial_: serial.Serial) -> Union[bool, cPacket]:
    """
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
        Packet parsed from data received on a serial device.
        ``None`` is returned if no response was received.

    Version log
    -----------
    .. versionchanged:: 0.48.4
        If a serial exception occurs, e.g., there was no response before timing out, return ``None``.
    .. versionchanged:: 0.52
        Improved error handling for better resilience.
    """
    parser = cPacketParser()
    result = False
    
    try:
        while result is False:
            try:
                character = await serial_.read(8 << 10)
                if not character:  # No data received
                    return None
            except (serial.SerialException, OSError, AttributeError) as exception:
                port = getattr(serial_, 'port', '??')
                if 'handle is invalid' not in str(exception):
                    _L().debug(f'Error communicating with port `{port}`: {exception}')
                return None
            except Exception as exception:
                _L().warning(f'Unexpected error reading from serial port: {exception}')
                return None
                
            result = parser.parse(np.frombuffer(character, dtype='uint8').copy())
            if parser.error:
                # Error parsing packet.
                parser.reset()
                _L().debug('Error parsing packet, resetting parser')
                result = False
    except Exception as e:
        _L().error(f'Fatal error in read_packet: {e}', exc_info=True)
        return None
        
    return result


async def _read_device_id(**kwargs) -> dict:
    """
    Request device identifier from a serial device.

    .. note::
        Asynchronous co-routine.

    Parameters
    ----------
    settling_time_s : float, optional
        Time to wait before writing device ID request to serial port.
    **kwargs
        Keyword arguments to pass to :class:`asyncserial.AsyncSerial`
        initialization function.

    Returns
    -------
    dict
        Specified :data:`kwargs` updated with ``device_name`` and
        ``device_version`` items.

    Version log
    -----------
    .. versionchanged:: 0.51.1
        Remove `timeout` argument in favour of using `asyncio` timeout
        features.  Discard any incoming packets that are not of type
        ``ID_RESPONSE``.
    .. versionchanged:: 0.51.2
        Add ``settling_time_s`` keyword argument.
    """
    settling_time_s = kwargs.pop('settling_time_s', 0)
    result = kwargs.copy()

    with asyncserial.AsyncSerial(**kwargs) as async_device:
        await asyncio.sleep(settling_time_s)
        await async_device.write(ID_REQUEST)
        while True:
            packet = await read_packet(async_device)
            if not hasattr(packet, 'type_'):
                # Error reading packet from a serial device.
                raise RuntimeError('Error reading packet from serial device.')
            elif packet.type_ == PACKET_TYPES.ID_RESPONSE:
                break
        name, version = packet.data().split(b'::')
        try:
            result['device_name'] = name.decode('utf-8')
        except UnicodeDecodeError:
            result['device_name'] = name
        try:
            result['device_version'] = version.decode('utf-8')
        except UnicodeDecodeError:
            result['device_version'] = version

    return result


async def _available_devices(ports: str = None, baudrate: Optional[int] = 9600,
                             timeout: Optional[float] = None, settling_time_s: Optional[float] = 0.) -> pd.DataFrame:
    """
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
    settling_time_s : float, optional
        Time to wait before writing device ID request to serial port.

    Returns
    -------
    pd.DataFrame
        Specified :data:`ports` table updated with ``baudrate``,
        ``device_name``, and ``device_version`` columns.

    Version log
    -----------
    .. versionchanged:: 0.48.4
        Make ports argument optional.
    .. versionchanged:: 0.51.2
        Add ``settling_time_s`` keyword argument.
    """
    if ports is None:
        ports = sd.comports(only_available=True)

    if not ports.shape[0]:
        # No ports
        return ports

    # Create tasks for each port with individual timeouts
    tasks = []
    for name_i in ports.index:
        task = asyncio.ensure_future(_read_device_id(port=name_i, baudrate=baudrate, settling_time_s=settling_time_s))
        if timeout is not None:
            task = asyncio.wait_for(task, timeout=timeout)
        tasks.append(task)

    # Run all tasks
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.TimeoutError:
        # If timeout occurs, cancel all pending tasks
        for task in tasks:
            if not task.done():
                task.cancel()
        results = [task.result() for task in tasks if task.done() and not task.cancelled()]

    # Filter out exceptions and None results
    results = [result for result in results if isinstance(result, dict)]

    if results:
        df_results = pd.DataFrame(results).set_index('port')
        df_results = ports.join(df_results)
    else:
        df_results = ports

    return df_results


async def _async_serial_keepalive(parent: 'AsyncSerialMonitor', *args, **kwargs) -> None:
    """
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
    """
    port = None
    parent.connected_event.clear()
    while not parent.stop_event.wait(.01):
        try:
            with asyncserial.AsyncSerial(*args, **kwargs) as async_device:
                _L().info(f'Connected to {async_device.port}')
                parent.disconnected_event.clear()
                parent.connected_event.set()
                parent.device = async_device
                port = async_device.port
                while async_device.is_open:
                    try:
                        assert async_device.in_waiting >= 0
                    except (serial.SerialException, OSError):
                        break
                    else:
                        await asyncio.sleep(.01)
            _L().info(f'Disconnected from {port}')
        except serial.SerialException as e:
            _L().debug(f"Serial exception while connecting to port: {e}")
        except Exception as e:
            _L().error(f"Unexpected error during serial connection: {e}", exc_info=True)
        parent.disconnected_event.set()
    parent.connected_event.clear()
    parent.disconnected_event.set()
    _L().info(f'Stopped monitoring {port}')


class AsyncSerialMonitor(threading.Thread):
    """
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

    Version log
    -----------
    .. versionchanged:: 0.50
        Add `serial_signals` signal namespace and emit ``connected`` and
        ``disconnected`` signals when corresponding threading events are set.
    """

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.connected_event = threading.Event()
        self.disconnected_event = threading.Event()
        self.disconnected_event.set()
        self.stop_event = threading.Event()
        self.loop = None
        self.device = None

        self.serial_signals = blinker.Namespace()

        def wrapper(f, signal_name):
            f()
            self.serial_signals.signal(signal_name, {'event': signal_name})

        def connected_wrapper(f):
            f()
            self.serial_signals.signal('connected', {'event': 'connected', 'device': self.device})

        self.connected_event.set = ft.partial(connected_wrapper, self.connected_event.set)
        self.disconnected_event.set = ft.partial(wrapper, self.disconnected_event.set, 'disconnected')

        super().__init__()
        self.daemon = True

    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.listen()

    def listen(self) -> asyncio.Future.result:
        return self.loop.run_until_complete(_async_serial_keepalive(self, *self.args, **self.kwargs))

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self.device.close()
        except Exception:
            pass
        self.disconnected_event.wait()

    def __enter__(self) -> 'AsyncSerialMonitor':
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()


class BaseNodeSerialMonitor(AsyncSerialMonitor):
    def __init__(self, *args, **kwargs):
        super(BaseNodeSerialMonitor, self).__init__(*args, **kwargs)
        self._request_queue = None
        self.signals = blinker.Namespace()

    def listen(self):
        _L().info('listening')
        self.loop = asyncio.new_event_loop()  # Create a new event loop
        asyncio.set_event_loop(self.loop)  # Set the event loop for this thread

        self._request_queue = asyncio.Queue()
        tasks = []
        
        try:
            # Create the tasks
            read_task = asyncio.ensure_future(self.read_packets(), loop=self.loop)
            keepalive_task = asyncio.ensure_future(_async_serial_keepalive(self, *self.args, **self.kwargs), loop=self.loop)
            tasks = [read_task, keepalive_task]
            
            # Run until complete or cancelled
            self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        except asyncio.CancelledError:
            _L().debug("Tasks were cancelled")
        except Exception as e:
            _L().error(f"Error in BaseNodeSerialMonitor.listen: {e}", exc_info=True)
        finally:
            # Ensure all tasks are properly cancelled
            for task in tasks:
                if not task.done():
                    task.cancel()
                    
            # Wait a moment for cancellation to complete
            if tasks:
                try:
                    self.loop.run_until_complete(asyncio.wait(tasks, timeout=1.0))
                except Exception:
                    pass
                    
            # Close the loop
            self.loop.close()

    def request(self, request, *args, **kwargs):
        """
        Submit request to serial device and wait for response packet.

        See :meth:`arequest` for async coroutine variant of this method.

        Returns
        -------
        nadamq.NadaMq.cPacket
            Response packet.
            
        Raises
        ------
        TimeoutError
            If the device does not respond after max_retries.
        """
        max_retries = kwargs.pop('max_retries', 3)
        retry_count = 0
        future = asyncio.run_coroutine_threadsafe(self.arequest(request), loop=self.loop)
        
        while retry_count < max_retries:
            try:
                return future.result(*args, **kwargs)
            except TimeoutError:
                retry_count += 1
                if retry_count >= max_retries:
                    _L().warning(f'Max retries ({max_retries}) reached waiting for response')
                    raise
                _L().debug(f'Retry {retry_count}/{max_retries} after timeout: {args}')
                # Create a new future for retry
                future = asyncio.run_coroutine_threadsafe(self.arequest(request), loop=self.loop)

    async def arequest(self, request, **kwargs):
        """
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
        """
        await self.device.write(request)
        return await self._request_queue.get()

    async def read_packets(self):
        L = _L()
        L.debug('start listening for packets')

        async def on_packet_received(packet_):

            L.debug(f'packet received: {PACKET_NAME_BY_TYPE[packet_.type_]}')
            L.debug('parsed packet: `%s`',
                    np.frombuffer(packet_.data(), dtype='uint8'))

            if packet_.type_ == PACKET_TYPES.STREAM:
                try:
                    # XXX Use `json_tricks` rather than standard `json` to
                    # support serializing [Numpy arrays and scalars][1].
                    #
                    # [1]: http://json-tricks.readthedocs.io/en/latest/#numpy-arrays
                    message = json_tricks.loads(packet_.data().decode('utf8'))
                    self.signals.signal(message['event']).send(message)
                    # Do not add event packets to a queue.  This prevents the
                    # `stream` queue from filling up with rapidly occurring
                    # events.
                except Exception:
                    L.debug(f"Stream packet contents do not describe an event: "
                            f"{packet_.data().decode('utf8')}", exc_info=True)
            elif packet_.type_ == PACKET_TYPES.DATA:
                await self._request_queue.put(packet_)

            for packet_type_i in ('data', 'ack', 'stream', 'id_response'):
                if packet_.type_ == getattr(PACKET_TYPES, packet_type_i.upper()):
                    self.signals.signal(f'{packet_type_i}-received').send(packet_)

        parser = cPacketParser()
        while not self.stop_event.is_set():
            L.debug('waiting for packet')
            try:
                result = False
                while result is False:
                    try:
                        data = await self.device.read(8 << 10)
                    except (AttributeError, serial.SerialException):
                        await asyncio.sleep(.01)
                        continue

                    if not data:
                        continue

                    L.debug(f'read: `{data}`')
                    buffer_ = np.frombuffer(data, dtype='uint8').copy()
                    for i in range(len(buffer_)):
                        result = parser.parse(buffer_[i:i + 1])
                        if result is not False:
                            packet_str = np.frombuffer(result.tobytes(), dtype='uint8').copy()
                            packet = cPacketParser().parse(packet_str)
                            await on_packet_received(packet)
                            parser.reset()
                        elif parser.error:
                            parser.reset()
            except Exception:
                if self.stop_event.is_set():
                    break
                L.debug('error reading packet', exc_info=True)
                await asyncio.sleep(.01)
                continue

        L.debug('stop listening for packets')
