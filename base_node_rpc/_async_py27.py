from __future__ import absolute_import

from logging_helpers import _L
from nadamq.NadaMq import cPacketParser, PACKET_TYPES
import asyncserial
import numpy as np
import pandas as pd
import serial_device as sd
import trollius as asyncio

from ._async_common import ParseError, ID_REQUEST


@asyncio.coroutine
def read_packet(serial_):
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
    result = False
    while result is False:
        try:
            character = yield asyncio.From(serial_.read(8 << 10))
        except Exception as exception:
            if 'handle is invalid' not in str(exception):
                _L().debug('error communicating with port `%s`',
                           serial_.ser.port, exc_info=True)
            break
        result = parser.parse(np.fromstring(character, dtype='uint8'))
        if parser.error:
            # Error parsing packet.
            raise ParseError('Error parsing packet.')
    raise asyncio.Return(result)


@asyncio.coroutine
def _read_device_id(**kwargs):
    '''
    Request device identifier from a serial device.

    .. note::
        Asynchronous co-routine.

    Parameters
    ----------
    **kwargs
        Keyword arguments to pass to :class:`asyncserial.AsyncSerial`
        initialization function.

    Returns
    -------
    dict
        Specified :data:`kwargs` updated with ``device_name`` and
        ``device_version`` items.


    .. versionchanged:: X.X.X
        Remove `timeout` argument in favour of using `asyncio` timeout
        features.  Discard any incoming packets that are not of type
        ``ID_RESPONSE``.
    '''
    result = kwargs.copy()
    with asyncserial.AsyncSerial(**kwargs) as async_device:
        async_device.write(ID_REQUEST)
        while True:
            packet = yield asyncio.From(asyncio
                                        .From(read_packet(async_device)))
            if not hasattr(packet, 'type_'):
                # Error reading packet from serial device.
                raise RuntimeError('Error reading packet from serial device.')
            elif packet.type_ == PACKET_TYPES.ID_RESPONSE:
                break
        result['device_name'], result['device_version'] = \
            packet.data().split('::')
        raise asyncio.Return(result)


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


    .. versionchanged:: 0.48.4
        Make ports argument optional.
    '''
    if ports is None:
        ports = sd.comports(only_available=True)

    if not ports.shape[0]:
        # No ports
        raise asyncio.Return(ports)
    futures = [_read_device_id(port=name_i, baudrate=baudrate)
               for name_i in ports.index]
    done, pending = yield asyncio.From(asyncio.wait(futures, timeout=timeout))
    results = [task_i.result() for task_i in done
               if task_i.result() is not None]
    if results:
        df_results = pd.DataFrame(results).set_index('port')
        df_results = ports.join(df_results)
    else:
        df_results = ports
    raise asyncio.Return(df_results)
