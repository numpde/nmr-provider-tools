"""Print this process's UID and GID in host coordinates."""

import os
import sys
from linux_idmap.process import IdentityError, observe_host_process

try:
    maps = observe_host_process(os.getpid())
    print(maps.uid_map.translate(os.getuid()), maps.gid_map.translate(os.getgid()))
except IdentityError as error:
    sys.exit(f"{error.category}: {error}")
