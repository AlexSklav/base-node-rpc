'''
.. versionadded:: v0.33
'''
import itertools as it
import struct
import time
import types

import numpy as np
import path_helpers as ph

from .intel_hex import parse_intel_hex


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

    def write_firmware(self, firmware_path):
        '''
        Write `Intel HEX file`__ and split into pages.

        __ Intel HEX file: https://en.wikipedia.org/wiki/Intel_HEX

        Parameters
        ----------
        firmware_path : str
            Path of Intel HEX file to read.
        page_size : int
            Size of each page.
        '''
        chip_info = self.read_chip_info()

        pages = load_pages(firmware_path, chip_info['page_size'])

        for i, page_i in enumerate(pages):
            print 'Write page: %4d/%d     \r' % (i + 1, len(pages)),
            self.write_flash(i * chip_info['page_size'], page_i)
            # Delay to allow bootloader to finish writing to flash.
            time.sleep(.01)

            print 'Verify page: %4d/%d    \r' % (i + 1, len(pages)),
            # Verify written page.
            verify_data_i = self.read_flash(i * chip_info['page_size'],
                                            chip_info['page_size'])
            assert((verify_data_i == page_i).all())
            # Delay to allow bootloader to finish processing flash read.
            time.sleep(.01)


def load_pages(firmware_path, page_size):
    '''
    Load `Intel HEX file`__ and split into pages.

    __ Intel HEX file: https://en.wikipedia.org/wiki/Intel_HEX

    Parameters
    ----------
    firmware_path : str
        Path of Intel HEX file to read.
    page_size : int
        Size of each page.

    Returns
    -------
    list
        List of page contents, where each page is represented as a list of
        integer byte values.
    '''
    firmware_path = ph.path(firmware_path)
    with firmware_path.open('r') as input_:
        data = input_.read()

    df_data = parse_intel_hex(data)
    data_bytes = list(it.chain(*df_data.loc[df_data.record_type == 0, 'data']))

    pages = [data_bytes[i:i + page_size]
             for i in xrange(0, len(data_bytes), page_size)]

    # Pad end of last page with 0xFF to fill full page size.
    pages[-1].extend([0xFF] * (page_size - len(pages[-1])))
    return pages
