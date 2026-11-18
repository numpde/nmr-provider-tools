"""Construct the installed proxy without a provider source or scientific import.

Ports nmrnet-repro d09b77b containers/delivery/check_runtime.py. This constructor
proof opens only a private loopback Unix listener; no DNS or API request runs.
"""

from provider_core.container_guard import require_container

require_container()

from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory

from provider_core.provider_proxy import ServicePolicy, ServiceProxyServer

for module in ("tools", "nmrnet_provider", "nmrnet_inference", "magnet_provider", "rdkit", "torch"):
    if find_spec(module) is not None:
        raise SystemExit(f"Cannot qualify delivery image: unexpected provider or build module {module} is importable.")

with TemporaryDirectory() as temporary:
    root = Path(temporary)
    policy = ServicePolicy.admit("external", ["api.example:443"], [])
    with ServiceProxyServer(root / "proxy.sock", "lo", policy, status_directory=root):
        pass
