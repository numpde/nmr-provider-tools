"""Verify the captured delivery role with the independently pinned Wheelhouse."""

from tools.container_guard import require_container

require_container()

import pathlib

from operations.delivery.build_inputs import read_json, wheelhouse
from sdk_build.provider_core_acquire import tool_present


pool = pathlib.Path("/wheelhouse")
pin = read_json(pathlib.Path("/wheelhouse-tool-release.json"))

if not tool_present(pool, pin):
    raise FileNotFoundError(f"Cannot verify delivery inputs: acquire pinned Wheelhouse {pin['name'] = !r} first.")

wheelhouse(pool / pin["name"], "check", pathlib.Path("/delivery.wheelhouse.json"), pool)
