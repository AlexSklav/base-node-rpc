from __future__ import absolute_import
import os
from collections import OrderedDict

from path_helpers import path

from ._version import get_versions
__version__ = get_versions()['version']
del get_versions

# .. versionadded:: 0.38
from .async import available_devices
# .. versionadded:: 0.39
from .async import read_device_id
try:
    from .node import Proxy, I2cProxy, SerialProxy
except (ImportError, TypeError):
    Proxy = None
    I2cProxy = None
    SerialProxy = None


def package_path():
    return path(os.path.dirname(__file__))


def get_sketch_directory():
    '''
    Return directory containing the Arduino sketch.
    '''
    return package_path().joinpath('..', 'src')


def get_lib_directory():
    return package_path().joinpath('..', 'lib').realpath()


def get_includes():
    '''
    Return directories containing the Arduino header files.

    Notes
    =====

    For example:

        import arduino_rpc
        ...
        print ' '.join(['-I%s' % i for i in arduino_rpc.get_includes()])
        ...

    '''
    import arduino_rpc

    return (list(get_lib_directory().walkdirs('src')) +
            arduino_rpc.get_includes())


def get_sources():
    '''
    Return Arduino source file paths.  This includes any supplementary source
    files that are not contained in Arduino libraries.
    '''
    import arduino_rpc

    return get_sketch_directory().files('*.c*') + arduino_rpc.get_sources()


def get_firmwares():
    '''
    Return compiled Arduino hex file paths.

    This function may be used to locate firmware binaries that are available
    for flashing to [Arduino][1] boards.

    [1]: http://arduino.cc
    '''
    return OrderedDict([(board_dir.name, [f.abspath() for f in
                                          board_dir.walkfiles('*.hex')])
                        for board_dir in
                        package_path().joinpath('firmware').dirs()])

from ._version import get_versions
__version__ = get_versions()['version']
del get_versions
