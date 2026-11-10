#!/usr/bin/env bash
# Exercise the cold identity adapter against its exact released dependency.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/host/tools/docker_context.sh"
select_docker_context
source "$root/host/tools/base_image.sh"
source "$root/host/tools/wheelhouse_path.sh"
select_wheelhouse
require_wheelhouse_pool
dependency=$(python3 -I -S -B - "$root/requirements/acquisition-bootstrap.pylock.toml" "$wheelhouse_directory" <<'PY'
import hashlib
import pathlib
import sys
import tomllib
import urllib.parse

pin = tomllib.loads(pathlib.Path(sys.argv[1]).read_text())["packages"][0]["wheels"][0]
dependency = pathlib.Path(sys.argv[2]) / pathlib.PurePosixPath(urllib.parse.urlsplit(pin["url"]).path).name
try:
    wheel_bytes = dependency.read_bytes()
except FileNotFoundError as error:
    raise SystemExit(
        f"Cannot test acquisition identity: pinned wheel {dependency.name} is missing from WHEELHOUSE. "
        "Run make packages/provider-core/download with explicit DOCKER_CONTEXT, WHEELHOUSE and NETWORK_INTERFACE."
    ) from error
if hashlib.sha256(wheel_bytes).hexdigest() != pin["hashes"]["sha256"]:
    raise SystemExit("Cannot test acquisition identity: dependency wheel differs from its release pin.")
sys.stdout.write(str(dependency) + "\n")
PY
)
source "$root/host/tools/checker_operation.sh"
checker_begin
checker_run 60 --pull never --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --pids-limit 64 --memory 128m \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m \
    --mount "type=bind,src=$dependency,dst=/identity.whl,readonly" \
    --mount "type=bind,src=$root/host/acquisition_tool,dst=/source/acquisition_tool,readonly" \
    --mount "type=bind,src=$root/tests/acquisition_identity,dst=/tests,readonly" \
    --entrypoint python3 "$base_image" -I -S -B -c \
    'import sys,unittest; sys.path[:0]=["/identity.whl", "/source", "/tests"]; unittest.main(module="test_identity", verbosity=2)'
