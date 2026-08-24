# coding: utf-8
"""
:mod:`base_node_rpc.bootloader_driver` -- ``twiboot`` I2C bootloader driver.

This driver writes firmware, so its defects brick boards.  The ones guarded
here are:

* ``read_chip_info()`` returning ``numpy`` scalars: ``page_size`` came back as
  a ``numpy.uint8``, which under NEP 50 makes ``i * page_size`` **wrap at 256**
  -- so page 2 onwards were written to the wrong flash addresses;
* ``_data_as_list()`` not accepting ``bytes``/``bytearray``/``ndarray``, whose
  results are concatenated with a plain ``list`` header in
  ``write_flash``/``write_eeprom``;
* verification comparing a short read against a full page (``numpy`` raises on
  mismatched lengths instead of simply retrying);
* ``load_pages()`` silently relocating an image whose base address is not zero;
* ``np.logspace`` producing NaNs for ``delay_s=0`` (``log(0)`` is ``-inf``),
  which makes ``time.sleep(nan)`` raise mid-flash.

Hardware-free: the I2C proxy is a mock backed by a ``bytearray``.  No serial
port, no network, no firmware.
"""
import struct

import numpy as np
import pytest

from base_node_rpc import bootloader_driver as bd

from .test_intel_hex import hex_record


class MockTwiProxy:
    """In-memory stand-in for a ``twiboot`` device behind ``i2c_write``/``i2c_read``."""

    def __init__(self, page_size=128, flash_size=32768, eeprom_size=1024,
                 version=b'TWIBOOT m328p'):
        self.page_size = page_size
        self.flash = bytearray([0xFF] * flash_size)
        self._chip_info = np.frombuffer(
            bytes([0x1E, 0x95, 0x0F, page_size]) +
            struct.pack('>HH', flash_size, eeprom_size), dtype=np.uint8)
        self._version = version.ljust(16, b'\x00')
        self._pending = None
        self.writes = []

    def i2c_write(self, address, data):
        if isinstance(data, int):
            data = [data]
        data = list(data)
        if data[:1] == [0x01]:
            self._pending = ('version', 0)
        elif data[:2] == [0x02, 0x00]:
            self._pending = ('chip_info', 0)
        elif data[:2] == [0x02, 0x01]:
            flash_address = (data[2] << 8) | data[3]
            if len(data) > 4:
                payload = data[4:]
                self.flash[flash_address:flash_address + len(payload)] = \
                    bytes(payload)
                self.writes.append((flash_address, len(payload)))
            else:
                self._pending = ('flash', flash_address)
        else:                                   # pragma: no cover - defensive
            raise AssertionError(f'unexpected i2c_write: {data!r}')

    def i2c_read(self, address, n_bytes):
        kind, flash_address = self._pending
        self._pending = None
        if kind == 'chip_info':
            return self._chip_info
        if kind == 'version':
            return np.frombuffer(self._version, dtype=np.uint8)
        return np.frombuffer(
            bytes(self.flash[flash_address:flash_address + n_bytes]),
            dtype=np.uint8)


def write_hex_image(tmp_path, n_bytes, base_address=0, name='firmware.hex'):
    """Write a contiguous Intel HEX image and return ``(path, payload)``."""
    payload = [(i * 7) & 0xFF for i in range(n_bytes)]
    lines = [hex_record(16, base_address + offset, 0, payload[offset:offset + 16])
             for offset in range(0, n_bytes, 16)]
    lines.append(hex_record(0, 0, 1, []))
    firmware_path = tmp_path / name
    firmware_path.write_text('\n'.join(lines) + '\n')
    return str(firmware_path), payload


