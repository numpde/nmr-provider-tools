"""Disposable explicit delivery configuration, with no real endpoint or key."""

import json
import os
from pathlib import Path

from tests.fakes.credential import document, encoded


class DeliveryConfigFiles:
    def __init__(self, root):
        self.root = Path(root)
        self.key = self.root / "signing.private.json"
        self.key.write_bytes(encoded(document()))
        self.key.chmod(0o600)
        self.path = self.root / "delivery.toml"
        self.raw = f'''schema_id = "provider-core.delivery.v1"
state_directory = {json.dumps(str(self.root / "state"))}
request_seconds = 1
retry_initial_seconds = 0.01
retry_max_seconds = 0.1
[limits]
provider_items = 4
provider_bytes = 16384
total_items = 8
total_bytes = 32768
[[bindings]]
socket_path = {json.dumps(str(self.root / "ipc/http.sock"))}
socket_group_id = {os.getgid()}
proxy_socket = {json.dumps(str(self.root / "api/http.sock"))}
[bindings.api]
origin = "https://api.example"
profile = "dev-local"
topology = "dev-local"
provider_ref = "provider:example"
credential_file = {json.dumps(str(self.key))}
exchange_seconds = 1
retirement_seconds = 0.2
'''.encode()
        self.write(self.raw)

    def write(self, raw):
        self.path.write_bytes(raw)
        self.path.chmod(0o600)
