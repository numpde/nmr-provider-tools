#!/usr/bin/env bash
# Reuse the common wheelhouse and build the explicitly pinned delivery release.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/tools/docker_context.sh"
select_docker_context
source "$root/tools/base_image.sh"
source "$root/tools/wheelhouse_path.sh"
select_wheelhouse
mkdir -p -- "$wheelhouse_directory"
require_wheelhouse_pool
uid=$(id -u) gid=$(id -g)
[[ $uid != 0 && $gid != 0 && ! $root =~ [[:cntrl:],] ]] || {
    printf '%s\n' 'Cannot prepare shared delivery: use a nonzero caller and a mountable repository path.' >&2; exit 2
}
source "$root/tools/checker_operation.sh"
checker_begin
checker_host_identity "$base_image" python3 "$uid" "$gid" || {
    printf '%s\n' 'Cannot map the wheelhouse owner into the selected engine.' >&2; exit 2;
}
cache=$(bash "$root/tools/delivery_cache_check.sh" "$wheelhouse_directory")
if [[ $cache == missing ]]; then
    checker_acquisition_receipt
    bash "$root/tools/docker.sh" run --image "$base_image" --retired-receipt "$operation_acquisition_receipt" -- --pull never --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m --workdir /workspace \
    --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/workspace \
    --mount "type=bind,src=$root/tools,dst=/workspace/tools,readonly" \
    --mount "type=bind,src=$root/requirements/provider-core.lock,dst=/core-runtime.lock,readonly" \
    --mount "type=bind,src=$root/requirements/delivery-release.json,dst=/release.json,readonly" \
    --mount "type=bind,src=$root/requirements/wheelhouse-tool-release.json,dst=/wheelhouse-tool-release.json,readonly" \
    --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse" \
    --entrypoint python3 -- -B -m tools.delivery_image acquire \
    --pin /release.json --runtime-lock /core-runtime.lock --pool /wheelhouse --tool-pin /wheelhouse-tool-release.json
elif [[ $cache != complete ]]; then
    printf '%s\n' 'Cannot inspect the shared delivery wheelhouse.' >&2; exit 2
fi
WHEELHOUSE_INPUT_ORIGIN='command line' WHEELHOUSE_INPUT=$wheelhouse_directory bash "$root/tools/delivery_image.sh"

# A nested image builder may defer retirement while this outer borrower runs.
image_retire_after_success=true
