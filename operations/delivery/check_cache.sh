#!/usr/bin/env bash
# Check a complete pool without requiring any network grant.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/operations/docker/base_image.sh"
source "$root/operations/docker/container_operation.sh"
source "$root/operations/checker/image_identity.sh"
uid=$(id -u) gid=$(id -g)
operation_begin
checker_admit_tools
operation_run 120 --pull never --network none --read-only --user "$uid:$gid" --cap-drop ALL \
    --security-opt no-new-privileges:true --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m --workdir /workspace \
    --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/workspace \
    --mount "type=bind,src=$root/tools,dst=/workspace/tools,readonly" \
    --mount "type=bind,src=$root/packages/type-requirements/src/type_requirements.py,dst=/workspace/type_requirements.py,readonly" \
    --mount "type=bind,src=$root/packages/provider-core/sdk_build/provider_core_acquire.py,dst=/workspace/sdk_build/provider_core_acquire.py,readonly" \
    --mount "type=bind,src=$root/operations/delivery/build_inputs.py,dst=/workspace/operations/delivery/build_inputs.py,readonly" \
    --mount "type=bind,src=$root/requirements/provider-core.lock,dst=/core-runtime.lock,readonly" \
    --mount "type=bind,src=$root/requirements/delivery-release.json,dst=/release.json,readonly" \
    --mount "type=bind,src=$root/requirements/wheelhouse-tool-release.json,dst=/wheelhouse-tool-release.json,readonly" \
    --mount "type=bind,src=$1,dst=/wheelhouse,readonly" \
    --entrypoint python3 "$checker_image" -B -m operations.delivery.build_inputs check \
    --pin /release.json --runtime-lock /core-runtime.lock --pool /wheelhouse --tool-pin /wheelhouse-tool-release.json > "$operation_directory/result"
docker_inspect_container_state "$operation_directory/check-exit" "$operation_container"
docker_container_state_matches "$operation_directory/check-exit" false 0 || {
    operation_diagnostic+=$'Cannot inspect the delivery pool: the checker did not retire successfully.\n'; exit 2;
}
IFS= read -r result < "$operation_directory/result"
[[ $result == complete || $result == missing ]] && cmp -s "$operation_directory/result" <(printf '%s\n' "$result")
printf -v operation_success '%s\n' "$result"
