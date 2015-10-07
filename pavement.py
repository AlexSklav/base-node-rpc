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
PROPERTIES = OrderedDict([('name', PROJECT_PREFIX),
                          ('base_node_version', VERSION),
                          ('manufacturer', 'Wheeler Lab'),
                          ('software_version', VERSION),
                          ('url', URL)])
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
def generate_library_main_header():
    library_header = (base_node_rpc.get_lib_directory()
                    .joinpath('BaseNodeRpc', 'BaseNodeRpc.h'))
    library_header.write_bytes('''
#ifndef ___BASE_NODE_RPC__H___
#define ___BASE_NODE_RPC__H___

#ifndef BASE_NODE__BASE_NODE_SOFTWARE_VERSION
#define BASE_NODE__BASE_NODE_SOFTWARE_VERSION   "%s"
#endif
#include "BaseNode.h"

#endif  // #ifndef ___BASE_NODE_RPC__H___
    '''.strip() % options.PROPERTIES['software_version'])


@task
@needs('generate_library_main_header')
def build_arduino_library():
    import os
    import tarfile

    package_lib_dir = path('base_node_rpc').joinpath('lib')
    if not package_lib_dir.isdir():
        package_lib_dir.mkdir()
    tf = tarfile.TarFile.bz2open(package_lib_dir
                                 .joinpath('BaseNodeRpc-Arduino.tar.gz'), 'w')
    version_path = (base_node_rpc.get_lib_directory()
                    .joinpath('RELEASE-VERSION'))
    version_path.write_bytes(VERSION)
    for s in base_node_rpc.get_lib_directory().walkfiles():
        tf.add(s, os.path.join('BaseNodeRpc', os.path.basename(s)))
    tf.close()


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
@needs('build_arduino_library', 'generate_setup', 'minilib', 'build_firmware',
       'generate_python_code', 'setuptools.command.sdist')
def sdist():
    """Overrides sdist to make sure that our setup.py is generated."""
    pass
