# coding: utf-8
import sys
import time
import queue
import blinker
import logging
import warnings
import threading
from typing import Optional, Any, Type

import serial
import serial.threaded
import pandas as pd
import serial_device as sd
import serial_device.threaded
import importlib.metadata as metadata

from arduino_rpc.protobuf import resolve_field_values, PYTYPE_MAP
from nadamq.NadaMq import cPacket, PACKET_TYPES
from or_event import OrEvent

from .queue import PacketQueueManager
from .ser_async import available_devices, read_device_id
from ._version import get_versions

__version__ = get_versions()['version']
del get_versions

logger = logging.getLogger(__name__)


class DeviceNotFound(Exception):
    """Raised when a device cannot be found."""
    pass


class MultipleDevicesFound(Exception):
    """Raised when multiple devices are found and only one was expected."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.df_comports = kwargs.pop('df_comports', None)
        super().__init__(*args, **kwargs)


class DeviceVersionMismatch(Exception):
    """Raised when device version doesn't match expected version."""
    def __init__(self, device: 'Proxy', device_version: str) -> None:
        self.device = device
        self.device_version = device_version
        super().__init__()

    def __str__(self) -> str:
        return (
            f'Device driver version ({self.device.device_version}) '
            f'does not match version reported by device '
            f'({self.device_version}).'
        )


class ProxyBase:
    """Base class for device proxies."""
    host_package_name: Optional[str] = None

    def __init__(self,
                 buffer_bounds_check: Optional[bool] = True,
                 high_water_mark: Optional[int] = 10,
                 timeout_s: Optional[int] = 10,
                 **kwargs: Any) -> None:
        """
        Initialize proxy base class.

        Parameters
        ----------
        buffer_bounds_check : bool, optional
            Whether to check buffer bounds. Default: True
        high_water_mark : int, optional
            High water mark for packet queue. Default: 10
        timeout_s : int, optional
            Timeout in seconds. Default: 10
        **kwargs
            Additional keyword arguments.
        """
        self._buffer_bounds_check = buffer_bounds_check
        self._buffer_size: Optional[int] = None
        self._packet_queue_manager = PacketQueueManager(
            high_water_mark=high_water_mark)
        self._timeout_s = timeout_s
        self._stream = None

    @property
    def host_software_version(self) -> str:
        # Get host software version from the module's __version__ attribute
        # (see PEP 396[1]).
        #
        # [1]: https://www.python.org/dev/peps/pep-0396/
        exec(f"from {self.__module__.split('.')[0]} import __version__ as host_version")
        return metadata.version(host_version)

    @property
    def remote_software_version(self) -> str:
        """Get remote software version."""
        from packaging.version import Version
        return Version(self.properties.software_version)

    @property
    def stream(self) -> Any:
        """Get stream."""
        return self._stream

    @stream.setter
    def stream(self, stream: Any) -> None:
        """Set stream."""
        self._stream = stream
        self.reset()

    @property
    def high_water_mark(self) -> int:
        """Get high water mark."""
        return self._packet_queue_manager.high_water_mark

    @high_water_mark.setter
    def high_water_mark(self, message_count: int) -> None:
        """Set high water mark."""
        self._packet_queue_manager.high_water_mark = message_count

    def help(self) -> None:
        """Open project webpage in new browser tab."""
        import webbrowser
        url = self.properties.url
        if url:
            webbrowser.open_new_tab(url)

    @property
    def properties(self) -> pd.Series:
        """Get device properties."""
        properties = {
            k: getattr(self, k)().tostring().decode('utf-8')
            for k in ['base_node_software_version', 'package_name',
                     'display_name', 'manufacturer', 'url',
                     'software_version']
            if hasattr(self, k)
        }
        return pd.Series(properties, dtype=object)

    @property
    def buffer_size(self) -> int:
        """Get buffer size."""
        if self._buffer_size is None:
            self._buffer_bounds_check = False
            payload_size_set = False
            try:
                max_i2c_payload_size = self.max_i2c_payload_size()
                payload_size_set = True
            except AttributeError:
                max_i2c_payload_size = sys.maxsize
            try:
                max_serial_payload_size = self.max_serial_payload_size()
                payload_size_set = True
            except AttributeError:
                max_serial_payload_size = sys.maxsize
            if not payload_size_set:
                raise IOError(
                    'Could not determine maximum packet payload size. '
                    'Make sure at least one of the following methods is '
                    'defined: `max_i2c_payload_size` method or '
                    '`max_serial_payload_size`.')
            self._buffer_size = min(max_serial_payload_size,
                                    max_i2c_payload_size)
            self._buffer_bounds_check = True
        return self._buffer_size

    @property
    def queues(self) -> pd.Series:
        """Get packet queues."""
        return self._packet_queue_manager.packet_queues

    def _send_command(self,
                     packet: cPacket,
                     timeout_s: Optional[int] = None,
                     poll: Optional[bool] = sd.threaded.POLL_QUEUES) -> None:
        """Send command to device."""
        raise NotImplementedError


