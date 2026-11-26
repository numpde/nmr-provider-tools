#!/usr/bin/env bash
# Exercise the cold identity adapter against its exact released dependency.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/operations/docker/docker_context.sh"
select_docker_context
source "$root/operations/docker/base_image.sh"
source "$root/host/tools/wheelhouse_path.sh"
select_wheelhouse
require_wheelhouse_pool
tool=$(python3 -I -S -B - "$root/requirements/wheelhouse-tool-release.json" "$wheelhouse_directory" <<'PYCODE'
import hashlib
import json
import pathlib
import sys

pin = json.loads(pathlib.Path(sys.argv[1]).read_text())
tool = pathlib.Path(sys.argv[2]) / pin["name"]
try:
    tool_bytes = tool.read_bytes()
except FileNotFoundError as error:
    raise SystemExit(
        f"Cannot test acquisition identity: pinned Wheelhouse {tool.name} is missing from WHEELHOUSE. "
        "Run make packages/provider-core/download with explicit DOCKER_CONTEXT, WHEELHOUSE and NETWORK_INTERFACE."
    ) from error
if hashlib.sha256(tool_bytes).hexdigest() != pin["sha256"]:
    raise SystemExit("Cannot test acquisition identity: Wheelhouse differs from its independent release pin.")
sys.stdout.write(str(tool) + "\n")
PYCODE
)
source "$root/operations/docker/container_operation.sh"
operation_begin
operation_run 60 --pull never --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --pids-limit 64 --memory 128m \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m \
    --mount "type=bind,src=$tool,dst=/wheelhouse.pyz,readonly" \
    --mount "type=bind,src=$root/requirements/acquisition-bootstrap.wheelhouse.json,dst=/bootstrap.wheelhouse.json,readonly" \
    --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly" \
    --mount "type=bind,src=$root/packages/acquisition/acquisition_tool,dst=/source/acquisition_tool,readonly" \
    --mount "type=bind,src=$root/tests/acquisition_identity,dst=/tests,readonly" \
    --entrypoint python3 "$base_image" -I -S -B /tests/run_tests.py
