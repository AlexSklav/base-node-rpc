from __future__ import absolute_import

import sys


if sys.version_info[0] < 3:
    from ._async_py27 import *
else:
    from ._async_py36 import *
