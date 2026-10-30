#!/usr/bin/env bash
# Check a complete pool without requiring any network grant.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/tools/base_image.sh"
source "$root/tools/checker_operation.sh"
uid=$(id -u) gid=$(id -g)
checker_begin
checker_run 120 --pull never --network none --read-only --user "$uid:$gid" --cap-drop ALL \
    --security-opt no-new-privileges:true --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m --workdir /workspace \
    --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/workspace \
    --mount "type=bind,src=$root/tools,dst=/workspace/tools,readonly" \
    --mount "type=bind,src=$root/requirements/provider-core.lock,dst=/core-runtime.lock,readonly" \
    --mount "type=bind,src=$root/requirements/delivery-release.json,dst=/release.json,readonly" \
    --mount "type=bind,src=$1,dst=/wheelhouse,readonly" \
    --entrypoint python3 "$base_image" -B -m tools.delivery_image check \
    --pin /release.json --runtime-lock /core-runtime.lock --pool /wheelhouse > "$operation_directory/result"
IFS= read -r result < "$operation_directory/result"
[[ $result == complete || $result == missing ]] && cmp -s "$operation_directory/result" <(printf '%s\n' "$result")
printf -v operation_success '%s\n' "$result"
