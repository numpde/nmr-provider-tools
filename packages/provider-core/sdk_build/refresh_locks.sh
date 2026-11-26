#!/usr/bin/env bash
# Capture direct intent, resolve through the acquisition route, then publish locks.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)
source "$root/operations/docker/docker_context.sh"
select_docker_context
uid=$(id -u)
gid=$(id -g)
if [[ $uid == 0 || $gid == 0 || $root =~ [[:cntrl:],] ]]; then
    printf '%s\n' 'Cannot refresh SDK locks: use a nonzero UID/GID and repository path without mount delimiters.' >&2
    exit 2
fi
source "$root/operations/docker/container_operation.sh"
operation_begin
source "$root/operations/checker/image_identity.sh"
checker_admit_tools
operation_host_identity "$checker_image" /usr/local/bin/python3 "$uid" "$gid" || exit "$?"
inputs=(packages/provider-core/pyproject.toml packages/provider-core/tests/requirements.in)
mkdir -- "$operation_directory/source" "$operation_directory/locks"
for path in "${inputs[@]}"; do
    (cd -- "$root"; cp --parents --preserve=mode -- "$path" "$operation_directory/source/")
done
worker=(--read-only --cap-drop ALL --security-opt no-new-privileges:true
    --cpus 2 --memory 1g --memory-swap 1g --pids-limit 128
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m --workdir /workspace
    --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1
    --env PYTHONPATH=/workspace:/workspace/packages/type-requirements/src
    --mount "type=bind,src=$root/tools,dst=/workspace/tools,readonly"
    --mount "type=bind,src=$root/packages/provider-core/sdk_build,dst=/workspace/sdk_build,readonly"
    --mount "type=bind,src=$root/packages/type-requirements/src,dst=/workspace/packages/type-requirements/src,readonly"
    --mount "type=bind,src=$operation_directory/source,dst=/intent,readonly"
    --mount "type=bind,src=$operation_directory/locks,dst=/locks"
    --entrypoint python3)
operation_reserve_acquisition_receipt
if controller_native 'SDK lock refresh' untimed stdio bash "$root/operations/docker/docker.sh" run \
    --image "$checker_image" --retired-receipt "$operation_acquisition_receipt" -- "${worker[@]}" -- \
    -B -m sdk_build.refresh_locks --source /intent --output /locks; then :; else
    refresh_status=$?
    printf '%s' "$inference_diagnostic" >&2
    exit "$refresh_status"
fi
if ! "$inference_retirement_confirmed"; then exit 2; fi
for path in "${inputs[@]}"; do
    if ! cmp -s -- "$root/$path" "$operation_directory/source/$path"; then
        operation_diagnostic+=$'Cannot publish SDK locks: dependency intent changed during resolution; retry after edits finish.\n'
        exit 2
    fi
done
for role in provider-core-runtime provider-core-build provider-core-test; do
    case $role in
        provider-core-runtime) destination=$root/requirements/provider-core.lock ;;
        provider-core-build) destination=$root/requirements/provider-core-build.lock ;;
        provider-core-test) destination=$root/packages/provider-core/tests/requirements.lock ;;
    esac
    # Stage beside the destination so each published lock is a complete rename.
    temporary=$(mktemp "$(dirname -- "$destination")/.$role.XXXXXXXX")
    if cp -- "$operation_directory/locks/$role.lock" "$temporary" \
        && chmod 644 -- "$temporary" \
        && mv -fT -- "$temporary" "$destination"; then :; else
        rm -f -- "$temporary"
        exit 2
    fi
done
printf -v operation_success 'Refreshed runtime, build and test requirements locks. Review package and origin changes before committing.\n'
