from __future__ import absolute_import
from collections import OrderedDict
import logging
import pkg_resources
import sys
import threading
import time
import warnings

from arduino_rpc.protobuf import resolve_field_values, PYTYPE_MAP
from nadamq.NadaMq import cPacket, PACKET_TYPES
from or_event import OrEvent
from six.moves import map
from six.moves import range
from six.moves import queue
import blinker
import serial
import serial_device as sd
import serial_device.threaded
import six

from .queue import PacketQueueManager
from . import __version__, available_devices, read_device_id

logger = logging.getLogger(__name__)


class DeviceNotFound(Exception):
    pass


class MultipleDevicesFound(Exception):
    def __init__(self, *args, **kwargs):
        self.df_comports = kwargs.pop('df_comports', None)
        super(MultipleDevicesFound, self).__init__(*args, **kwargs)


class DeviceVersionMismatch(Exception):
    def __init__(self, device, device_version):
        self.device = device
        self.device_version = device_version
        super(DeviceVersionMismatch, self).__init__()

    def __str__(self):
        return ('Device driver version (%s) does not match version reported '
                'by device (%s).' % (self.device.device_version,
                                     self.device_version))


class ProxyBase(object):
    host_package_name = None

    def __init__(self, buffer_bounds_check=True, high_water_mark=10,
                 timeout_s=10, **kwargs):
        '''
        .. versionchanged:: 0.43
            Ignore extra keyword arguments (rather than throwing an exception).
        '''
        self._buffer_bounds_check = buffer_bounds_check
        self._buffer_size = None
        self._packet_queue_manager = \
            PacketQueueManager(high_water_mark=high_water_mark)
        self._timeout_s = timeout_s

    @property
    def host_software_version(self):
        # Get host software version from the module's __version__ attribute
        # (see PEP 396[1]).
        #
        # [1]: https://www.python.org/dev/peps/pep-0396/
        exec('from %s import __version__ as host_version' %
             self.__module__.split('.')[0])
        return pkg_resources.parse_version(host_version)

    @property
    def remote_software_version(self):
        return pkg_resources.parse_version(self.properties.software_version)

    @property
    def stream(self):
        return self._stream

    @stream.setter
    def stream(self, stream):
        self._stream = stream
        self.reset()

    @property
    def high_water_mark(self):
        return self._packet_queue_manager.high_water_mark

    @high_water_mark.setter
    def high_water_mark(self, message_count):
        self._packet_queue_manager.high_water_mark = message_count

    def help(self):
        '''
        Open project webpage in new browser tab.
        '''
        import webbrowser

        url = self.properties.url
        if url:
            webbrowser.open_new_tab(url)

    @property
    def properties(self):
        import pandas as pd

        properties = OrderedDict([(k, getattr(self, k)().tostring())
                                  for k in ['base_node_software_version',
                                            'package_name', 'display_name',
                                            'manufacturer', 'url',
                                            'software_version']
                                  if hasattr(self, k)])
        return pd.Series(properties, dtype=object)

    @property
    def buffer_size(self):
        if self._buffer_size is None:
            self._buffer_bounds_check = False
            payload_size_set = False
            try:
                max_i2c_payload_size = self.max_i2c_payload_size()
                payload_size_set = True
            except AttributeError:
                max_i2c_payload_size = sys.maxint
            try:
                max_serial_payload_size = self.max_serial_payload_size()
                payload_size_set = True
            except AttributeError:
                max_serial_payload_size = sys.maxint
            if not payload_size_set:
                raise IOError('Could not determine maximum packet payload '
                              'size. Make sure at least one of the following '
                              'methods is defined: `max_i2c_payload_size` '
                              'method or `max_serial_payload_size`.')
            self._buffer_size = min(max_serial_payload_size,
                                    max_i2c_payload_size)
            self._buffer_bounds_check = True
        return self._buffer_size

    @property
    def queues(self):
        return self._packet_queue_manager.packet_queues

    def _send_command(self, packet, timeout_s=None,
                      poll=sd.threaded.POLL_QUEUES):
        raise NotImplementedError


