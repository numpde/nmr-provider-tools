"""Exercise delivery's generated lock with the released shared publisher offline."""

import hashlib
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

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

# Exercise a genuinely missing synthetic wheel without downloading a second
# copy of any real dependency from the shared pool.
content = b"synthetic delivery publisher test"
checksum = hashlib.sha256(content).hexdigest()
requests = []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        requests.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *_args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = Thread(target=server.serve_forever)
thread.start()
try:
    with tempfile.TemporaryDirectory() as temporary:
        test_pool = Path(temporary)
        test_name = "delivery_fixture-1.0-py3-none-any.whl"
        test_url = f"http://127.0.0.1:{server.server_port}/{test_name}"
        publish_release_wheels(pool / tool["name"], test_pool, [(test_name, checksum, test_url)])
        publish_release_wheels(pool / tool["name"], test_pool, [(test_name, checksum, test_url)])
        if (test_pool / test_name).read_bytes() != content or requests != [f"/{test_name}"]:
            raise AssertionError("The publisher did not acquire once and reuse the locked wheel.")
finally:
    server.shutdown()
    thread.join()
    server.server_close()
