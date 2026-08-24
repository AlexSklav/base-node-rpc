# coding: utf-8
import serial
import blinker
import asyncio
import logging
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

from .queue import _header_only_packet

__all__ = ['read_packet', '_read_device_id', '_available_devices',
           '_async_serial_keepalive', 'AsyncSerialMonitor',
           'BaseNodeSerialMonitor']

ID_REQUEST = cPacket(type_=PACKET_TYPES.ID_REQUEST).tobytes()


class ParseError(Exception):
    """Raised when there is an error parsing a packet."""
    pass


#: Attribute used to stash bytes that were read from a serial device but not
#: consumed by :func:`read_packet` (i.e., the remainder of a chunk that
#: contained more than one packet).
_PENDING_ATTR = '_nadamq_pending_bytes'


def _pending_bytes(serial_) -> bytes:
    """Bytes read from :data:`serial_` but not yet consumed by a packet."""
    return getattr(serial_, _PENDING_ATTR, b'') or b''


def _set_pending_bytes(serial_, data: bytes) -> None:
    """Stash unconsumed bytes on :data:`serial_` for the next read."""
    try:
        setattr(serial_, _PENDING_ATTR, data)
    except (AttributeError, TypeError):
        # Serial object does not accept arbitrary attributes; fall back to the
        # previous behaviour of discarding trailing bytes.
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
    .. versionchanged:: 0.55
        Feed the parser one byte at a time and retain any bytes trailing the
        completed packet for the next call, rather than passing the whole read
        chunk to a single ``parse()`` call (which discarded everything after
        the first completed packet).
    """
    parser = cPacketParser()
    # Start with any bytes left over from a previous call, i.e., the remainder
    # of a read chunk that contained more than one packet.
    pending = _pending_bytes(serial_)
    _set_pending_bytes(serial_, b'')

    try:
        while True:
            if not pending:
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
                pending = bytes(character)

            # Feed the parser one byte at a time.  N.B. the whole chunk **must
            # not** be passed to a single `parse()` call: that call returns as
            # soon as a packet completes, discarding the rest of the chunk --
            # so, e.g., a ``STREAM`` event arriving in the same read as the
            # ``ID_RESPONSE`` being waited for would destroy the
            # ``ID_RESPONSE``.
            for i in range(len(pending)):
                result = parser.parse(pending[i:i + 1])
                if result is not False:
                    # Retain the trailing bytes of the chunk so the packet(s)
                    # behind this one are not lost with the parser state.
                    _set_pending_bytes(serial_, pending[i + 1:])
                    return result
                if parser.error:
                    _L().debug('Error parsing packet, resetting parser')
                    parser.reset()
            pending = b''
    except Exception:
        _L().error('Fatal error in read_packet', exc_info=True)
        return None


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
            # N.B. also consider bytes that were already read from the device
            # but not consumed by the previous `read_packet()` call (i.e., the
            # remainder of a chunk that carried more than one packet);
            # `in_waiting` is 0 in that case even though a complete
            # ``ID_RESPONSE`` may still be waiting to be parsed.
            if not async_device.in_waiting and not _pending_bytes(async_device):
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
    timeout: Optional[float] = 5.,
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

        **Default: 5 seconds**

        .. note::
            A device that never responds (e.g., a modem) would otherwise block
            **forever** while holding the corresponding serial port open.
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
        coro = _read_device_id(port=name_i, baudrate=baudrate,
                               settling_time_s=settling_time_s)
        if timeout is not None:
            coro = asyncio.wait_for(coro, timeout=timeout)
        tasks.append(coro)

    # Run all tasks (return_exceptions=True so gather never raises)
    results = await asyncio.gather(*tasks, return_exceptions=True)

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
        # NOTE: `parent.stop_event` is a **blocking** `threading.Event`, so it
        # must not be waited on from within this coroutine.  Yield to the event
        # loop using `asyncio.sleep()` at the end of each iteration instead
        # (see below), otherwise this coroutine would starve the rest of the
        # event loop (e.g., packet reading, pending requests) whenever no
        # serial device is available to connect to.
        while not parent.stop_event.is_set():
            try:
                async with asyncserial.AsyncSerial(*args, warned=warned, **kwargs) as async_device:
                    _L().info(f'Connected to {async_device.port}')

                    parent.disconnected_event.clear()
                    # Assign the device *before* setting the connected event,
                    # since setting the event notifies waiting threads and
                    # sends the `connected` signal (including a reference to
                    # `parent.device`).
                    parent.device = async_device
                    parent.connected_event.set()
                    port = async_device.port

                    # Monitor connection while stop_event is not set
                    while async_device.is_open and not parent.stop_event.is_set():
                        try:
                            # N.B. reading `in_waiting` *is* the disconnect
                            # detector: it raises `SerialException`/`OSError`
                            # once the device has been unplugged.  It must not
                            # be written as an `assert`, since assertions are
                            # stripped under `python -O`, which would silently
                            # disable disconnect detection (and hence
                            # reconnection) entirely.
                            _ = async_device.in_waiting
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

            # Yield control back to the event loop before retrying.  This
            # **must** happen on every path through the loop -- in particular
            # when opening the serial port raised (synchronously) because no
            # device is connected, in which case nothing else above awaits.
            await asyncio.sleep(.01)

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

        # NOTE: only send a signal when the corresponding event is not
        # *already* set, i.e., on an actual connected/disconnected
        # **transition**.  Otherwise, e.g., a `disconnected` signal would be
        # sent on every iteration of the reconnect loop in
        # `_async_serial_keepalive()` (~100 times per second) while no serial
        # device is available.
        def wrapper(event, f, signal_name):
            transition = not event.is_set()
            f()
            if transition:
                self.serial_signals.signal(signal_name).send(
                    {'event': signal_name})

        def connected_wrapper(event, f):
            transition = not event.is_set()
            f()
            if transition:
                self.serial_signals.signal('connected').send(
                    {'event': 'connected', 'device': self.device})

        self.connected_event.set = ft.partial(connected_wrapper,
                                              self.connected_event,
                                              self.connected_event.set)
        self.disconnected_event.set = ft.partial(wrapper,
                                                 self.disconnected_event,
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

        # Try to close device gracefully.  The device is bound to the monitor
        # event loop (closing it cancels pending reads), so schedule the close
        # **on that loop** rather than performing it from the calling thread.
        device = self.device
        try:
            if device is not None:
                if (self.loop is not None and not self.loop.is_closed() and
                        self.loop.is_running()):
                    self.loop.call_soon_threadsafe(device.close)
                    _L().debug('Device close scheduled')
                else:
                    # N.B. a callback scheduled with
                    # `call_soon_threadsafe()` is *never* executed while the
                    # loop is not running, so close directly instead.
                    device.close()
                    _L().debug('Device closed')
        except Exception as e:
            _L().debug(f'Error closing device: {e}')

        # Wait for disconnected event with timeout
        if not self.disconnected_event.wait(timeout=2.0):
            _L().warning('Timeout waiting for disconnected event, forcing cleanup')
            
            # Force cleanup if timeout occurs
            if self.loop is not None and not self.loop.is_closed():
                try:
                    # Cancel all tasks in the loop.  Task cancellation is not
                    # thread-safe, so it must be performed *on* the loop while
                    # the loop is running.
                    def _cancel_all_tasks():
                        for task in asyncio.all_tasks(self.loop):
                            task.cancel()

                    if self.loop.is_running():
                        self.loop.call_soon_threadsafe(_cancel_all_tasks)
                    else:
                        # A callback scheduled on a loop that is not running
                        # would never execute, so cancel directly.
                        _cancel_all_tasks()
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
        super().__init__(*args, **kwargs)
        self._request_queue = None
        self._request_lock = None
        self.signals = blinker.Namespace()

    def listen(self) -> None:
        """Start listening for serial data and process packets."""
        _L().info('Starting BaseNodeSerialMonitor listener')

        # Reuse the event loop created by :meth:`run` (rather than leaking it
        # by replacing it with a new one, which would also leave a window
        # during which `run_coroutine_threadsafe()` could target a loop that is
        # never run).  A new loop is only created if there is no usable loop,
        # e.g., if :meth:`listen` is called directly, or called again after a
        # previous loop was closed below.
        if self.loop is None or self.loop.is_closed():
            self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        self._request_queue = asyncio.Queue()
        # Create the lock here (i.e., in the thread running the fresh event
        # loop) so that it is bound to the current event loop across
        # reconnects.
        self._request_lock = asyncio.Lock()
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
                        asyncio.gather(*tasks, return_exceptions=True))
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
        future = self._submit_request(request)

        while retry_count < max_retries:
            try:
                return future.result(*args, **kwargs)
            except TimeoutError:
                future.cancel()
                retry_count += 1
                if retry_count >= max_retries:
                    _L().warning(f'Max retries ({max_retries}) reached waiting for response')
                    raise
                _L().debug(f'Retry {retry_count}/{max_retries} after timeout: {args}')
                future = self._submit_request(request)

    def _submit_request(self, request: bytes):
        """
        Schedule :meth:`arequest` on the monitor loop from any thread.

        Returns a :class:`concurrent.futures.Future`.  If the loop is closed,
        not running, or was never created (e.g., after :meth:`stop`, or during
        interpreter shutdown when a ``__del__`` issues a final command), close
        the never-scheduled coroutine so it does not emit a ``coroutine ...
        was never awaited`` :class:`RuntimeWarning` during garbage collection,
        and raise a clear error instead.
        """
        coro = self.arequest(request)
        try:
            return asyncio.run_coroutine_threadsafe(coro, loop=self.loop)
        except (RuntimeError, AttributeError) as exception:
            coro.close()
            raise RuntimeError('Serial monitor event loop is not running; '
                               'cannot send request.') from exception

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
        # Serialize requests so that each request/response pair is matched up
        # one-to-one.  Using `async with` guarantees the lock is released even
        # if this coroutine is cancelled while waiting for a response (which is
        # exactly what the timeout/retry logic in :meth:`request` does).
        async with self._request_lock:
            # Discard any responses that are already queued before sending a
            # new request.  Anything sitting in the queue at this point is a
            # response nobody is waiting for -- typically the duplicate
            # response produced when :meth:`request` timed out and re-sent a
            # command that the device was still busy executing.  Draining
            # prevents the queue from becoming desynchronized by one packet,
            # which would otherwise cause each subsequent command to receive
            # the *previous* command's response.
            #
            # NOTE: this does not close the race completely.  A stale response
            # that arrives *between* the drain below and the real response can
            # still be mis-delivered (the duplicate command may still be
            # executing on the device).  Callers should therefore use timeouts
            # longer than the device's worst-case blocking time.
            while True:
                try:
                    stale_packet = self._request_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                _L().warning('Discarding stale response packet '
                             f'(type={PACKET_NAME_BY_TYPE[stale_packet.type_]},'
                             f' {len(stale_packet.data())} bytes) left over '
                             'from a timed-out request')

            await self.device.write(request)
            return await self._request_queue.get()

    async def read_packets(self) -> None:
        """Read and process packets from the serial device."""
        L = _L()
        L.debug('start listening for packets')

        async def on_packet_received(packet_: cPacket) -> None:
            # N.B. guarded: these are evaluated for *every* received packet,
            # and the `numpy` conversion below is far from free.
            if L.isEnabledFor(logging.DEBUG):
                L.debug(f'packet received: {PACKET_NAME_BY_TYPE[packet_.type_]}')
                L.debug(f'parsed packet: `{np.frombuffer(packet_.data(), dtype="uint8")}`')

            if packet_.type_ == PACKET_TYPES.STREAM:
                try:
                    # XXX Use `json_tricks` rather than standard `json` to
                    # support serializing [Numpy arrays and scalars][1].
                    #
                    # [1]: http://json-tricks.readthedocs.io/en/latest/#numpy-arrays
                    message = json_tricks.loads(packet_.data().decode('utf8'))
                    event = message['event']
                except Exception:
                    event = None
                    # N.B. log the *raw* payload bytes.  Re-running
                    # `.decode('utf8')` here would raise straight back out of
                    # this handler whenever the decode is what failed in the
                    # first place, abandoning the rest of the read chunk.
                    L.debug("Stream packet contents do not describe an event: "
                            f"{packet_.data()}", exc_info=True)
                if event is not None:
                    # Dispatch the signal *outside* of the `try` above so that
                    # an exception raised by a subscriber is neither swallowed
                    # nor misreported as a malformed event packet.
                    #
                    # Do not add event packets to a queue.  This prevents the
                    # `stream` queue from filling up with rapidly occurring
                    # events.
                    try:
                        self.signals.signal(event).send(message)
                    except Exception:
                        L.warning(f'Error handling `{event}` event signal',
                                  exc_info=True)
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
                    except (AttributeError, serial.SerialException,
                            IOError, OSError):
                        if self.stop_event.is_set():
                            break
                        # Device not yet connected (None) — wait for
                        # keepalive task to establish connection.
                        # Device connected but closed — hot-unplug.
                        if (self.device is not None and
                                not getattr(self.device, 'is_open', False)):
                            # Device was connected but is now closed (e.g.,
                            # hot-unplug latched by `asyncserial`).
                            #
                            # N.B. yield to the event loop *before* breaking
                            # out to the outer loop.  Breaking straight out
                            # re-enters the outer `while` -- which calls
                            # `read()` again immediately -- with **no** `await`
                            # anywhere along the path, since `read()` raises
                            # synchronously on a dead device.  That starves the
                            # event loop, so the keepalive task never gets to
                            # run and the device is never torn down or
                            # reconnected.
                            await asyncio.sleep(.01)
                            break
                        await asyncio.sleep(.01)
                        continue

                    if not data:
                        continue

                    # N.B. guarded: `data` can be up to 8 KiB, and formatting
                    # its `repr` for every read is not free.
                    if L.isEnabledFor(logging.DEBUG):
                        L.debug(f'read: `{data}`')
                    # Feed the parser one byte at a time.  N.B. the whole
                    # chunk **must not** be passed in a single `parse()` call:
                    # that call returns as soon as a packet completes,
                    # discarding the rest of the chunk.  1-byte `bytes` slices
                    # are used rather than `numpy` slices (which allocated an
                    # array per byte), and the packet returned by `parse()` is
                    # dispatched directly rather than being serialized and
                    # parsed a second time (which re-ran the CRC over every
                    # payload and allocated a second parser per packet).
                    parse = parser.parse
                    for i in range(len(data)):
                        result = parse(data[i:i + 1])
                        if result is not False:
                            await on_packet_received(result)
                            parser.reset()
                        elif parser.error:
                            parser.reset()
                        else:
                            # Header-only packets (i.e., ``ACK``/``NACK``) are
                            # not completed by the parser when fed one byte at
                            # a time.
                            header_only_packet = _header_only_packet(parser)
                            if header_only_packet is not None:
                                await on_packet_received(header_only_packet)
            except Exception:
                if self.stop_event.is_set():
                    L.debug('Stop event set during exception, exiting')
                    break
                L.debug('error reading packet', exc_info=True)
                await asyncio.sleep(.01)
                continue

        L.debug('stop listening for packets')
