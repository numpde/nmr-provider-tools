"""Print this process's reported UID and GID map rows."""

import os
from linux_idmap import observe_process

maps = observe_process(os.getpid())
for name, rows in (("uid", maps.uid_rows), ("gid", maps.gid_rows)):
    for row in rows:
        print(name, row.inside, row.outside, row.length)
