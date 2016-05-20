import time
import datetime
import sys
from collections import OrderedDict
import logging
import pkg_resources

from arduino_rpc.protobuf import resolve_field_values, PYTYPE_MAP
import serial
from nadamq.NadaMq import cPacket, PACKET_TYPES
from .queue import SerialStream, PacketWatcher

logger = logging.getLogger(__name__)


class ProxyBase(object):
    host_package_name = None

    def __init__(self, stream, buffer_bounds_check=True, high_water_mark=10,
                 auto_close_stream=False):
        self._buffer_bounds_check = buffer_bounds_check
        self._buffer_size = None
        self._packet_watcher = None
        self._timeout_s = 0.5
        self._auto_close_stream = True
        self._stream = stream
        self._reset_packet_watcher(stream, high_water_mark)

    @property
    def host_software_version(self):
        return pkg_resources.parse_version(
             pkg_resources.get_distribution(
                 self.properties['package_name']
             ).version
         )

    @property
    def remote_software_version(self):
        return pkg_resources.parse_version(self.properties.software_version)

    def reset(self):
        self._reset_packet_watcher(self.stream, self.high_water_mark)

    @property
    def stream(self):
        return self._stream

    @stream.setter
    def stream(self, stream):
        self._stream = stream
        self.reset()

    def _reset_packet_watcher(self, stream, high_water_mark):
        packet_watcher = PacketWatcher(stream,
                                       high_water_mark=high_water_mark)
        packet_watcher.start()

        # Terminate existing watcher thread.
        if self._packet_watcher is not None:
            self._packet_watcher.terminate()

        # Set new watcher.
        self._packet_watcher = packet_watcher
        self._packet_watcher.enabled = True

    @property
    def high_water_mark(self):
        return self._packet_watcher.message_parser.high_water_mark

    @high_water_mark.setter
    def high_water_mark(self, message_count):
        self._packet_watcher.message_parser.high_water_mark = message_count

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
        for k in ['id', 'uuid']:
            properties[k] = getattr(self, k)
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

    def _send_command(self, packet):
        if self._buffer_bounds_check and len(packet.data()) > self.buffer_size:
            raise IOError('Packet size %s bytes too large.' %
                          (len(packet.data()) - self.buffer_size))

        # Flush outstanding data packets.
        for p in xrange(self.queues['data'].qsize()):
            self.queues['data'].get()

        self._packet_watcher.enabled = False
        try:
            self._stream.write(packet.tostring())
            result = self._read_response()
        finally:
            self._packet_watcher.enabled = True
        return result

    def _read_response(self):
        start = datetime.datetime.now()
        while self.queues['data'].qsize() < 1:
            self._packet_watcher.parse_available()
            if self._timeout_s < (datetime.datetime.now() -
                                  start).total_seconds():
               raise IOError('Timed out waiting for response.')
        # Return packet from queue.
        return self.queues['data'].get()[1]

    @property
    def queues(self):
        return self._packet_watcher.queues

    def terminate(self):
        self._packet_watcher.terminate()
        if self._auto_close_stream:
            self._stream.close()

    def __del__(self):
        self.terminate()


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

        If `name` is specified, only connect to proxy matching name.
        If `verify` callback is specified, only connect to proxy where `verify`
        returns `True`.
        '''
        # Import here, since other classes in this file do not depend on serial
        # libraries directly.
        from serial import Serial
        from serial_device import get_serial_ports

        baudrate = kwargs.pop('baudrate', 115200)
        retry_count = kwargs.pop('retry_count', 6)
        port = kwargs.pop('port', None)
        auto_close_stream = kwargs.pop('auto_close_stream', True)
        if not auto_close_stream:
            raise ValueError('auto_close_stream must be set to true for '
                             'classes derived from SerialProxyMixin')

        if port is None:
            ports = get_serial_ports()
        else:
            ports = [port]

        first_port = True
        for port in ports:
            for i in xrange(retry_count):
                try:
                    serial_device = Serial(port, baudrate=baudrate)
                except serial.SerialException:
                    continue
                stream = SerialStream(serial_device)

                if first_port:
                    super(SerialProxyMixin, self).__init__(stream,
                                                           auto_close_stream=
                                                           auto_close_stream,
                                                           **kwargs)
                    first_port = False
                else:
                    self.stream = stream

                time.sleep(.5 * i)

                try:
                    self.ram_free()
                except IOError:
                    logger.debug('Connection unsuccessful on port %s after %d '
                                 'attempts.', port, i + 1)
                    if i >= retry_count - 1: break
                    self.terminate()
                    continue
                try:
                    device_package_name = self.properties['package_name']
                    if (self.host_package_name
                        is None) or (device_package_name ==
                                     self.host_package_name):
                        logger.info('Successfully connected to %s on port %s',
                                    device_package_name, port)
                        return
                    else: # not the device we're looking for
                        logger.debug('Package name of device (%s) on port (%s)'
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

        fv = resolve_field_values(self._config_pb,
                                  set_default=True).set_index(['full_name'])
        return pd.Series(OrderedDict([(k, PYTYPE_MAP[v.field_desc.type](v.value))
                                      for k, v in fv.iterrows()]), dtype=object)

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

        fv = resolve_field_values(self._state_pb,
                                  set_default=True).set_index(['full_name'])
        return pd.Series(OrderedDict([(k, PYTYPE_MAP[v.field_desc.type](v.value))
                                      for k, v in fv.iterrows()]), dtype=object)

    @state.setter
    def state(self, value):
        # convert pandas Series to a dictionary if necessary
        if hasattr(value, 'to_dict'):
            value = value.to_dict()

        self.update_state(**value)

    def update_state(self, **kwargs):
        state = self.state_class(**kwargs)
        return super(StateMixinBase, self).update_state(state)
