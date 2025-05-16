'''
.. versionadded:: v0.33
'''
# coding: utf-8
import time
import struct
import logging

import numpy as np

from itertools import chain
from path_helpers import path
from typing import Union, List, Optional, Dict

from .intel_hex import parse_intel_hex
from .proxy import ProxyBase


def _data_as_list(data: Union[List, np.array]) -> List[bytes]:
    """
    Parameters
    ----------
    data : list or numpy.array or str
        Data bytes.

    Returns
    -------
    list
        List of integer byte values.
    """
    if isinstance(data, np.ndarray):
        data = data.tobytes()
    if isinstance(data, str):
        data = [ord(char) for char in data]
    return data


class TwiBootloader:
    def __init__(self, proxy: ProxyBase, bootloader_address: Optional[int] = 0x29):
        """
        Parameters
        ----------
        proxy : base_node_rpc.Proxy
        bootloader_address : int
            I2C address of switching board.
        """
        self.proxy = proxy
        self.bootloader_address = bootloader_address
        self.logger = logging.getLogger(__name__)

    def abort_boot_timeout(self) -> None:
        """
        Prevent bootloader from automatically starting application code.
        """
        self.proxy.i2c_write(self.bootloader_address, 0x00)

    def start_application(self) -> None:
        """
        Explicitly start application code.
        """
        self.proxy.i2c_write(self.bootloader_address, [0x01, 0x80])

    def read_bootloader_version(self) -> str:
        """
        Read ``twiboot`` version string.
        """
        self.proxy.i2c_write(self.bootloader_address, 0x01)
        return self.proxy.i2c_read(self.bootloader_address, 16).tobytes()

    def read_chip_info(self) -> Dict:
        """
        Returns
        -------
        dict
            Information about device, including, e.g., sizes of memory regions.
        """
        self.proxy.i2c_write(self.bootloader_address, [0x02, 0x00, 0x00, 0x00])
        data = self.proxy.i2c_read(self.bootloader_address, 8)
        return {
            'signature': data[:3].tolist(),
            'page_size': data[3],
            'flash_size': struct.unpack('>H', data[4:6])[0],
            'eeprom_size': struct.unpack('>H', data[6:8])[0]
        }

    def read_flash(self, address: int, n_bytes: int) -> bytes:
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
        self.proxy.i2c_write(self.bootloader_address, [0x02, 0x01, addrh, addrl])
        return self.proxy.i2c_read(self.bootloader_address, n_bytes)

    def read_eeprom(self, address: int, n_bytes: int) -> bytes:
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
        self.proxy.i2c_write(self.bootloader_address, [0x02, 0x02, addrh, addrl])
        return self.proxy.i2c_read(self.bootloader_address, n_bytes)

    def write_flash(self, address: int, page: Union[List, np.array, str]) -> None:
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
        self.proxy.i2c_write(self.bootloader_address, [0x02, 0x01, addrh, addrl] + page)

    def write_eeprom(self, address: int, data: Union[List, np.array, str]) -> None:
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
        self.proxy.i2c_write(self.bootloader_address, [0x02, 0x02, addrh, addrl] + data)

    def write_firmware(self, firmware_path: str, verify: Optional[bool] = True,
                       delay_s: Optional[float] = 0.02) -> None:
        """
        Write `Intel HEX file`__ and split into pages.

        __ Intel HEX file: https://en.wikipedia.org/wiki/Intel_HEX

        Parameters
        ----------
        firmware_path : str
            Path of Intel HEX file to read.
        verify : bool, optional
            If ``True``, verify each page after it is written.
        delay_s : float, optional
            Time to wait between each write/read operation.

            This delay allows for operation to complete before triggering I2C
            next call.

        Raises
        ------
        IOError
            If a flash page write fails after 10 retry attempts.

            Delay is increased exponentially between operations from one
            attempt to the next.

        Version log
        -----------
        .. versionchanged:: 0.34
            Prior to version 0.34, if a page write failed while writing
            firmware to flash memory, an exception was raised immediately.
            This approach is problematic, as it leaves the flash memory in a
            non-deterministic state which may prevent, for example, returning
            control to the bootloader.

            As of version 0.34, retry failed page writes up to 10 times,
            increasing the delay between operations exponentially from one
            attempt to the next.
        """
        chip_info = self.read_chip_info()

        pages = load_pages(firmware_path, chip_info['page_size'])

        # At most, wait 100x the specified nominal delay during retries of
        # failed page writes.
        max_delay = max(1., 100. * delay_s)
        # Retry failed page writes up to 10 times, increasing the delay between
        # operations exponentially from one attempt to the next.
        delay_durations = np.logspace(np.log(delay_s) / np.log(10), np.log(max_delay) / np.log(10),
                                      num=10, base=10)

        for i, page_i in enumerate(pages):
            # If `verify` is `True`, retry failed page writes up to 10 times.
            for delay_j in delay_durations:
                self.logger.info(f'Write page: {i + 1}/{len(pages)}')
                self.write_flash(i * chip_info['page_size'], page_i)
                # Delay to allow bootloader to finish writing to flash.
                time.sleep(delay_j)

                if not verify:
                    break
                self.logger.info(f'Verify page: {i + 1}/{len(pages)}')
                # Verify written page.
                verify_data_i = self.read_flash(i * chip_info['page_size'], chip_info['page_size'])
                # Delay to allow bootloader to finish processing flash read.
                time.sleep(delay_j)
                try:
                    if (verify_data_i == page_i).all():
                        # Data page has been verified successfully.
                        break
                except AttributeError:
                    # Data lengths do not match.
                    pass
            else:
                raise IOError('Page write failed to verify for **all** attempted delay durations.')


def load_pages(firmware_path: str, page_size: int) -> List:
    """
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
    """
    firmware_path = path(firmware_path)
    data = firmware_path.text()

    df_data = parse_intel_hex(data)
    data_bytes = list(chain(*df_data.loc[df_data.record_type == 0, 'data']))

    pages = [data_bytes[i:i + page_size] for i in range(0, len(data_bytes), page_size)]

    # Pad end of last page with 0xFF to fill full page size.
    pages[-1].extend([0xFF] * (page_size - len(pages[-1])))
    return pages
