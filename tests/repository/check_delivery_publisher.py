"""Verify the pinned delivery inputs offline through pip."""

from tools.container_guard import require_container

require_container()

import pathlib
from operations.delivery.build_inputs import check_release

check_release(
    pathlib.Path("/delivery-release.json"),
    pathlib.Path("/delivery-runtime.lock"),
    pathlib.Path("/wheelhouse"),
)

print("complete")