class I2cProxyMixin(object):
    def __init__(self, i2c_address, proxy):
        self.proxy = proxy
        self.address = i2c_address

    def _send_command(self, packet):
        response = self.proxy.i2c_request(self.address,
                                          list(map(ord, packet.data())))
        return cPacket(data=response.tostring(), type_=PACKET_TYPES.DATA)

    def __del__(self):
        pass


def serial_ports(device_name=None, timeout=5., allow_multiple=False):
    '''
    Parameters
    ----------
    device_name : str, optional
        Device name as reported by :data:`ID_RESPONSE` packet.

        If ``None``, return all serial ports that are not busy.
    timeout : float, optional
        Maximum number of seconds to wait for a response from each serial
        device.

        If ``None``, call will block until response is received from each
        serial port.
    allow_multiple : bool, optional
        Allow multiple devices with the same name.

    Returns
    -------
    pandas.DataFrame
        Table of serial ports that match the specified :data:`device_name`.

        If no device name was specified, returns all serial ports that are not
        busy.

    .. versionadded:: 0.40
    .. versionchanged:: 0.40.3
        Add :data:`allow_multiple` argument.
    '''
    if device_name is None:
        # No device name specified in base class.
        return sd.comports(only_available=True)
    else:
        # Device name specified in base class.
        # Only return serial ports with matching device name in ``ID_RESPONSE``
        # packet.
        df_comports = available_devices(timeout=timeout)
        if 'device_name' not in df_comports:
            # No devices found with matching name.
            raise DeviceNotFound('No named devices found.')
        elif df_comports.shape[0]:
            df_comports = df_comports.loc[df_comports.device_name ==
                                          device_name].copy()
        if not df_comports.shape[0]:
            raise DeviceNotFound('No devices found with matching name.')
        elif df_comports.shape[0] > 1 and not allow_multiple:
            # Multiple devices found with matching name.
            raise MultipleDevicesFound(df_comports)
        return df_comports


