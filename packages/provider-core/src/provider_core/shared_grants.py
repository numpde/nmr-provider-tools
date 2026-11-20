"""Project the shared operator settings into provider socket grants."""

from provider_core.container_guard import require_container

require_container()

import argparse
import json
import re
import tomllib
import pathlib
from dataclasses import dataclass

from provider_core.delivery_client import DeliveryEndpoint
from provider_core.startup_file import read_startup_file
from provider_core.type_requirements import require_type


@dataclass(frozen=True, slots=True)
class SharedGrants:
    project: str
    gid: int

    @property
    def proxy_volume(self):
        return self.project + "-proxy-socket"

    @property
    def outbox_volume(self):
        return self.project + "-outbox-socket"

    @property
    def proxy_socket(self):
        return pathlib.Path("/run/nmr-api-proxy/proxy.sock")

    @property
    def outbox_socket(self):
        return pathlib.Path("/run/nmr-api-outbox/http.sock")

    def endpoint(self, origin, provider_ref):
        return DeliveryEndpoint(origin, provider_ref, self.outbox_socket, socket_group_id=self.gid)

    def compose(self):
        return {
            "group_add": [str(self.gid)],
            "volumes": {
                self.proxy_volume: {"external": True, "name": self.proxy_volume},
                self.outbox_volume: {"external": True, "name": self.outbox_volume},
            },
            "mounts": [
                {
                    "type": "volume",
                    "source": self.proxy_volume,
                    "target": str(self.proxy_socket.parent),
                    "read_only": True,
                    "volume": {"nocopy": True}
                },
                {
                    "type": "volume",
                    "source": self.outbox_volume,
                    "target": str(self.outbox_socket.parent),
                    "read_only": True,
                    "volume": {"nocopy": True}
                },
            ],
            "proxy_socket": str(self.proxy_socket),
            "outbox_socket": str(self.outbox_socket),
            "socket_group_id": self.gid,
        }


def shared_grants(path):
    try:
        operator_settings = tomllib.loads(read_startup_file(path, maximum_bytes=65536).decode("utf-8"))
        (project, gid) = (operator_settings["deployment"]["project"], operator_settings["deployment"]["gid"])

        require_type(project, str)

        if not (re.fullmatch((expected_project_pattern := r"[a-z0-9][a-z0-9_-]{0,48}"), project) is not None):
            raise ValueError(f"{project=} should match {expected_project_pattern=}")

        require_type(gid, int)

        (min_gid, max_gid) = (1, 4294967294)

        if not (min_gid <= gid <= max_gid):
            raise ValueError(f"gid should be between {min_gid=} and {max_gid=}")
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as cause:
        raise ValueError(f"Cannot read shared socket grants from {path=}") from cause

    return SharedGrants(project, gid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--config", type=pathlib.Path, required=True)
    options = parser.parse_args()

    print(json.dumps(shared_grants(options.config).compose(), sort_keys=True))


if __name__ == "__main__":
    main()
