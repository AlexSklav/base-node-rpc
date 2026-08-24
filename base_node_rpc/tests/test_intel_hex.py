# coding: utf-8
"""
:func:`base_node_rpc.intel_hex.parse_intel_hex`.

The parser feeds firmware bytes to a bootloader, so every *rejection* it makes
is a safety property: a record it silently mis-parses is a brick.  It also had
to survive the pandas 3 change that infers string columns as ``str`` dtype and
refuses an in-place integer write-back (the int columns are now assigned whole
rather than through ``.loc[...]``).

Hardware-free: pure string parsing.
"""
import pandas as pd
import pytest

from base_node_rpc.intel_hex import hex2ints, parse_intel_hex


def hex_record(byte_count, address, record_type, data_bytes):
    """Build a well-formed Intel HEX record with a correct checksum."""
    body = ([byte_count, (address >> 8) & 0xFF, address & 0xFF, record_type] +
            list(data_bytes))
    checksum = ((sum(body) ^ 0xFF) + 1) & 0xFF
    return ':' + ''.join('%02X' % b for b in body + [checksum])


def good_records(n_data=3, byte_count=16):
    records = [hex_record(byte_count, byte_count * i, 0,
                          [(i * byte_count + j) & 0xFF
                           for j in range(byte_count)])
               for i in range(n_data)]
    records.append(hex_record(0, 0, 1, []))
    return records


@pytest.fixture
def good_hex():
    return '\n'.join(good_records()) + '\n'


def test_parses_a_well_formed_image(good_hex):
    """A well-formed image parses into typed, ordered records."""
    df_data = parse_intel_hex(good_hex)
    assert df_data.record_type.tolist() == [0, 0, 0, 1]
    assert df_data.address.tolist() == [0, 16, 32, 0]
    assert df_data.byte_count.tolist() == [16, 16, 16, 0]
    assert df_data.data.iloc[0][:4] == [0, 1, 2, 3]


@pytest.mark.parametrize('column', ['record_type', 'address', 'byte_count',
                                    'checksum'])
def test_numeric_columns_are_integers(good_hex, column):
    """The hex-string columns really are converted to integers.

    pandas 3 infers these columns as ``str`` dtype and refuses an in-place
    ``.loc[...]`` integer write-back, so they are assigned as whole columns.
    A column left as strings would make every later comparison
    (``record_type == 0``, address arithmetic) silently wrong.
    """
    df_data = parse_intel_hex(good_hex)
    assert pd.api.types.is_integer_dtype(df_data[column]), df_data[column].dtype
    assert all(isinstance(value, (int,)) or hasattr(value, '__index__')
               for value in df_data[column])


@pytest.mark.parametrize('suffix', ['', '\n', '\n\n', '\n   \n', '\n\t\n\n'])
def test_blank_and_whitespace_only_lines_are_skipped(good_hex, suffix):
    """Trailing blank lines do not become bogus records.

    Practically every real ``.hex`` file ends with a newline; treating that as
    a record made the parser reject valid firmware.
    """
    reference = parse_intel_hex(good_hex)
    assert parse_intel_hex(good_hex + suffix).shape == reference.shape


def test_malformed_line_names_the_line_number(good_hex):
    """A line that is not a record raises :class:`ValueError` naming the line."""
    records = good_records()
    bad = good_hex.replace(records[1], 'GARBAGE-NOT-A-RECORD')
    with pytest.raises(ValueError, match=r'Line 2 is not a valid Intel HEX'):
        parse_intel_hex(bad)


def test_bad_checksum_is_rejected(good_hex):
    """A record whose checksum does not match is rejected, not flashed."""
    records = good_records()
    corrupted = good_hex.replace(records[0], records[0][:-2] + 'FF')
    with pytest.raises(ValueError, match='checksum'):
        parse_intel_hex(corrupted)


def test_unsupported_record_type_raises_value_error():
    """An unsupported record type raises rather than tripping an ``assert``.

    ``assert`` statements are stripped under ``python -O``, which would let an
    extended-address record through and silently relocate the image.
    """
    records = ([hex_record(2, 0, 4, [0x08, 0x00])] + good_records())
    with pytest.raises(ValueError, match='supported types'):
        parse_intel_hex('\n'.join(records) + '\n')


def test_two_end_of_file_records_are_rejected(good_hex):
    """Exactly one end-of-file record is required."""
    with pytest.raises(ValueError, match='end-of-file'):
        parse_intel_hex(good_hex + hex_record(0, 0, 1, []) + '\n')


def test_missing_end_of_file_record_is_rejected():
    """A truncated image (no EOF record) is rejected."""
    records = good_records()[:-1]
    with pytest.raises(ValueError, match='end-of-file'):
        parse_intel_hex('\n'.join(records) + '\n')


def test_non_contiguous_data_is_rejected():
    """A gap between data records is rejected.

    Only contiguous images are supported; flashing a sparse image as if it were
    contiguous writes the wrong bytes to the wrong addresses.
    """
    records = good_records()
    non_contiguous = '\n'.join([records[0],
                                hex_record(16, 0x100, 0, list(range(16))),
                                records[-1]]) + '\n'
    with pytest.raises(ValueError, match='contiguous'):
        parse_intel_hex(non_contiguous)


def test_hex2ints_round_trips():
    """``hex2ints`` decodes byte pairs, not nibbles."""
    assert hex2ints('00FF10') == [0x00, 0xFF, 0x10]
    assert hex2ints('') == []