# --------------------------------------------------------------------------
# `_data_as_list`
# --------------------------------------------------------------------------
@pytest.mark.parametrize('value,expected', [
    ([1, 2, 3], [1, 2, 3]),
    ((1, 2, 3), [1, 2, 3]),
    (b'\x01\x02\x03', [1, 2, 3]),
    (bytearray(b'\x01\x02\x03'), [1, 2, 3]),
    (np.array([1, 2, 3], dtype=np.uint8), [1, 2, 3]),
    ('abc', [97, 98, 99]),
])
def test_data_as_list_normalizes_every_accepted_shape(value, expected):
    """Every accepted payload shape becomes a plain ``list`` of ``int``.

    ``write_flash``/``write_eeprom`` concatenate the result with a plain
    ``list`` header (``[0x02, 0x01, addrh, addrl] + data``); an ``ndarray`` or
    ``bytes`` there raises, and a list of ``numpy`` scalars serializes wrong.
    """
    out = bd._data_as_list(value)
    assert out == expected
    assert isinstance(out, list)
    assert all(type(x) is int for x in out)
    # Must survive the concatenation performed by `write_flash`.
    assert isinstance([0x02, 0x01, 0, 0] + out, list)


# --------------------------------------------------------------------------
# `read_chip_info`
# --------------------------------------------------------------------------
def test_read_chip_info_returns_plain_python_ints():
    """``page_size`` and friends are ``int``, never ``numpy`` scalars.

    A ``numpy.uint8`` ``page_size`` wraps at 256 under NEP 50, so
    ``i * page_size`` produced *aliased* flash page addresses from page 2 on --
    silently overwriting earlier pages.
    """
    info = bd.TwiBootloader(MockTwiProxy()).read_chip_info()
    assert type(info['page_size']) is int
    assert type(info['flash_size']) is int
    assert type(info['eeprom_size']) is int
    assert all(type(x) is int for x in info['signature'])
    assert info == {'signature': [0x1E, 0x95, 0x0F], 'page_size': 128,
                    'flash_size': 32768, 'eeprom_size': 1024}


def test_page_addresses_stay_monotonic_past_256_bytes():
    """Page addresses increase monotonically across the whole flash.

    This is the arithmetic that the ``numpy.uint8`` ``page_size`` broke; it is
    asserted directly so the regression is caught without flashing anything.
    """
    page_size = bd.TwiBootloader(MockTwiProxy()).read_chip_info()['page_size']
    addresses = [i * page_size for i in range(40)]
    assert addresses == list(range(0, 40 * 128, 128))
    assert all(b > a for a, b in zip(addresses, addresses[1:]))


def test_read_bootloader_version_returns_a_stripped_str():
    """The NUL-padded 16-byte version string is decoded and trimmed."""
    version = bd.TwiBootloader(MockTwiProxy()).read_bootloader_version()
    assert isinstance(version, str)
    assert version == 'TWIBOOT m328p'


def test_read_bootloader_version_survives_non_utf8_padding():
    """A garbled version string is replaced, not raised on.

    ``read_bootloader_version()`` is often the *first* call made to a board in
    an unknown state; a :class:`UnicodeDecodeError` there is unhelpful.
    """
    proxy = MockTwiProxy(version=b'TWIBOOT\xff\xfe')
    version = bd.TwiBootloader(proxy).read_bootloader_version()
    assert isinstance(version, str)
    assert version.startswith('TWIBOOT')


# --------------------------------------------------------------------------
# `write_firmware`
# --------------------------------------------------------------------------
def test_write_firmware_writes_every_page_at_the_right_address(tmp_path):
    """A 40-page image lands byte-for-byte at monotonic page addresses."""
    firmware_path, payload = write_hex_image(tmp_path, 40 * 128)
    proxy = MockTwiProxy()

    bd.TwiBootloader(proxy).write_firmware(firmware_path, verify=True,
                                           delay_s=1e-6)

    assert [address for address, _ in proxy.writes] == list(
        range(0, 40 * 128, 128))
    assert list(proxy.flash[:40 * 128]) == payload


