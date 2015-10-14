from collections import OrderedDict
import sys
from importlib import import_module

from paver.setuputils import setup, install_distutils_tasks
from paver.easy import options, path, environment

sys.path.insert(0, '.')
from base_node_rpc.pavement_base import *
import version

install_distutils_tasks()

DEFAULT_ARDUINO_BOARDS = ['uno', 'mega2560']
PROJECT_PREFIX = [d for d in path('.').dirs()
                  if d.joinpath('Arduino').isdir()][0].name
rpc_module = import_module(PROJECT_PREFIX)
VERSION = version.getVersion()
URL='http://github.com/wheeler-microfluidics/%s.git' % PROJECT_PREFIX
package_name = PROJECT_PREFIX.replace('_', '-')
PROPERTIES = OrderedDict([('package_name', package_name),
                          ('base_node_version', VERSION),
                          ('manufacturer', 'Wheeler Lab'),
                          ('software_version', VERSION),
                          ('url', URL)])
LIB_PROPERTIES = PROPERTIES.copy()
LIB_PROPERTIES.update(OrderedDict([('author', 'Christian Fobel'),
                                   ('author_email', 'christian@fobel.net'),
                                   ('short_description', 'Base classes for '
                                    'Arduino RPC node/device.'),
                                   ('version', VERSION),
                                   ('long_description',
'Provides: 1) A memory-efficient set of base classes providing an API to most '
'of the Arduino API, including EEPROM access, raw I2C '
'master-write/slave-request, etc., and 2) Support for processing RPC command '
'requests through either serial or I2C interface.  Utilizes Python (host) and '
'C++ (device) code generation from the `arduino_rpc` '
'(http://github.com/wheeler-microfluidics/arduino_rpc.git) package.'),
                                   ('category', 'Communication'),
                                   ('architectures', 'avr')]))
package_name = PROJECT_PREFIX.replace('_', '-')
print 'package_name', package_name

options(
    rpc_module=rpc_module,
    PROPERTIES=PROPERTIES,
    LIB_PROPERTIES=LIB_PROPERTIES,
    DEFAULT_ARDUINO_BOARDS=DEFAULT_ARDUINO_BOARDS,
    setup=dict(name=package_name,
               version=VERSION,
               description='Arduino RPC node packaged as Python package.',
               author='Christian Fobel',
               author_email='christian@fobel.net',
               url=URL,
               license='GPLv2',
               install_requires=['arduino_scons>=0.1.post10',
                                 'arduino-rpc>=1.7.post9', 'protobuf>=2.6.1'],
               # Install data listed in `MANIFEST.in`
               include_package_data=True,
               packages=[str(PROJECT_PREFIX)]))
