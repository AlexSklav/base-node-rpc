# coding: utf-8
import serial
import blinker
import asyncio
import threading
import asyncserial
import json_tricks

from typing import Optional, Dict, Any

import numpy as np
import pandas as pd
import functools as ft
import serial_device as sd

from logging_helpers import _L
from nadamq.NadaMq import (cPacket, cPacketParser, PACKET_TYPES,
                          PACKET_NAME_BY_TYPE)

__all__ = ['read_packet', '_read_device_id', '_available_devices',
           '_async_serial_keepalive', 'AsyncSerialMonitor',
           'BaseNodeSerialMonitor']

ID_REQUEST = cPacket(type_=PACKET_TYPES.ID_REQUEST).tobytes()


class ParseError(Exception):
    """Raised when there is an error parsing a packet."""
    pass


async def read_packet(serial_: serial.Serial) -> Optional[cPacket]:
    """
    Read a single packet from a serial device.

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
            except (serial.SerialException, OSError, AttributeError) as e:
                port = getattr(serial_, 'port', '??')
                if 'handle is invalid' not in str(e):
                    _L().debug(f'Error communicating with port `{port}`: {e}')
                return None
            except Exception as e:
                _L().warning(f'Unexpected error reading from serial port: {e}')
                return None
                
            result = parser.parse(np.frombuffer(character, dtype='uint8').copy())
            if parser.error:
                parser.reset()
                _L().debug('Error parsing packet, resetting parser')
                result = False
    except Exception:
        _L().error('Fatal error in read_packet', exc_info=True)
        return None
        
    return result


async def _read_device_id(**kwargs) -> Dict[str, Any]:
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

    Raises
    ------
    RuntimeError
        If device doesn't respond or returns invalid packet.
    IOError
        If serial connection fails.

    Version log
    -----------
    .. versionchanged:: 0.51.1
        Remove `timeout` argument in favour of using `asyncio` timeout
        features.  Discard any incoming packets that are not of type
        ``ID_RESPONSE``.
    .. versionchanged:: 0.51.2
        Add ``settling_time_s`` keyword argument.
    """
    settling_time_s = kwargs.pop('settling_time_s', 0.5)
    result = kwargs.copy()

    async with asyncserial.AsyncSerial(**kwargs) as async_device:
        # Wait for device to settle
        await asyncio.sleep(settling_time_s)
        
        # Send ID request
        await async_device.write(ID_REQUEST)
        
        while True:
            if not async_device.in_waiting:
                # Add small delay to ensure bytes show up
                await asyncio.sleep(0.01)
                continue
            
            # Read and parse packet
            packet = await read_packet(async_device)
            if not hasattr(packet, 'type_'):
                raise RuntimeError('Error reading packet from serial device.')
            elif packet.type_ == PACKET_TYPES.ID_RESPONSE:
                break
        name, version = packet.data().split(b'::')
        try:
            result['device_name'] = name.decode('utf-8')
        except UnicodeDecodeError:
            result['device_name'] = name
        
        # Decode version
        try:
            result['device_version'] = version.decode('utf-8')
        except UnicodeDecodeError:
            result['device_version'] = version

    return result


async def _available_devices(
    ports: Optional[pd.DataFrame] = None,
    baudrate: Optional[int] = 9600,
    timeout: Optional[float] = None,
    settling_time_s: Optional[float] = 0.,
    **kwargs
) -> pd.DataFrame:
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
    extra_args = {f'skip_{arg}' : kwargs.pop(f'skip_{arg}', None)
                  for arg in ['vid', 'pid', 'descriptor']}
    # FTDI devices reset when tried
    extra_args['skip_manufacturer'] = kwargs.pop('skip_manufacturer', ['ftdi'])
    if ports is None:
        ports = sd.comports(only_available=True,
                            **extra_args)

    if not ports.shape[0]:
        return ports

    # Create tasks for each port with individual timeouts
    tasks = []
    for name_i in ports.index:
        task = asyncio.ensure_future(
            _read_device_id(port=name_i, baudrate=baudrate, 
                            settling_time_s=settling_time_s))
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
        results = [task.result() for task in tasks 
                   if task.done() and not task.cancelled()]

    # Filter out exceptions and None results
    results = [result for result in results if isinstance(result, dict)]

    if results:
        df_results = pd.DataFrame(results).set_index('port')
        df_results = ports.join(df_results)
    else:
        ports['device_name'] = None
        df_results = ports

    return df_results


