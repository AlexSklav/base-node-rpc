from collections import OrderedDict
import Queue
import datetime
import logging
import pkg_resources
import sys
import threading
import time
import types

from arduino_rpc.protobuf import resolve_field_values, PYTYPE_MAP
from nadamq.NadaMq import cPacket, PACKET_TYPES
import serial
import serial_device as sd
import serial_device.threaded
import serial_device.or_event
import path_helpers as ph

from .queue import PacketQueueManager
from . import __version__

logger = logging.getLogger(__name__)


class ProxyBase(object):
    host_package_name = None

    def __init__(self, buffer_bounds_check=True, high_water_mark=10,
                 timeout_s=10):
        self._buffer_bounds_check = buffer_bounds_check
        self._buffer_size = None
        self._packet_queue_manager = PacketQueueManager(high_water_mark=
                                                        high_water_mark)
        self._timeout_s = 10

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
                                          map(ord, packet.data()))
        return cPacket(data=response.tostring(), type_=PACKET_TYPES.DATA)

    def __del__(self):
        pass


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
        '''
        port = kwargs.pop('port', None)
        baudrate = kwargs.pop('baudrate', 115200)


        self._retry_count = kwargs.pop('retry_count', 6)
        self._settling_time_s = kwargs.pop('settling_time_s', 0)

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
                 retry_count=None):
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

        If a successful connection is made, those paramters will be used
        as the new defaults.
        '''
        if port is None and self.port:
            port = self.port

        if port is None:
            ports = sd.comports().index.tolist()
        elif isinstance(port, types.StringTypes):
            ports = [port]
        else:
            ports = port

        if baudrate is None:
            try:
                baudrate = self.baudrate
            except Exception:
                baudrate = 115200

        if settling_time_s is None:
            try:
                settling_time_s = self._settling_time_s
            except Exception:
                settling_time_s = 0

        if retry_count is None:
	    try:
                retry_count = self._retry_count
            except Exception:
                retry_count = 6

        parent = self

        class PacketProtocol(sd.threaded.EventProtocol):
            def connection_made(self, transport):
                super(PacketProtocol, self).connection_made(transport)
                if parent.device_verified.is_set():
                    # Device identity has been previously verified.
                    # Must be reconnecting after lost connection.
                    parent.reconnection_made(self)

            def data_received(self, data):
                # New data received from serial port.  Parse using queue
                # manager.
                parent._packet_queue_manager.parse(data)

            def connection_lost(self, exception):
                super(PacketProtocol, self).connection_lost(exception)
                parent.connection_lost(self, exception)

        for port in ports:
            for i in xrange(retry_count):
                try:
                    logger.debug('Attempt to connect to device on port %s '
                                 '(baudrate=%s)', port, baudrate)
                    # Launch background thread to:
                    #
                    #  - Connect to serial port
                    #  - Listen for incoming data and parse into packets.
                    #  - Attempt to reconnect if disconnected.
                    self.serial_thread = (sd.threaded
                                          .KeepAliveReader(PacketProtocol,
                                                           port,
                                                           baudrate=baudrate)
                                          .__enter__())
                    event = sd.or_event.OrEvent(self.serial_thread.closed,
                                                self.serial_thread.connected)
                except serial.SerialException:
                    continue

                logger.debug('Wait for connection to port %s', port)
                event.wait()
                if self.serial_thread.error.is_set():
                    raise self.serial_thread.error.exception

                time.sleep(settling_time_s + .5 * i)

                try:
                    self.ram_free()
                except IOError:
                    logger.debug('Connection unsuccessful on port %s after %d '
                                 'attempts.', port, i + 1)
                    if i >= retry_count - 1: break
                    self.terminate()
                    continue
                try:
                    properties = self.properties
                    device_package_name = properties['package_name']
                    if (self.host_package_name
                        is None) or (device_package_name ==
                                     self.host_package_name):
                        logger.info('Successfully connected to %s on port %s',
                                    device_package_name, port)
                        self.device_verified.set()
                        return
                    else: # not the device we're looking for
                        logger.info('Package name of device (%s) on port (%s)'
                                    ' does not match package name (%s)',
                                    device_package_name, port,
                                    self.host_package_name)
                        self.terminate()
                        break
                except:
                    # There was an exception, so free the serial port.
                    logger.debug('Exception occurred while querying '
                                 'properties on port %s.', port, exc_info=True)
                    self.terminate()
                    raise

        raise IOError('Device not found on any port.')

    def terminate(self):
        if self.serial_thread is not None:
            self.serial_thread.__exit__()

    def _send_command(self, packet, timeout_s=None,
                      poll=sd.threaded.POLL_QUEUES):
        if timeout_s is None:
            timeout_s = self._timeout_s

        if self._buffer_bounds_check and len(packet.data()) > self.buffer_size:
            raise IOError('Packet size %s bytes too large.' %
                          (len(packet.data()) - self.buffer_size))

        # Flush outstanding data packets.
        for p in xrange(self.queues['data'].qsize()):
            self.queues['data'].get()

        try:
            timestamp, response = (self.serial_thread
                                   .request(self.queues['data'],
                                            packet.tostring(),
                                            timeout_s=timeout_s, poll=poll))
        except Queue.Empty:
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
            return pd.Series(OrderedDict([(k, PYTYPE_MAP[v.field_desc.type](v.value))
                                          for k, v in fv.iterrows()]), dtype=object)
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
        if 'save' in kwargs.keys() and not kwargs.pop('save'):
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
        if 'save' in kwargs.keys() and not kwargs.pop('save'):
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
            fv = resolve_field_values(self._state_pb,
                                      set_default=True).set_index(['full_name'])
            return pd.Series(OrderedDict([(k, PYTYPE_MAP[v.field_desc.type](v.value))
                                          for k, v in fv.iterrows()]), dtype=object)
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
