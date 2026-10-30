#!/usr/bin/env bash
# Reuse the common wheelhouse and build the explicitly pinned delivery release.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/tools/base_image.sh"
source "$root/tools/wheelhouse_path.sh"
select_wheelhouse
mkdir -p -- "$wheelhouse_directory"
require_wheelhouse_pool
uid=$(id -u) gid=$(id -g)
[[ $uid != 0 && $gid != 0 && ! $root =~ [[:cntrl:],] ]] || {
    printf '%s\n' 'Cannot prepare shared delivery: use a nonzero caller and a mountable repository path.' >&2; exit 2
}
cache=$(bash "$root/tools/delivery_cache_check.sh" "$wheelhouse_directory")
if [[ $cache == missing ]]; then
    bash "$root/tools/docker.sh" run --pull never --read-only --user "$uid:$gid" --cap-drop ALL \
    --security-opt no-new-privileges:true --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m --workdir /workspace \
    --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/workspace \
    --mount "type=bind,src=$root/tools,dst=/workspace/tools,readonly" \
    --mount "type=bind,src=$root/requirements/provider-core.lock,dst=/core-runtime.lock,readonly" \
    --mount "type=bind,src=$root/requirements/delivery-release.json,dst=/release.json,readonly" \
    --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse" \
    --entrypoint python3 "$base_image" -B -m tools.delivery_image acquire \
    --pin /release.json --runtime-lock /core-runtime.lock --pool /wheelhouse
elif [[ $cache != complete ]]; then
    printf '%s\n' 'Cannot inspect the shared delivery wheelhouse.' >&2; exit 2
fi
make --no-print-directory delivery/image/build "WHEELHOUSE=$wheelhouse_directory"
