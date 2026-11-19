"""Project and decode the Docker inspect results used by image retirement."""

import json
from typing import Any


# repo-check: allow-module-constant
IMAGE_INSPECTION_FORMAT = (
    '{"Id":{{json .Id}},"Labels":{{json .Config.Labels}},'
    '"RepoTags":{{json .RepoTags}},"RepoDigests":{{json .RepoDigests}}}'
)

# repo-check: allow-module-constant
CONTAINER_IMAGE_FORMAT = "{{json .Image}}"


def decode_inspection_lines(raw: str) -> list[Any]:
    """Decode the JSON value emitted for each object in the inspect request."""

    return [json.loads(line) for line in raw.splitlines()]