class I2cProxyMixin:
    """Mixin for I2C proxy functionality."""
    def __init__(self, i2c_address: str, proxy: 'Proxy') -> None:
        self.proxy = proxy
        self.address = i2c_address

    def _send_command(self, packet: cPacket) -> cPacket:
        """Send command via I2C."""
        response = self.proxy.i2c_request(
            self.address, list(map(ord, packet.data())))
        return cPacket(data=response.tostring(), type_=PACKET_TYPES.DATA)

    def __del__(self) -> None:
        """Cleanup."""
        pass


def serial_ports(
    device_name: Optional[str] = None,
    timeout: Optional[float] = 5.,
    allow_multiple: Optional[bool] = False,
    **kwargs: Any
) -> pd.DataFrame:
    """
    Get list of available serial ports.

    Parameters
    ----------
    device_name : str, optional
        Device name as reported by ID_RESPONSE packet.
        If None, return all serial ports that are not busy.
    timeout : float, optional
        Maximum seconds to wait for response from each serial device.
        If None, call will block until response is received.
    allow_multiple : bool, optional
        Allow multiple devices with the same name.
    **kwargs
        Keyword arguments to pass to available_devices() function.

    Returns
    -------
    pd.DataFrame
        Table of serial ports that match the specified device_name.
        If no device name was specified, returns all serial ports
        that are not busy.
    """
    if device_name is None:
        # No device name specified in base class.
        return sd.comports(only_available=True)
    else:
        # Device name specified in base class.
        # Only return serial ports with matching device name in ``ID_RESPONSE`` packet.
        df_comports = available_devices(timeout=timeout, **kwargs)
        if 'device_name' not in df_comports:
            # No devices found with matching name.
            raise DeviceNotFound('No named devices found.')
        elif df_comports.shape[0]:
            df_comports = df_comports.loc[
                df_comports.device_name == device_name].copy()
        if not df_comports.shape[0]:
            raise DeviceNotFound('No devices found with matching name.')
        elif df_comports.shape[0] > 1 and not allow_multiple:
            # Multiple devices found with matching name.
            raise MultipleDevicesFound(df_comports)
        return df_comports


