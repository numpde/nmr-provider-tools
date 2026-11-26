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
source "$root/operations/docker/container_operation.sh"
operation_begin
operation_run 60 --pull never --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --pids-limit 64 --memory 128m \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m \
    --mount "type=bind,src=$root/requirements/acquisition-bootstrap.pylock.toml,dst=/bootstrap.pylock.toml,readonly" \
    --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly" \
    --mount "type=bind,src=$root/packages/acquisition/acquisition_tool,dst=/source/acquisition_tool,readonly" \
    --mount "type=bind,src=$root/tests/acquisition_identity,dst=/tests,readonly" \
    --entrypoint python3 "$base_image" -I -S -B /tests/run_tests.py
