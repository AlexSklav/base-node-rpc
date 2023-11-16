# coding: utf-8
from typing import Dict, List

from path_helpers import path

from . import proxy, bootloader_driver, intel_hex, protobuf
from .ser_async import available_devices, read_device_id

try:
    from .node import Proxy, I2cProxy, SerialProxy
except (ImportError, TypeError):
    Proxy = None
    I2cProxy = None
    SerialProxy = None

from ._version import get_versions

__version__ = get_versions()['version']
del get_versions


def package_path() -> path:
    return path(__file__).parent


def get_sketch_directory() -> path:
    """
    Return directory containing the Arduino sketch.
    """
    return package_path().joinpath('..', 'src').realpath()


def get_lib_directory() -> path:
    return package_path().joinpath('..', 'lib').realpath()


def get_includes() -> List[path]:
    """
    Return directories containing the Arduino header files.

    Notes
    =====

    For example:

        import arduino_rpc
        ...
        print ' '.join(['-I%s' % i for i in arduino_rpc.get_includes()])
        ...

    """
    import arduino_rpc

    return list(get_lib_directory().walkdirs('src')) + arduino_rpc.get_includes()


def get_sources() -> List[path]:
    """
    Return Arduino source file paths.  This includes any supplementary source
    files that are not contained in Arduino libraries.
    """
    import arduino_rpc

    return get_sketch_directory().files('*.c*') + arduino_rpc.get_sources()


def get_firmwares() -> Dict:
    """
    Return compiled Arduino hex file paths.

    This function may be used to locate firmware binaries that are available
    for flashing to [Arduino][1] boards.

    [1]: http://arduino.cc
    """
    return {board_dir.name: [f.abspath() for f in board_dir.walkfiles('*.hex')]
            for board_dir in package_path().joinpath('firmware').dirs()}
