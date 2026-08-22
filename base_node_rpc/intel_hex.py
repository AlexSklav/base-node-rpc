# coding: utf-8
import re

import pandas as pd

cre_hex_record = re.compile(r'^:'
                            r'(?P<byte_count>[0-9a-fA-F]{2})'
                            r'(?P<address>[0-9a-fA-F]{4})'
                            r'(?P<record_type>0[0-5])'
                            r'(?P<data>[0-9a-fA-F]+)?'
                            r'(?P<checksum>[0-9a-fA-F]{2})'
                            r'$')


def hex2ints(hex_str: str) -> list:
    return [int(hex_str[j:j + 2], 16) for j in range(0, len(hex_str), 2)]


def parse_intel_hex(data: str) -> pd.DataFrame:
    """
    Parse Intel HEX file contents.

    See also
    --------
    https://en.wikipedia.org/wiki/Intel_HEX#Record_types

    Parameters
    ----------
    data : str
        Intel HEX file contents.

    Returns
    -------
    pandas.DataFrame
        Parsed binary data as a table.
    """
    matches = []

    for i, line_i in enumerate(data.splitlines()):
        if not line_i.strip():
            # Skip blank/whitespace-only lines (e.g., trailing newline).
            continue

        match_i = cre_hex_record.match(line_i)
        if match_i is None:
            raise ValueError(f'Line {i + 1} is not a valid Intel HEX record: "{line_i}".')
        match_i = match_i.groupdict()

        checksum_i = ((sum(hex2ints(line_i[1:-2])) ^ 0xFF) + 1 & 0xFF)
        if not checksum_i == int(match_i['checksum'], 16):
            raise ValueError(f'Computed checksum ({hex(checksum_i)}) does not match expected '
                             f'checksum (0x{match_i["checksum"]}) for line: "{line_i}".')

        match_i['text'] = line_i
        if match_i['data']:
            match_i['data'] = hex2ints(match_i['data'])
        matches.append(match_i)

    df_data = pd.DataFrame(matches)
    # Assign whole columns (not `.loc[...] = ...`): pandas 3 infers these
    # columns as `str` dtype and refuses an in-place int write-back.
    int_columns = ['record_type', 'address', 'byte_count', 'checksum']
    df_data[int_columns] = df_data[int_columns].map(lambda x: int(x, 16))

    # XXX We don't currently support [record types 2-5][1].
    # XXX We only currently support **contiguous** data sections.
    #
    # [1]: https://en.wikipedia.org/wiki/Intel_HEX#Record_types

    # Verify all records are only of type 0 or 1.
    if not df_data.record_type.isin([0, 1]).all():
        unsupported = sorted(set(df_data.loc[~df_data.record_type.isin([0, 1]), 'record_type']))
        raise ValueError('Records appear to be outside of the supported types [0 or 1]: '
                         f'{unsupported}')

    # Verify there is exactly one 1 type record.
    n_eof = df_data.loc[df_data.record_type == 1].shape[0]
    if n_eof != 1:
        raise ValueError(f'Expected exactly one type 1 (end-of-file) record, found {n_eof}.')

    # Verify data is contiguous.
    if not (df_data.loc[df_data.record_type == 0, 'address'].diff()[1:].values
            == df_data.loc[df_data.record_type == 0, 'byte_count'].iloc[:-1]).all():
        raise ValueError('Data is not contiguous')

    return df_data