class SerialProxyMixin(object):
    def __init__(self, **kwargs):
        '''
        Attempt to auto-connect to a proxy.

        Parameters
        ----------
        port : str, optional
        baudrate : int, optional
        settling_time_s : float, optional
            If specified, wait :data:`settling_time_s` seconds after
            establishing serial connection before trying to execute test
            command.

            Useful, for example, to allow Arduino boards that reset upon
            connection to initialize before attempting serial communication.

            By default, :data:`settling_time_s` is assumed to be zero.
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

        .. versionchanged:: X.X.X
            Add thread-safety to `_send_command` method using lock.
        '''
        port = kwargs.pop('port', None)
        baudrate = kwargs.pop('baudrate', 115200)

        if 'retry_count' in kwargs:
            warnings.warn('`retry_count` arg is deprecated and will be removed'
                          ' in future releases.', DeprecationWarning)
            kwargs.pop('retry_count', None)

        self.serial_signals = blinker.Namespace()
        self.ignore = kwargs.pop('ignore', None)
        self._settling_time_s = kwargs.pop('settling_time_s', 0)

        self.serial_thread = None
        self._command_lock = threading.Lock()

        super(SerialProxyMixin, self).__init__(**kwargs)

        # Event to indicate that device has been connected to and correctly
        # identified.
        self.device_verified = threading.Event()

        self._connect(port=port, baudrate=baudrate)

    @property
    def port(self):
        try:
            port = self.serial_thread.protocol.port
        except Exception:
            port = None
        return port

    @property
    def baudrate(self):
        try:
            baudrate = self.serial_thread.protocol.transport.serial.baudrate
        except Exception:
            baudrate = None
        return baudrate

    def reconnection_made(self, protocol):
        '''
        Callback called if/when device is reconnected to port after lost
        connection.
        '''
        logger.debug('Reconnected to `%s`', protocol.port)

    def connection_lost(self, protocol, exception):
        '''
        Callback called if/when serial connection is lost.
        '''
        logger.debug('Connection lost `%s`', protocol.port)

    def _connect(self, port=None, baudrate=None, settling_time_s=None,
                 retry_count=None, ignore=None):
        '''
        Parameters
        ----------
        port : str, optional
        baudrate : int, optional
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
        '''
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
            warnings.warn('`retry_count` arg is deprecated and will be removed'
                          ' in future releases.', DeprecationWarning)

        parent = self

        class PacketProtocol(sd.threaded.EventProtocol):
            def connection_made(self, transport):
                super(PacketProtocol, self).connection_made(transport)
                if parent.device_verified.is_set():
                    # Device identity has been previously verified.
                    # Must be reconnecting after lost connection.
                    parent.reconnection_made(self)
                parent.serial_signals.signal('connected').send({'event':
                                                                'connected',
                                                                'device':
                                                                transport})

            def data_received(self, data):
                # New data received from serial port.  Parse using queue
                # manager.
                parent._packet_queue_manager.parse(data)
                parent.serial_signals.signal('data_received')\
                    .send({'event': 'data_received', 'data': data})

            def connection_lost(self, exception):
                super(PacketProtocol, self).connection_lost(exception)
                parent.connection_lost(self, exception)
                parent.serial_signals.signal('disconnected')\
                    .send({'event': 'disconnected', 'exception': exception})

        # Look up device information for all available ports.
        device_name = getattr(self, 'device_name', None)

        if isinstance(port, six.string_types):
            # Single port was explicitly specified.
            df_comports = serial_ports(device_name=device_name,
                                       timeout=settling_time_s,
                                       allow_multiple=True)
            ports = [port]
        else:
            df_comports = serial_ports(device_name=device_name,
                                       timeout=settling_time_s)
            if port is None:
                ports = df_comports.index.tolist()
            else:
                # List of ports was specified.
                ports = port

        for port_i in ports:
            if port_i not in df_comports.index:
                raise DeviceNotFound('No %sdevice available on port %s' %
                                     (device_name + ' ' if device_name else '',
                                      port_i))

        for port_i in ports:
            # Read device ID.
            device_id = read_device_id(port=port_i, timeout=settling_time_s)

            if device_id is not None and device_name is not None:
                if device_id.get('device_name') != device_name:
                    # No devices found with matching name.
                    raise DeviceNotFound('Device `%s` does not match expected '
                                         'name `%s`' % (device_id,
                                                        device_name))
                elif not (device_id.get('device_version') ==
                          getattr(self, 'device_version', None)):
                    # Mismatch between device driver version and version
                    # reported by device.
                    if DeviceVersionMismatch in ignore:
                        logger.warn('Device driver version (%s) does not '
                                    'match version reported by device '
                                    '(%s).', self.device_version,
                                    device_id.get('device_version'))
                    else:
                        raise DeviceVersionMismatch(self, device_id
                                                    .get('device_version'))

            try:
                logger.debug('Attempt to connect to device on port %s '
                             '(baudrate=%s)', port_i, baudrate)
                # Launch background thread to:
                #
                #  - Connect to serial port
                #  - Listen for incoming data and parse into packets.
                #  - Attempt to reconnect if disconnected.
                self.serial_thread = (sd.threaded
                                      .KeepAliveReader(PacketProtocol, port_i,
                                                       baudrate=baudrate)
                                      .__enter__())
                event = OrEvent(self.serial_thread.closed,
                                self.serial_thread.connected)
            except serial.SerialException:
                continue

            logger.debug('Wait for connection to port %s', port_i)
            event.wait()
            if self.serial_thread.error.is_set():
                raise self.serial_thread.error.exception

            time.sleep(settling_time_s)

            try:
                self.ram_free()
                if device_id is None:
                    properties = self.properties
                    device_id = {'device_name': properties['package_name']}
            except IOError:
                logger.debug('Connection unsuccessful on port %s' % port_i)
                continue

            logger.info('Successfully connected to %s on port %s',
                        device_id['device_name'], port_i)
            self.device_verified.set()
            return
        raise IOError('Device not found on any port.')

    def terminate(self):
        if self.serial_thread is not None:
            self.serial_thread.__exit__()

    def _send_command(self, packet, timeout_s=None,
                      poll=sd.threaded.POLL_QUEUES):
        '''
        .. versionchanged:: X.X.X
            Add thread-safety using lock.
        '''
        if timeout_s is None:
            timeout_s = self._timeout_s

        if self._buffer_bounds_check and len(packet.data()) > self.buffer_size:
            raise IOError('Packet size %s bytes too large.' %
                          (len(packet.data()) - self.buffer_size))

        with self._command_lock:
            # Flush outstanding data packets.
            for p in range(self.queues['data'].qsize()):
                self.queues['data'].get()

            try:
                timestamp, response = (self.serial_thread
                                       .request(self.queues['data'],
                                                packet.tostring(),
                                                timeout_s=timeout_s,
                                                poll=poll))
            except queue.Empty:
                raise IOError('Did not receive response.')
        return response