class SerialProxyMixin:
    """Mixin for serial proxy functionality."""
    def __init__(self, **kwargs: Any) -> None:
        """
        Initialize serial proxy or attempt to auto-connect to a proxy.

        Parameters
        ----------
        port : str, optional
            Serial port to connect to.
        baudrate : int, optional
            Baud rate to use. Default: 115200
        settling_time_s : float, optional
            If specified, wait :data:`settling_time_s` seconds after
            establishing serial connection before trying to execute test
            command.

            Useful, for example, to allow Arduino boards that reset upon
            connection to initialize before attempting serial communication.

            Default: 25 ms (i.e., ``0.025``).

        Version log
        -----------
        .. versionchanged:: 0.51.4
            Change default from 0 s to 25 ms.
            retry_count : int, optional
            Deprecated as of 0.40.

        .. versionchanged:: 0.40
            Deprecate :data:`retry_count` arg.

            Use :data:`settling_time_s` as timeout for async calls.  Prior to
            version 0.40, this async calls would block indefinitely if device
            didn't respond with an :data:`ID_RESPONSE` packet.

            Test identity using :func:`read_device_id` function, with fallback
            to :data:`self.properties['package_name']` for devices that do not
            respond with :data:`ID_RESPONSE` packet.

        .. versionchanged:: 0.50
            Add `serial_signals` signal namespace and emit ``connected`` and
            ``disconnected`` signals when corresponding events occur.

        .. versionchanged:: 0.51
            Add thread-safety to `_send_command` method using lock.
        """
        port = kwargs.pop('port', None)
        baudrate = kwargs.pop('baudrate', 115200)

        if 'retry_count' in kwargs:
            warnings.warn(
                '`retry_count` arg is deprecated and will be removed in '
                'future releases.', DeprecationWarning)
            kwargs.pop('retry_count', None)

        self.serial_signals = blinker.Namespace()
        self.ignore = kwargs.pop('ignore', None)
        self._settling_time_s = kwargs.pop('settling_time_s', 0.025)

        self.serial_thread = None
        self._command_lock = threading.Lock()

        super(SerialProxyMixin, self).__init__(**kwargs)

        # Event to indicate that device has been connected to and correctly
        # identified.
        self.device_verified = threading.Event()

        self._connect(port=port, baudrate=baudrate)

    @property
    def port(self) -> Optional[str]:
        """Get serial port."""
        try:
            port = self.serial_thread.protocol.port
        except Exception:
            port = None
        return port

    @property
    def baudrate(self) -> Optional[int]:
        """Get baud rate."""
        try:
            baudrate = self.serial_thread.protocol.transport.serial.baudrate
        except Exception:
            baudrate = None
        return baudrate

    def reconnection_made(self, protocol: serial.threaded.Protocol) -> None:
        """
        Callback called if/when a device is reconnected to port after lost connection.
        """
        logger.debug(f'Reconnected to `{protocol.port}`')

    def connection_lost(self,
                       protocol: serial.threaded.Protocol,
                       exception: Exception) -> None:
        """Callback called if/when serial connection is lost."""
        logger.debug(f'Connection lost `{protocol.port}`')

    def _connect(self,
                port: Optional[str] = None,
                baudrate: Optional[int] = None,
                settling_time_s: Optional[float] = None,
                retry_count: Optional[int] = None,
                ignore: Optional[bool] = None) -> None:
        """
        Connect to serial device.

        Parameters
        ----------
        port : str, optional
            Serial port to connect to.
        baudrate : int, optional
            Baud rate to use.
        settling_time_s : float, optional
            If specified, wait :data:`settling_time_s` seconds after
            establishing serial connection before trying to execute test
            command.

            Useful, for example, to allow Arduino boards that reset upon
            connection to initialize before attempting serial communication.

            By default, :data:`settling_time_s` is assumed to be zero.
        retry_count : int, optional
            Deprecated.
        ignore : bool or list, optional
            List of non-critical exception types to ignore during
            initialization.

            This allows, for example:

             - Connecting to a device with a different version.

            If set to ``True``, all optional exception types to ignore during
            initialization.

            Default is to raise all exceptions encountered during
            initialization.

        Parameters are saved as defaults upon successful connection.

        Version log
        -----------
        .. versionchanged:: 0.40
            Deprecate :data:`retry_count` arg.

            Use :data:`settling_time_s` as timeout for async calls.  Prior to
            version 0.40, this async calls would block indefinitely if device
            didn't respond with an :data:`ID_RESPONSE` packet.

            Test identity using :func:`read_device_id` function, with fallback
            to :data:`self.properties['package_name']` for devices that do not
            respond with :data:`ID_RESPONSE` packet.
        .. versionchanged:: 0.40.2
            Use :data:`settling_time_s` as timeout when querying available
            devices.
        .. versionchanged:: 0.40.3
            Fix case where single :data:`port` is specified explicitly with
            multiple devices available.
        .. versionchanged:: 0.50
            Emit ``connected`` and ``disconnected`` signals in the
            `serial_signals` namespace when corresponding events occur.
        .. versionchanged:: 0.51.2
            Use specified ``baudrate`` _and_ ``settling_time_s`` to query
            device ID.  This is required to support devices that cannot
            communicate using the default baudrate of 9600, e.g.,
            ``pro8MHzatmega328``.
        """
        if port is None and self.port:
            port = self.port
        if settling_time_s is None:
            try:
                settling_time_s = self._settling_time_s
            except Exception:
                settling_time_s = 0

        if ignore is None and self.ignore:
            ignore = self.ignore
        if not ignore:
            ignore = []
        elif isinstance(ignore, bool):
            # If `ignore` is set to `True`, ignore all optional exceptions.
            ignore = [DeviceVersionMismatch]

        if baudrate is None:
            try:
                baudrate = self.baudrate
            except Exception:
                baudrate = 115200

        if retry_count is not None:
            warnings.warn(
                '`retry_count` arg is deprecated and will be removed in '
                'future releases.',
                DeprecationWarning
            )

        parent = self

        class PacketProtocol(sd.threaded.EventProtocol):
            def connection_made(self, transport):
                super(PacketProtocol, self).connection_made(transport)
                if parent.device_verified.is_set():
                    # Device identity has been previously verified.
                    # Must be reconnecting after lost connection.
                    parent.reconnection_made(self)
                parent.serial_signals.signal('connected').send(
                    {'event': 'connected', 'device': transport}
                )

            def data_received(self, data):
                # New data received from serial port.  Parse using queue manager.
                try:
                    parent._packet_queue_manager.parse(data)
                    parent.serial_signals.signal('data_received').send(
                        {'event': 'data_received', 'data': data}
                    )
                except Exception as e:
                    logger.error(f"Error processing received data: {e}")

            def connection_lost(self, exception):
                try:
                    super(PacketProtocol, self).connection_lost(exception)
                    parent.connection_lost(self, exception)
                    parent.serial_signals.signal('disconnected').send(
                        {'event': 'disconnected', 'exception': exception}
                    )
                except Exception as e:
                    logger.error(f"Error handling connection loss: {e}")

        device_name = getattr(self, 'device_name', None)

        if isinstance(port, str):
            # Single port was explicitly specified.
            df_comports = serial_ports(
                device_name=device_name,
                baudrate=baudrate,
                settling_time_s=settling_time_s,
                allow_multiple=True
            )
            ports = [port]
        else:
            df_comports = serial_ports(
                device_name=device_name,
                settling_time_s=settling_time_s,
                baudrate=baudrate
            )
            if port is None:
                ports = df_comports.index.tolist()
            else:
                # List of ports was specified.
                ports = port

        for port_i in ports:
            if port_i not in df_comports.index:
                raise DeviceNotFound(
                    f"No {'device_name ' if device_name else ''}"
                    f"device available on port {port_i}"
                )

        for port_i in ports:
            try:
                device_id = read_device_id(
                    port=port_i,
                    timeout=2 * settling_time_s,
                    settling_time_s=settling_time_s,
                    baudrate=baudrate
                )

                if device_id is not None and device_name is not None:
                    if device_id.get('device_name') != device_name:
                        raise DeviceNotFound(
                            f"Device `{device_id}` does not match "
                            f"expected name `{device_name}`"
                        )
                    elif not (device_id.get('device_version') ==
                            getattr(self, 'device_version', None)):
                        if DeviceVersionMismatch in ignore:
                            logger.warning(
                                f"Device driver version ({self.device_version}) "
                                f"does not match version reported by device "
                                f"({device_id.get('device_version')})."
                            )
                        else:
                            raise DeviceVersionMismatch(
                                self, device_id.get('device_version')
                            )

                logger.debug(
                    f'Attempt to connect to device on port {port_i} '
                    f'(baudrate={baudrate})'
                )
                # Launch background thread to:
                #
                #  - Connect to serial port
                #  - Listen for incoming data and parse into packets.
                #  - Attempt to reconnect if disconnected.
                self.serial_thread = sd.threaded.KeepAliveReader(
                    PacketProtocol, port_i,
                    baudrate=baudrate
                ).__enter__()
                event = OrEvent(
                    self.serial_thread.closed,
                    self.serial_thread.connected
                )
                logger.debug(f'Wait for connection to port {port_i}')
                event.wait(timeout=2.0)  # Short timeout for connection
                
                if self.serial_thread.error.is_set():
                    raise self.serial_thread.error.exception

                time.sleep(settling_time_s)

                try:
                    self.ram_free()
                    if device_id is None:
                        properties = self.properties
                        device_id = {'device_name': properties['package_name']}
                    
                    logger.info(
                        f"Successfully connected to {device_id['device_name']} "
                        f"on port {port_i}"
                    )
                    self.device_verified.set()
                    return
                except IOError:
                    logger.debug(f'Connection unsuccessful on port {port_i}')
                    continue
                except Exception as e:
                    logger.warning(f"Failed to connect to port {port_i}: {e}")
                    continue
            except Exception as e:
                logger.warning(f"Failed to connect to port {port_i}: {e}")
                continue

        raise IOError('Device not found on any port.')

    def terminate(self) -> None:
        """Terminate the serial connection."""
        if self.serial_thread is not None:
            self.serial_thread.__exit__()

    def _send_command(self,
                     packet: cPacket,
                     timeout_s: Optional[float] = None,
                     poll: Optional[bool] = sd.threaded.POLL_QUEUES) -> cPacket:
        """
        Send command to device.

        Parameters
        ----------
        packet : cPacket
            Packet to send.
        timeout_s : float, optional
            Timeout in seconds.
        poll : bool, optional
            Whether to poll queues.

        Returns
        -------
        cPacket
            Response packet.

        Raises
        ------
        IOError
            If packet size is too large or no response is received.
        """
        if timeout_s is None:
            timeout_s = self._timeout_s

        if self._buffer_bounds_check and len(packet.data()) > self.buffer_size:
            raise IOError(
                f'Packet size {len(packet.data()) - self.buffer_size} '
                f'bytes too large.'
            )

        with self._command_lock:
            # Flush outstanding data packets.
            for p in range(self.queues['data'].qsize()):
                self.queues['data'].get()

            try:
                timestamp, response = self.serial_thread.request(
                    self.queues['data'],
                    packet.tostring(),
                    timeout_s=timeout_s,
                    poll=poll
                )
            except queue.Empty:
                raise IOError('Did not receive response.')
        return response