async def _async_serial_keepalive(
    parent: 'AsyncSerialMonitor',
    *args: Any,
    **kwargs: Any
) -> None:
    """
    Connect to serial port and automatically try to reconnect if disconnected.

    Parameters
    ----------
    parent : AsyncSerialMonitor
        Serial monitor parent with the following attributes:
        - connected_event : threading.Event()
            Set when serial connection is established.
        - device : AsyncSerial
            Set when serial connection is established.
        - disconnected_event : threading.Event()
            Set when serial connection is lost.
        - stop_event : threading.Event()
            When set, coroutine serial connection is closed and coroutine exits.
    *args
        Passed to :class:`asyncserial.AsyncSerial.__init__`.
    **kwargs
        Passed to :class:`asyncserial.AsyncSerial.__init__`.
    """
    port = None
    parent.connected_event.clear()
    warned = False
    try:
        while not parent.stop_event.wait(.01):
            try:
                async with asyncserial.AsyncSerial(*args, warned=warned, **kwargs) as async_device:
                    _L().info(f'Connected to {async_device.port}')

                    parent.disconnected_event.clear()
                    parent.connected_event.set()
                    parent.device = async_device
                    port = async_device.port
                    
                    # Monitor connection while stop_event is not set
                    while async_device.is_open and not parent.stop_event.is_set():
                        try:
                            assert async_device.in_waiting >= 0
                        except (serial.SerialException, OSError):
                            _L().debug(f'Serial connection lost for {port}')
                            break
                        else:
                            warned = False
                            await asyncio.sleep(.01)
                    
                    _L().info(f'Disconnected from {port}')
                    
            except serial.SerialException as e:
                _L().debug(f"Serial exception while connecting to port: {e}")
                warned = True
            except Exception:
                _L().error("Unexpected error during serial connection",
                           exc_info=True)
            finally:
                # Always set disconnected_event after exiting context manager
                parent.disconnected_event.set()
                parent.connected_event.clear()
            
            # If stop requested, break immediately
            if parent.stop_event.is_set():
                break
                
    finally:
        # Ensure events are in correct state when exiting
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

    def __init__(self, *args: Any, **kwargs: Any) -> None:
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
            self.serial_signals.signal('connected',
                                       {'event': 'connected',
                                        'device': self.device})

        self.connected_event.set = ft.partial(connected_wrapper,
                                              self.connected_event.set)
        self.disconnected_event.set = ft.partial(wrapper,
                                                 self.disconnected_event.set,
                                                 'disconnected')

        super().__init__()
        self.daemon = True

    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.listen()

    def listen(self) -> asyncio.Future.result:
        return self.loop.run_until_complete(
            _async_serial_keepalive(self, *self.args, **self.kwargs))

    def stop(self) -> None:
        """Stop the serial monitor and wait for cleanup."""
        _L().debug('Stopping serial monitor...')
        self.stop_event.set()
        
        # Try to close device gracefully
        try:
            if self.device is not None:
                self.device.close()
                _L().debug('Device closed')
        except Exception as e:
            _L().debug(f'Error closing device: {e}')

        # Wait for disconnected event with timeout
        if not self.disconnected_event.wait(timeout=2.0):
            _L().warning('Timeout waiting for disconnected event, forcing cleanup')
            
            # Force cleanup if timeout occurs
            if self.loop is not None and not self.loop.is_closed():
                try:
                    # Cancel all tasks in the loop
                    for task in asyncio.all_tasks(self.loop):
                        task.cancel()
                    _L().debug('Cancelled all tasks')
                except Exception as e:
                    _L().debug(f'Error during cleanup: {e}')
            
            # Set the disconnected event manually
            self.disconnected_event.set()

    def __enter__(self) -> 'AsyncSerialMonitor':
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()


