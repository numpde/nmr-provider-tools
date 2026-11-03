"""Exercise delivery's generated lock with the released shared publisher offline."""

from pathlib import Path

from tools.delivery_image import publish_release_wheels, release_inputs
from tools.provider_core_acquire import tool_present
from tools.wheelhouse import read_json


pool = Path("/wheelhouse")
release, _ = release_inputs(Path("/release.json"), Path("/core-runtime.lock"))
tool = read_json(Path("/wheelhouse-tool-release.json"))
if not tool_present(pool, tool):
    raise FileNotFoundError("The pinned wheelhouse tool is missing from the pool.")
name = f"provider_core-{release['version']}-py3-none-any.whl"
url = f"https://github.com/numpde/nmr-provider-tools/releases/download/v{release['version']}/{name}"
publish_release_wheels(pool / tool["name"], pool, [(name, release["wheel_sha256"], url)])