class ConfigMixinBase:
    """
    Mixin class to add convenience wrappers around config getter/setter.

    **N.B.,** Sub-classes *MUST* implement the `config_class` method to return
    the `Config` class type for the proxy.
    """

    @property
    def config_class(self) -> Type:
        """Get configuration class."""
        raise NotImplementedError(
            'Sub-classes must implement this method to return the `Config` '
            'class type for the proxy.')

    @property
    def _config_pb(self) -> Any:
        """Get configuration protobuf."""
        return self.config_class.FromString(
            self.serialize_config().tostring())

    @property
    def config(self) -> pd.Series:
        """Get configuration."""
        try:
            fv = resolve_field_values(self._config_pb,
                                      set_default=True).set_index(['full_name'])
            return pd.Series(
                {k: PYTYPE_MAP[v.field_desc.type](v.value)
                 for k, v in fv.iterrows()},
                dtype=object)
        except ValueError:
            return pd.Series()

    @config.setter
    def config(self, value: pd.Series) -> None:
        """Set configuration."""
        if hasattr(value, 'to_dict'):
            value = value.to_dict()
        self.update_config(**value)

    def update_config(self, **kwargs: Any) -> int:
        """
        Update fields in the config object based on keyword arguments.
        
        By default, these values will be saved to EEPROM. To prevent this
        (e.g., to verify system behavior before committing the changes), you
        can pass the special keyword argument 'save=False'. In this case, you
        will need to call the method save_config() to make your changes
        persistent.
        
        Parameters
        ----------
        **kwargs
            Configuration values to update.

        Returns
        -------
        int
            Return code.
        """
        save = True
        if 'save' in kwargs and not kwargs.pop('save'):
            save = False

        # convert dictionary to a protobuf
        config_pb = self.config_class(**kwargs)
        return_code = super().update_config(config_pb)

        if save:
            super(ConfigMixinBase, self).save_config()

        return return_code

    def reset_config(self, **kwargs: Any) -> None:
        """
        Reset fields in the config object to their default values.
        
        By default, these values will be saved to EEPROM. To prevent this
        (e.g., to verify system behavior before committing the changes), you
        can pass the special keyword argument 'save=False'. In this case, you
        will need to call the method save_config() to make your changes
        persistent.
        
        Parameters
        ----------
        **kwargs
            Additional keyword arguments.
        """
        save = True
        if 'save' in kwargs and not kwargs.pop('save'):
            save = False

        super().reset_config()
        if save:
            super().save_config()


