import struct
import types

import numpy as np


def _data_as_list(data):
    '''
    Parameters
    ----------
    data : list or numpy.array or str
        Data bytes.

    Returns
    -------
    list
        List of integer byte values.
    '''
    if isinstance(data, np.ndarray):
        data = data.tostring()
    if isinstance(data, types.StringTypes):
        data = map(ord, data)
    return data


class TwiBootloader(object):
    def __init__(self, proxy, bootloader_address=0x29):
        '''
        Parameters
        ----------
        proxy : base_node_rpc.Proxy
        address : int
            I2C address of switching board.
        '''
        self.proxy = proxy
        self.bootloader_address = bootloader_address

    def abort_boot_timeout(self):
        '''
        Prevent bootloader from automatically starting application code.
        '''
        self.proxy.i2c_write(self.bootloader_address, 0x00)

    def start_application(self):
        '''
        Explicitly start application code.
        '''
        self.proxy.i2c_write(self.bootloader_address, [0x01, 0x80])

    def read_bootloader_version(self):
        '''
        Read ``twiboot`` version string.
        '''
        self.proxy.i2c_write(self.bootloader_address, 0x01)
        return self.proxy.i2c_read(self.bootloader_address, 16).tostring()

    def read_chip_info(self):
        '''
        Returns
        -------
        dict
            Information about device, including, e.g., sizes of memory regions.
        '''
        self.proxy.i2c_write(self.bootloader_address, [0x02, 0x00, 0x00, 0x00])
        data = self.proxy.i2c_read(self.bootloader_address, 8)
        return {
            'signature': data[:3].tolist(),
            'page_size': data[3],
            'flash_size': struct.unpack('>H', data[4:6])[0],
            'eeprom_size': struct.unpack('>H', data[6:8])[0]
        }

    def read_flash(self, address, n_bytes):
        """
        Read one or more flash bytes.

        Parameters
        ----------
        address : int
            Address in flash memory to read from.
        n_bytes : int
            Number of bytes to read.
        """
        addrh = address >> 8 & 0xFF
        addrl = address & 0xFF
        self.proxy.i2c_write(self.bootloader_address, [0x02, 0x01, addrh,
                                                       addrl])
        return self.proxy.i2c_read(self.bootloader_address, n_bytes)

    def read_eeprom(self, address, n_bytes):
        """
        Read one or more eeprom bytes

        Parameters
        ----------
        address : int
            Address in EEPROM to read from.
        n_bytes : int
            Number of bytes to read.
        """
        addrh = address >> 8 & 0xFF
        addrl = address & 0xFF
        self.proxy.i2c_write(self.bootloader_address, [0x02, 0x02, addrh,
                                                       addrl])
        return self.proxy.i2c_read(self.bootloader_address, n_bytes)

    def write_flash(self, address, page):
        """
        Write one flash page (128bytes on atmega328p).

        .. note::
            Page size can be queried by through :meth:`read_chip_info`.

        Parameters
        ----------
        address : int
            Address in EEPROM to write to.
        page : list or numpy.array or str
            Data to write.

            .. warning::
                Length **MUST** be equal to page size.
        """
        addrh = address >> 8 & 0xFF
        addrl = address & 0xFF
        page = _data_as_list(page)
        self.proxy.i2c_write(self.bootloader_address, [0x02, 0x01, addrh,
                                                       addrl] + page)

    def write_eeprom(self, address, data):
        """
        Write one or more eeprom bytes.

        Parameters
        ----------
        address : int
            Address in EEPROM to write to.
        data : list or numpy.array or str
            Data to write.

        See also
        --------
        :func:`_data_as_list`
        """
        addrh = address >> 8 & 0xFF
        addrl = address & 0xFF
        data = _data_as_list(data)
        self.proxy.i2c_write(self.bootloader_address, [0x02, 0x02, addrh,
                                                       addrl] + data)
