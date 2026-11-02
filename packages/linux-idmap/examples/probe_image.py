"""Print the host UID and GID of a temporary process in a local image."""

import json
import sys
from linux_idmap import Identifier
from linux_idmap.probe import ProbeFailure, ProbeRequest, StepError, probe_identity

if len(sys.argv) != 6:
    sys.exit("usage: probe_image.py ENDPOINT IMAGE PYTHON_PATH UID GID")

endpoint, image, interpreter, uid, gid = sys.argv[1:]
try:
    request = ProbeRequest(
        endpoint=endpoint,
        image=image,
        interpreter=interpreter,
        user_uid=Identifier.of(uid),
        user_gid=Identifier.of(gid),
        confine=True,
    )
    result = probe_identity(request)
    print(
        result.uid_map.translate(request.user_uid),
        result.gid_map.translate(request.user_gid),
    )
except ProbeFailure as error:
    sys.exit(json.dumps(error.as_dict()))
except (StepError, ValueError) as error:
    sys.exit(str(error))
