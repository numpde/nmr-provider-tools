#!/usr/bin/env bash
# Reuse the common wheelhouse and build the explicitly pinned delivery release.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/operations/docker/docker_context.sh"
select_docker_context
source "$root/operations/docker/base_image.sh"
source "$root/host/tools/wheelhouse_path.sh"
select_wheelhouse
mkdir -p -- "$wheelhouse_directory"
require_wheelhouse_pool
uid=$(id -u) gid=$(id -g)
[[ $uid != 0 && $gid != 0 && ! $root =~ [[:cntrl:],] ]] || {
    printf '%s\n' 'Cannot prepare shared delivery: use a nonzero caller and a mountable repository path.' >&2; exit 2
}
source "$root/operations/docker/container_operation.sh"
source "$root/operations/checker/image_identity.sh"
operation_begin
checker_admit_tools
operation_reserve_acquisition_receipt
bash "$root/operations/docker/docker.sh" run --image "$checker_image" --retired-receipt "$operation_acquisition_receipt" -- --pull never --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m --workdir /workspace \
    --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/workspace \
    --mount "type=bind,src=$root/tools,dst=/workspace/tools,readonly" \
    --mount "type=bind,src=$root/packages/type-requirements/src/type_requirements.py,dst=/workspace/type_requirements.py,readonly" \
    --mount "type=bind,src=$root/packages/provider-core/sdk_build/provider_core_acquire.py,dst=/workspace/sdk_build/provider_core_acquire.py,readonly" \
    --mount "type=bind,src=$root/operations/delivery/build_inputs.py,dst=/workspace/operations/delivery/build_inputs.py,readonly" \
    --mount "type=bind,src=$root/requirements/delivery.wheelhouse.json,dst=/delivery.wheelhouse.json,readonly" \
    --mount "type=bind,src=$root/requirements/wheelhouse-tool-release.json,dst=/wheelhouse-tool-release.json,readonly" \
    --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse" \
    --entrypoint python3 -- -B -m operations.delivery.build_inputs acquire \
    --lock /delivery.wheelhouse.json --pool /wheelhouse --tool-pin /wheelhouse-tool-release.json
WHEELHOUSE_INPUT_ORIGIN='command line' WHEELHOUSE_INPUT=$wheelhouse_directory bash "$root/operations/delivery/build_image.sh"

# A nested image builder may defer retirement while this outer borrower runs.
image_retire_after_success=true
