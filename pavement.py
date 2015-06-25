from collections import OrderedDict
import sys

from paver.setuputils import setup, find_package_data, install_distutils_tasks
from paver.easy import options, path, environment

sys.path.insert(0, '.')
from base_node_rpc.pavement_base import *
import version

install_distutils_tasks()

DEFAULT_ARDUINO_BOARDS = ['uno', 'mega2560']
PROJECT_PREFIX = [d for d in path('.').dirs()
                  if d.joinpath('Arduino').isdir()][0].name
VERSION = version.getVersion()
URL='http://github.com/wheeler-microfluidics/%s.git' % PROJECT_PREFIX
PROPERTIES = OrderedDict([('name', PROJECT_PREFIX),
                          ('software_version', VERSION),
                          ('url', URL)])
package_files = find_package_data(package=PROJECT_PREFIX, where=PROJECT_PREFIX,
                                  only_in_packages=False)

options(
    PROPERTIES=PROPERTIES,
    DEFAULT_ARDUINO_BOARDS=DEFAULT_ARDUINO_BOARDS,
    setup=dict(name='wheeler.' + PROJECT_PREFIX,
               version=VERSION,
               description='Arduino RPC node packaged as Python package.',
               author='Christian Fobel',
               author_email='christian@fobel.net',
               url=URL,
               license='GPLv2',
               install_requires=['arduino_scons', 'nadamq', 'path_helpers',
                                 'arduino_helpers',
                                 'wheeler.arduino_rpc>=1.2'],
               packages=[PROJECT_PREFIX], package_data=package_files))
