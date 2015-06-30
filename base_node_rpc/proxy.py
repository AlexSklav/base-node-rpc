from collections import OrderedDict

import arduino_rpc.proxy as ap
from nadamq.NadaMq import cPacket, PACKET_TYPES


class ProxyBase(ap.ProxyBase):
    def __init__(self, serial):
        self._serial = serial
        self._buffer_bounds_check = True
        self._buffer_size = None

    def properties(self):
        import pandas as pd

        return pd.Series(OrderedDict([(k, getattr(self, k)().tostring())
                                      for k in ['name', 'manufacturer', 'url',
                                                'software_version']
                                      if hasattr(self, k)]))

    @property
    def buffer_size(self):
        if self._buffer_size is None:
            self._buffer_bounds_check = False
            self._buffer_size = self.max_payload_size()
            self._buffer_bounds_check = True
        return self._buffer_size

    def _send_command(self, packet):
        if self._buffer_bounds_check and len(packet.data()) > self.buffer_size:
            raise IOError('Packet size %s bytes too large.' %
                          (len(packet.data()) - self.buffer_size))
        return super(ProxyBase, self)._send_command(packet)


class I2cProxyMixin(object):
    def __init__(self, i2c_address, proxy):
        self.proxy = proxy
        self.address = i2c_address

    def _send_command(self, packet):
        response = self.proxy.i2c_request(self.address,
                                          map(ord, packet.data()))
        return cPacket(data=response.tostring(), type_=PACKET_TYPES.DATA)
