# coding: utf-8
import asyncio
import platform
import threading
from functools import wraps
from typing import Optional, Coroutine, Any

import pandas as pd
from logging_helpers import _L

from ._async_base import BaseNodeSerialMonitor, _available_devices, _read_device_id


def new_file_event_loop() -> asyncio.AbstractEventLoop:
    """
    Create a new event loop appropriate for the current platform.
    
    Returns
    -------
    asyncio.AbstractEventLoop
        ProactorEventLoop on Windows for better I/O performance,
        default event loop on other platforms.
    """
    if platform.system() == 'Windows':
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()


def ensure_event_loop() -> asyncio.AbstractEventLoop:
    """
    Ensure there is a valid event loop in the current thread.
    
    Returns
    -------
    asyncio.AbstractEventLoop
        The current event loop, or a newly created one if none exists.
    """
    try:
        # Try to get running loop first (modern approach)
        try:
            loop = asyncio.get_running_loop()
            return loop
        except RuntimeError:
            pass  # No running loop, continue to get_event_loop
        
        # Get current event loop (may be deprecated in future Python versions)
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = new_file_event_loop()
            asyncio.set_event_loop(loop)
    except RuntimeError as e:
        if 'There is no current event loop' in str(e):
            loop = new_file_event_loop()
            asyncio.set_event_loop(loop)
        else:
            raise
    return loop


def with_loop(func: callable) -> callable:
    """
    Decorator to run function within an asyncio event loop.

    Uses :class:`asyncio.ProactorEventLoop` on Windows by default for better
    I/O performance, but falls back to :class:`asyncio.SelectorEventLoop` if
    needed.

    If an event loop is already bound to the thread, but is either a)
    currently running, or b) *not the preferred loop type for the platform*,
    execute function in a new thread running a new event loop instance.
    """

    @wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        loop = ensure_event_loop()

        thread_required = False
        if loop.is_running():
            _L().debug('Event loop is already running.')
            thread_required = True
        elif platform.system() == 'Windows':
            # Only require ProactorEventLoop if we're doing I/O operations
            if not isinstance(loop, asyncio.ProactorEventLoop):
                _L().debug('Using ProactorEventLoop for better I/O performance')
                thread_required = True

        if thread_required:
            _L().debug('Execute new loop in background thread.')
            finished = threading.Event()
            finished.result = None
            finished.error = None

            def _run(generator):
                # Create and set a new event loop for this thread
                new_loop = new_file_event_loop()
                asyncio.set_event_loop(new_loop)
                
                try:
                    # Use create_task instead of ensure_future (more modern)
                    result = new_loop.run_until_complete(generator)
                except Exception as e:
                    finished.result = None
                    finished.error = e
                else:
                    finished.result = result
                    finished.error = None
                finally:
                    try:
                        # Cancel all running tasks
                        pending = asyncio.all_tasks(new_loop)
                        for task in pending:
                            task.cancel()
                        # Run the loop until all tasks are cancelled
                        if pending:
                            new_loop.run_until_complete(
                                asyncio.gather(*pending, 
                                               return_exceptions=True))
                        # Stop the loop before closing it
                        new_loop.stop()
                        new_loop.close()
                        _L().debug('Closed event loop')
                    except Exception as e:
                        _L().debug(f'Error closing event loop: {e}')
                    finally:
                        finished.set()

            thread = threading.Thread(
                target=_run,
                args=(func(*args, **kwargs),),
                daemon=True,
                name="AsyncSerialWorker"
            )
            thread.start()
            finished.wait()
            if finished.error is not None:
                raise finished.error
            return finished.result

        _L().debug('Execute in existing event loop in main thread')
        return loop.run_until_complete(func(*args, **kwargs))

    return wrapped


@with_loop
def available_devices(
    baudrate: Optional[int] = 9600,
    ports: Optional[pd.DataFrame] = None,
    timeout: Optional[float] = None,
    **kwargs
) -> Coroutine:
    """
    Request list of available serial devices, including device identifier.

    Parameters
    ----------
    baudrate : int, optional
        Baud rate to use for device identifier request.
        Default: 9600
    ports : pd.DataFrame, optional
        Table of ports to query.
        Default: all available ports
    timeout : float, optional
        Maximum seconds to wait for response from each serial device.
    **kwargs
        Keyword arguments to pass to `_available_devices()` function.

    Returns
    -------
    pd.DataFrame
        Specified ports table updated with baudrate, device_name, and
        device_version columns.
    """
    return _available_devices(ports=ports, baudrate=baudrate,
                              timeout=timeout, **kwargs)


@with_loop
def read_device_id(**kwargs) -> Coroutine:
    """
    Request device identifier from a serial device.

    Parameters
    ----------
    timeout : float, optional
        Number of seconds to wait for response from a serial device.
        If not specified, will wait indefinitely.
    **kwargs
        Keyword arguments to pass to :class:`asyncserial.AsyncSerial`
        initialization function.

    Returns
    -------
    dict
        Specified kwargs updated with device_name and device_version items.
    
    Raises
    ------
    asyncio.TimeoutError
        If timeout is specified and device doesn't respond in time.
    """
    timeout = kwargs.pop('timeout', None)
    coro = _read_device_id(**kwargs)
    
    # Only use wait_for if timeout is specified
    if timeout is not None:
        return asyncio.wait_for(coro, timeout=timeout)
    return coro
