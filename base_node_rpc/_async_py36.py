from __future__ import absolute_import
import logging
import platform

from functools import wraps
from nadamq.NadaMq import cPacket, cPacketParser, PACKET_TYPES
import asyncio
import asyncserial
import numpy as np
import pandas as pd
import serial_device as sd


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
