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
    DEFAULT_ARDUINO_BOARDS=DEFAULT_ARDUINO_BOARDS,
    setup=dict(name=package_name,
               version=VERSION,
               description='Arduino RPC node packaged as Python package.',
               author='Christian Fobel',
               author_email='christian@fobel.net',
               url=URL,
               license='GPLv2',
               install_requires=['arduino_scons>=0.1.post8',
                                 'arduino-rpc>=1.6.post21',
                                 'protobuf>=2.6.1'],
               # Install data listed in `MANIFEST.in`
               include_package_data=True,
               packages=[str(PROJECT_PREFIX)]))


@task
@cmdopts(LIB_CMDOPTS, share_with=LIB_GENERATE_TASKS)
def generate_arduino_library_properties(options):
    import jinja2
    import arduino_rpc

    template = jinja2.Template(open(arduino_rpc.get_library_directory()
                                    .joinpath('library.properties.t'),
                                    'rb').read())
    library_dir = verify_library_directory(options)
    library_properties = library_dir.joinpath('library.properties')
    with library_properties.open('wb') as output:
        output.write(template.render(LIB_PROPERTIES))


@task
@cmdopts(LIB_CMDOPTS, share_with=LIB_GENERATE_TASKS)
def generate_library_main_header(options):
    library_dir = verify_library_directory(options)
    library_header = library_dir.joinpath('src', 'BaseNodeRpc.h')
    print library_header
    with library_header.open('wb') as output:
        output.write('''
#ifndef ___BASE_NODE_RPC__H___
#define ___BASE_NODE_RPC__H___

#ifndef BASE_NODE__BASE_NODE_SOFTWARE_VERSION
#define BASE_NODE__BASE_NODE_SOFTWARE_VERSION   "%s"
#endif
#include "BaseNode.h"

#endif  // #ifndef ___BASE_NODE_RPC__H___
    '''.strip() % options.PROPERTIES['software_version'])


@task
@needs('generate_all_code', 'generate_arduino_library_properties')
def build_arduino_library():
    import zipfile
    from arduino_rpc import get_library_directory

    library_dir = verify_library_directory(options)
    zf = zipfile.ZipFile(library_dir + '.zip', mode='w')

    for f in library_dir.walkfiles():
        zf.write(f, arcname=library_dir.relpathto(f))
    zf.close()


@task
@needs('generate_setup', 'minilib', 'generate_library_main_header',
       'generate_config_c_code', 'generate_config_python_code',
       'generate_command_processor_header', 'generate_rpc_buffer_header',
       'generate_python_code')
@cmdopts(LIB_CMDOPTS, share_with=LIB_GENERATE_TASKS)
def generate_all_code(options):
    '''
    Generate all C++ (device) and Python (host) code, but do not compile
    device sketch.
    '''
    pass


@task
@needs('generate_library_main_header', 'generate_config_c_code',
       'generate_config_python_code', 'generate_command_processor_header',
       'generate_rpc_buffer_header',
       'base_node_rpc.pavement_base.build_firmware')
def build_firmware():
    '''
    Override `pavement_base.build_firmware` to generate header with version
    number.
    '''
    pass


@task
@needs('base_node_rpc.pavement_base.generate_all_code')
@cmdopts(LIB_CMDOPTS, share_with=LIB_GENERATE_TASKS)
def generate_all_code(options):
    '''
    Generate all C++ (device) and Python (host) code, but do not compile
    device sketch.
    '''
    pass


@task
@needs('build_arduino_library', 'generate_setup', 'minilib', 'build_firmware',
       'generate_python_code', 'setuptools.command.sdist')
def sdist():
    """Overrides sdist to make sure that our setup.py is generated."""
    pass
