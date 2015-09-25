import time
import datetime
import sys
from collections import OrderedDict

from nadamq.NadaMq import cPacket, PACKET_TYPES
from .queue import SerialStream, PacketWatcher


class ProxyBase(object):
    def __init__(self, serial, buffer_bounds_check=True, high_water_mark=10):
        self._serial = serial
        self._buffer_bounds_check = buffer_bounds_check
        self._buffer_size = None
        self._packet_watcher = None
        self._reset_packet_watcher(serial, high_water_mark)
        self._timeout_s = 0.5

    def _reset_packet_watcher(self, serial, high_water_mark):
        stream = SerialStream(serial)
        packet_watcher = PacketWatcher(stream, high_water_mark=high_water_mark)
        packet_watcher.start()

        # Terminate existing watcher thread.
        if self._packet_watcher is not None:
            self._packet_watcher.terminate()

        # Set new watcher.
        self._packet_watcher = packet_watcher
        self._serial = serial
        self._stream = stream
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

        url = self.properties().get('url')
        if url:
            webbrowser.open_new_tab(url)

    def properties(self):
        import pandas as pd

        return pd.Series(OrderedDict([(k, getattr(self, k)().tostring())
                                      for k in ['base_node_software_version',
                                                'name', 'manufacturer', 'url',
                                                'software_version']
                                      if hasattr(self, k)]))

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
            self._serial.write(packet.tostring())
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


def connect(proxy_class, baudrate=115200, name=None, verify=None,
            retry_count=6, high_water_mark=None, port=None):
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

    if name and verify is None:
        verify = lambda p: p.properties()['name'] == name

    if port is None:
        ports = get_serial_ports()
    else:
        ports = [port]

    for port in ports:
        for i in xrange(retry_count):
            serial_device = Serial(port, baudrate=baudrate)
            time.sleep(.5 * i)
            kwargs = {}
            if high_water_mark:
                kwargs['high_water_mark'] = high_water_mark
            proxy = proxy_class(serial_device, **kwargs)
            try:
                proxy.ram_free()
            except IOError:
                if i >= retry_count - 1: raise
                proxy.terminate()
                serial_device.close()
                continue
            if verify is None:
                return proxy
            else:
                try:
                    if verify(proxy):
                        return proxy
                except:
                    proxy.terminate()
                    serial_device.close()
    raise IOError('Device not found on any port.')