class BaseNodeSerialMonitor(AsyncSerialMonitor):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(BaseNodeSerialMonitor, self).__init__(*args, **kwargs)
        self._request_queue = None
        self.signals = blinker.Namespace()

    def listen(self) -> None:
        """Start listening for serial data and process packets."""
        _L().info('Starting BaseNodeSerialMonitor listener')
        
        # Create new event loop for this thread
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        self._request_queue = asyncio.Queue()
        tasks = []
        
        try:
            # Create the tasks (without deprecated loop parameter)
            read_task = self.loop.create_task(self.read_packets())
            keepalive_task = self.loop.create_task(
                _async_serial_keepalive(self, *self.args, **self.kwargs))
            tasks = [read_task, keepalive_task]
            
            _L().debug(f'Created {len(tasks)} monitoring tasks')
            
            # Run until complete or cancelled
            self.loop.run_until_complete(
                asyncio.gather(*tasks, return_exceptions=True))
                
        except asyncio.CancelledError:
            _L().debug("Tasks were cancelled")
        except Exception:
            _L().error("Error in BaseNodeSerialMonitor.listen",
                       exc_info=True)
        finally:
            # Ensure all tasks are properly cancelled
            for task in tasks:
                if not task.done():
                    task.cancel()
                    
            # Wait a moment for cancellation to complete
            if tasks:
                try:
                    self.loop.run_until_complete(
                        asyncio.wait(tasks, timeout=1.0))
                except Exception as e:
                    _L().debug(f'Error waiting for tasks to complete: {e}')
                    
            # Close the loop
            try:
                self.loop.close()
                _L().debug('Event loop closed')
            except Exception as e:
                _L().debug(f'Error closing event loop: {e}')

    def request(self, request: bytes, *args: Any, **kwargs: Any) -> cPacket:
        """
        Submit request to serial device and wait for response packet.

        See :meth:`arequest` for asynchronous coroutine variant of this method.

        Parameters
        ----------
        request : bytes
            Request to send.
        *args
            Arguments to pass to future.result().
        **kwargs
            Keyword arguments to pass to future.result().

        Returns
        -------
        cPacket
            Response packet.
            
        Raises
        ------
        TimeoutError
            If the device does not respond after max_retries.
        """
        max_retries = kwargs.pop('max_retries', 3)
        retry_count = 0
        future = asyncio.run_coroutine_threadsafe(self.arequest(request),
                                                  loop=self.loop)
        
        while retry_count < max_retries:
            try:
                return future.result(*args, **kwargs)
            except TimeoutError:
                retry_count += 1
                if retry_count >= max_retries:
                    _L().warning(f'Max retries ({max_retries}) reached waiting for response')
                    raise
                _L().debug(f'Retry {retry_count}/{max_retries} after timeout: {args}')
                future = asyncio.run_coroutine_threadsafe(self.arequest(request),
                                                       loop=self.loop)

    async def arequest(self, request: bytes, **kwargs: Any) -> cPacket:
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
        cPacket
            Response packet.
        """
        await self.device.write(request)
        return await self._request_queue.get()

    async def read_packets(self) -> None:
        """Read and process packets from the serial device."""
        L = _L()
        L.debug('start listening for packets')

        async def on_packet_received(packet_: cPacket) -> None:
            L.debug(f'packet received: {PACKET_NAME_BY_TYPE[packet_.type_]}')
            L.debug(f'parsed packet: `{np.frombuffer(packet_.data(), dtype="uint8")}`')

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
                    L.debug("Stream packet contents do not describe an event: "
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
                            packet_str = np.frombuffer(result.tobytes(),
                                                       dtype='uint8').copy()
                            packet = cPacketParser().parse(packet_str)
                            await on_packet_received(packet)
                            parser.reset()
                        elif parser.error:
                            parser.reset()
            except Exception:
                if self.stop_event.is_set():
                    L.debug('Stop event set during exception, exiting')
                    break
                L.debug('error reading packet', exc_info=True)
                await asyncio.sleep(.01)
                continue

        L.debug('stop listening for packets')
