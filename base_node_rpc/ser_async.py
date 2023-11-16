# coding: utf-8
import asyncio
import platform
import threading

import pandas as pd

from functools import wraps
from typing import Optional, Coroutine

from logging_helpers import _L

from ._async_base import BaseNodeSerialMonitor, _available_devices, _read_device_id


def new_file_event_loop() -> asyncio.AbstractEventLoop:
    return asyncio.new_event_loop()


def ensure_event_loop() -> asyncio.AbstractEventLoop:
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError as e:
        if 'There is no current event loop' in str(e):
            loop = new_file_event_loop()
            asyncio.set_event_loop(loop)
        else:
            raise
    return loop


def with_loop(func: callable):
    """
    Decorator to run function within an asyncio event loop.

    .. notes::
        Uses :class:`asyncio.ProactorEventLoop` on Windows to support file I/O
        events, e.g., serial device events.

        If an event loop is already bound to the thread, but is either a)
        currently running, or b) *not a :class:`asyncio.ProactorEventLoop`
        instance*, execute function in a new thread running a new
        :class:`asyncio.ProactorEventLoop` instance.
    """

    @wraps(func)
    def wrapped(*args, **kwargs):
        loop = ensure_event_loop()

        thread_required = False
        if loop.is_running():
            _L().debug('Event loop is already running.')
            thread_required = True
        elif platform.system() == 'Windows':
            if not isinstance(loop, asyncio.ProactorEventLoop):
                _L().debug(f'`ProactorEventLoop` required, not `{type(loop)}` loop in background thread.')
            thread_required = True

        if thread_required:
            _L().debug('Execute new loop in background thread.')
            finished = threading.Event()

            def _run(generator):
                loop = ensure_event_loop()
                try:
                    result = loop.run_until_complete(asyncio
                                                     .ensure_future(generator))
                except Exception as e:
                    finished.result = None
                    finished.error = e
                else:
                    finished.result = result
                    finished.error = None
                finally:
                    loop.close()
                    _L().debug('closed event loop')
                finished.set()

            thread = threading.Thread(target=_run,
                                      args=(func(*args, **kwargs),))
            thread.daemon = True
            thread.start()
            finished.wait()
            if finished.error is not None:
                raise finished.error
            return finished.result

        _L().debug('Execute in exiting event loop in main thread')
        return loop.run_until_complete(func(**kwargs))

    return wrapped


@with_loop
def available_devices(baudrate: Optional[int] = 9600, ports: Optional[pd.DataFrame] = None,
                      timeout: Optional[float] = None, **kwargs) -> Coroutine:
    """
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
    **kwargs
        Keyword arguments to pass to `_available_devices()` function.

    Returns
    -------
    pd.DataFrame
        Specified :data:`ports` table updated with ``baudrate``,
        ``device_name``, and ``device_version`` columns.

    Version log
    -----------
    .. versionchanged:: 0.47
        Make ports argument optional.
    .. versionchanged:: 0.51.2
        Pass extra keyword arguments to `_available_devices()` function.
    """
    return _available_devices(ports=ports, baudrate=baudrate, timeout=timeout, **kwargs)


@with_loop
def read_device_id(**kwargs):
    """
    Request device identifier from a serial device.

    .. note::
        Synchronous wrapper for :func:`_read_device_id`.

    Parameters
    ----------
    timeout : float, optional
        Number of seconds to wait for response from a serial device.
    **kwargs
        Keyword arguments to pass to :class:`asyncserial.AsyncSerial`
        initialization function.

    Returns
    -------
    dict
        Specified :data:`kwargs` updated with ``device_name`` and
        ``device_version`` items.
    """
    timeout = kwargs.pop('timeout', None)
    return asyncio.wait_for(_read_device_id(**kwargs), timeout=timeout)