class StateMixinBase:
    """
    Mixin class to add convenience wrappers around state getter/setter.

    **N.B.,** Sub-classes *MUST* implement the `state_class` method to return
    the `State` class type for the proxy.
    """

    @property
    def state_class(self) -> Type:
        """Get state class."""
        raise NotImplementedError(
            'Sub-classes must implement this method to return the `State` '
            'class type for the proxy.')

    @property
    def _state_pb(self) -> Any:
        """Get state protobuf."""
        return self.state_class.FromString(
            self.serialize_state().tostring())

    @property
    def state(self) -> pd.Series:
        """Get state."""
        try:
            fv = (resolve_field_values(self._state_pb,
                                     set_default=True).set_index(['full_name']))
            return pd.Series(
                {k: PYTYPE_MAP[v.field_desc.type](v.value)
                 for k, v in fv.iterrows()},
                dtype=object)
        except ValueError:
            return pd.Series()

    @state.setter
    def state(self, value: pd.Series) -> None:
        """Set state."""
        if hasattr(value, 'to_dict'):
            value = value.to_dict()
        self.update_state(**value)

    def update_state(self, **kwargs: Any) -> int:
        """
        Update state.

        Parameters
        ----------
        **kwargs
            State values to update.

        Returns
        -------
        int
            Return code.
        """
        state = self.state_class(**kwargs)
        return super().update_state(state)