class ConfigMixinBase(object):
    '''
    Mixin class to add convenience wrappers around config getter/setter.

    **N.B.,** Sub-classes *MUST* implement the `config_class` method to return
    the `Config` class type for the proxy.
    '''
    @property
    def config_class(self):
        raise NotImplementedError('Sub-classes must implement this method to '
                                  'return the `Config` class type for the '
                                  'proxy.')

    @property
    def _config_pb(self):
        return self.config_class.FromString(self.serialize_config().tostring())

    @property
    def config(self):
        import pandas as pd

        try:
            fv = resolve_field_values(self._config_pb,
                                      set_default=True).set_index(['full_name'])
            return pd.Series(OrderedDict([(k, PYTYPE_MAP[v.field_desc
                                                         .type](v.value))
                                          for k, v in fv.iterrows()]),
                             dtype=object)
        except ValueError:
            return pd.Series()

    @config.setter
    def config(self, value):
        # convert pandas Series to a dictionary if necessary
        if hasattr(value, 'to_dict'):
            value = value.to_dict()

        self.update_config(**value)

    def update_config(self, **kwargs):
        '''
        Update fields in the config object based on keyword arguments.

        By default, these values will be saved to EEPROM. To prevent this
        (e.g., to verify system behavior before committing the changes), you
        can pass the special keyword argument 'save=False'. In this case, you
        will need to call the method save_config() to make your changes
        persistent.
        '''
        save = True
        if 'save' in kwargs and not kwargs.pop('save'):
            save = False

        # convert dictionary to a protobuf
        config_pb = self.config_class(**kwargs)

        return_code = super(ConfigMixinBase, self).update_config(config_pb)

        if save:
            super(ConfigMixinBase, self).save_config()

        return return_code

    def reset_config(self, **kwargs):
        '''
        Reset fields in the config object to their default values.

        By default, these values will be saved to EEPROM. To prevent this
        (e.g., to verify system behavior before committing the changes), you
        can pass the special keyword argument 'save=False'. In this case, you
        will need to call the method save_config() to make your changes
        persistent.
        '''
        save = True
        if 'save' in kwargs and not kwargs.pop('save'):
            save = False

        super(ConfigMixinBase, self).reset_config()
        if save:
            super(ConfigMixinBase, self).save_config()


class StateMixinBase(object):
    '''
    Mixin class to add convenience wrappers around state getter/setter.

    **N.B.,** Sub-classes *MUST* implement the `state_class` method to return
    the `State` class type for the proxy.
    '''
    @property
    def state_class(self):
        raise NotImplementedError('Sub-classes must implement this method to '
                                  'return the `State` class type for the '
                                  'proxy.')

    @property
    def _state_pb(self):
        return self.state_class.FromString(self.serialize_state().tostring())

    @property
    def state(self):
        import pandas as pd

        try:
            fv = (resolve_field_values(self._state_pb,
                                       set_default=True)
                  .set_index(['full_name']))
            return pd.Series(OrderedDict([(k, PYTYPE_MAP[v.field_desc
                                                         .type](v.value))
                                          for k, v in fv.iterrows()]),
                             dtype=object)
        except ValueError:
            return pd.Series()

    @state.setter
    def state(self, value):
        # convert pandas Series to a dictionary if necessary
        if hasattr(value, 'to_dict'):
            value = value.to_dict()

        self.update_state(**value)

    def update_state(self, **kwargs):
        state = self.state_class(**kwargs)
        return super(StateMixinBase, self).update_state(state)
