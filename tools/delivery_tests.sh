#!/usr/bin/env bash
# Qualify the installed finite freezer with no source, network or Engine grant.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ $# == 0 ]] || exit 2
uid=$(id -u) gid=$(id -g)
if [[ $uid == 0 || $gid == 0 || $root =~ [[:cntrl:],] ]]; then exit 2; fi
[[ -v SHARED_RUNTIME_IMAGE_INPUT_ORIGIN && $SHARED_RUNTIME_IMAGE_INPUT_ORIGIN == 'command line' \
    && -v SHARED_RUNTIME_IMAGE_INPUT && $SHARED_RUNTIME_IMAGE_INPUT =~ ^sha256:[0-9a-f]{64}$ ]] \
    || { printf '%s\n' 'Use tests/deployment with explicit SHARED_RUNTIME_IMAGE from delivery/image/build.' >&2; exit 2; }
image=$SHARED_RUNTIME_IMAGE_INPUT
source "$root/tools/checker_operation.sh"
checker_begin
checker_metadata "$operation_directory/image" image inspect --format '{{.Id}} {{index .Config.Labels "org.nmr-provider-tools.role"}}' "$image"
cmp -s "$operation_directory/image" <(printf '%s delivery\n' "$image") || exit 2
checker_run untimed --pull never --network none --init --read-only --user "$uid:$gid" --cap-drop ALL \
    --security-opt no-new-privileges:true --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m --env PROVIDER_CORE_IN_CONTAINER=1 \
    --mount "type=bind,src=$root/packages/provider-core/tests,dst=/test-support/tests,readonly" \
    --mount "type=bind,src=$root/tests/deployment,dst=/qualification,readonly" \
    --entrypoint /opt/provider-core/environment/bin/python "$image" -I -B -m unittest discover -s /qualification -p 'test*.py' -v
