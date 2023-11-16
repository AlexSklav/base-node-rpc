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
        match_i = cre_hex_record.match(line_i).groupdict()

        checksum_i = ((sum(hex2ints(line_i[1:-2])) ^ 0xFF) + 1 & 0xFF)
        if not checksum_i == int(match_i['checksum'], 16):
            raise ValueError(f'Computed checksum ({hex(checksum_i)}) does not match expected '
                             f'checksum (0x{match_i["checksum"]}) for line: "{line_i}".')

        match_i['text'] = line_i
        if match_i['data']:
            match_i['data'] = hex2ints(match_i['data'])
        matches.append(match_i)

    df_data = pd.DataFrame(matches)
    df_data.loc[:, ['record_type', 'address', 'byte_count', 'checksum']] = \
        df_data[['record_type', 'address', 'byte_count', 'checksum']].applymap(lambda x: int(x, 16))

    # XXX We don't currently support [record types 2-5][1].
    # XXX We only currently support **contiguous** data sections.
    #
    # [1]: https://en.wikipedia.org/wiki/Intel_HEX#Record_types

    # Verify all records are only of type 0 or 1.
    assert df_data.record_type.isin([0, 1]).all(), 'Records appear to be outside of the supported types [0 or 1]'

    # Verify there is exactly one 1 type record.
    assert df_data.loc[df_data.record_type == 1].shape[0] == 1, 'More than one type 1 records found'

    # Verify data is contiguous.
    assert (df_data.loc[df_data.record_type == 0, 'address'].diff()[1:].values
            == df_data.loc[df_data.record_type == 0, 'byte_count'].iloc[:-1]).all(), 'Data is not contiguous'

    return df_data
