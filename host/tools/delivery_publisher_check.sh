#!/usr/bin/env bash
# Qualify the delivery lock against the released publisher without network.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/host/tools/base_image.sh"
source "$root/host/tools/wheelhouse_path.sh"
select_wheelhouse
require_wheelhouse_pool
source "$root/host/tools/checker_operation.sh"
checker_begin
checker_host_identity "$base_image" /usr/local/bin/python3 "$(id -u)" "$(id -g)"
checker_run 120 --pull never --network none --read-only --user "$checker_host_uid:$checker_host_gid" \
    --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 64 --memory 512m \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m --workdir /workspace \
    --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/workspace \
    --mount "type=bind,src=$root/tools,dst=/workspace/tools,readonly" \
    --mount "type=bind,src=$root/tests/repository/check_delivery_publisher.py,dst=/check.py,readonly" \
    --mount "type=bind,src=$root/requirements/provider-core.lock,dst=/core-runtime.lock,readonly" \
    --mount "type=bind,src=$root/requirements/delivery-release.json,dst=/release.json,readonly" \
    --mount "type=bind,src=$root/requirements/wheelhouse-tool-release.json,dst=/wheelhouse-tool-release.json,readonly" \
    --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly" \
    --entrypoint python3 "$base_image" -B /check.py