def test_write_firmware_retries_a_short_verify_read(tmp_path):
    """A transient short read is retried, not raised on.

    ``numpy`` raises when comparing mismatched lengths, so an unguarded
    ``verify_data == page`` turned a recoverable I2C hiccup into an exception
    that aborted the flash mid-image.
    """
    firmware_path, payload = write_hex_image(tmp_path, 8 * 128)

    class ShortReadProxy(MockTwiProxy):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.short_reads = 0

        def i2c_read(self, address, n_bytes):
            if (self._pending is not None and self._pending[0] == 'flash'
                    and self.short_reads < 1):
                self.short_reads += 1
                n_bytes //= 2
            return super().i2c_read(address, n_bytes)

    proxy = ShortReadProxy()
    bd.TwiBootloader(proxy).write_firmware(firmware_path, verify=True,
                                           delay_s=1e-6)

    assert proxy.short_reads == 1, 'the short read was never injected'
    assert list(proxy.flash[:8 * 128]) == payload


def test_write_firmware_raises_io_error_when_verification_never_succeeds(
        tmp_path):
    """A page that never verifies raises :class:`IOError` after the retries.

    It must not loop forever, and it must not fail silently and leave the board
    half-flashed without saying so.
    """
    firmware_path, _ = write_hex_image(tmp_path, 2 * 128)

    class NeverVerifiesProxy(MockTwiProxy):
        def i2c_read(self, address, n_bytes):
            is_flash_read = (self._pending is not None and
                             self._pending[0] == 'flash')
            data = super().i2c_read(address, n_bytes)
            # Corrupt only the *verify* reads, so `read_chip_info()` still
            # reports a usable page size.
            return np.zeros_like(data) if is_flash_read else data

    with pytest.raises(IOError, match='failed to verify'):
        bd.TwiBootloader(NeverVerifiesProxy()).write_firmware(
            firmware_path, verify=True, delay_s=1e-6)


@pytest.mark.parametrize('delay_s', [0, 1e-6, 0.005, 1])
def test_retry_delay_schedule_contains_no_nan(delay_s):
    """The exponential retry schedule is finite for every ``delay_s``.

    ``np.log(0)`` is ``-inf``, which turns the whole ``logspace`` schedule into
    NaNs -- and ``time.sleep(nan)`` raises, aborting the flash.  The floor
    clamp is what keeps ``delay_s=0`` usable.
    """
    max_delay = max(1., 100. * delay_s)
    min_delay = max(delay_s, 1e-6)
    schedule = np.logspace(np.log(min_delay) / np.log(10),
                           np.log(max_delay) / np.log(10), num=10, base=10)
    assert not np.isnan(schedule).any()
    assert np.isfinite(schedule).all()
    assert (schedule >= 0).all()
    assert schedule[-1] == pytest.approx(max_delay)


def test_write_firmware_with_delay_zero_completes(tmp_path):
    """``delay_s=0`` flashes rather than raising ``sleep(nan)``."""
    firmware_path, payload = write_hex_image(tmp_path, 2 * 128)
    proxy = MockTwiProxy()
    bd.TwiBootloader(proxy).write_firmware(firmware_path, verify=True,
                                           delay_s=0)
    assert list(proxy.flash[:2 * 128]) == payload


# --------------------------------------------------------------------------
# `load_pages`
# --------------------------------------------------------------------------
def test_load_pages_splits_on_page_boundaries(tmp_path):
    """Pages are exactly ``page_size`` bytes, in address order."""
    firmware_path, payload = write_hex_image(tmp_path, 5 * 128)
    pages = bd.load_pages(firmware_path, 128)
    assert len(pages) == 5
    assert all(len(page) == 128 for page in pages)
    assert [byte for page in pages for byte in page] == payload


def test_load_pages_refuses_a_non_zero_base_address(tmp_path):
    """An image that does not start at address 0 is refused.

    Relocating it silently to 0 would write the firmware over the bootloader's
    own vector table.
    """
    firmware_path, _ = write_hex_image(tmp_path, 2 * 128, base_address=0x1000)
    with pytest.raises(ValueError):
        bd.load_pages(firmware_path, 128)
