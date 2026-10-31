#!/usr/bin/env bash
# Explicit wheel acquisition, installed tests and committed-source package builds.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
printing=false
if [[ $# -gt 0 && $1 == --print ]]; then printing=true; shift; fi
if [[ $# != 1 ]]; then printf '%s\n' 'Supply one provider-core target from make help.' >&2; exit 2; fi
action=$1
case "$action" in download|check|unit|contract|integration|integration/delivery|build|package) ;;
    *) printf 'Cannot select provider-core operation %q. Use make help.\n' "$action" >&2; exit 2 ;;
esac
source "$root/tools/base_image.sh"
source "$root/tools/wheelhouse_path.sh"
select_wheelhouse
uid=$(id -u)
gid=$(id -g)
if [[ $uid == 0 || $gid == 0 || $root =~ [[:cntrl:],] ]]; then
    printf '%s\n' 'Cannot mount provider-core inputs: use a nonzero UID/GID and repository path without control characters or commas.' >&2
    exit 2
fi
worker=(--pull never --read-only --user "$uid:$gid" --cap-drop ALL --security-opt no-new-privileges:true
    --cpus 2 --memory 1g --memory-swap 1g --pids-limit 128
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m --workdir /workspace
    --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1 --env PROVIDER_CORE_IN_CONTAINER=1
    --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/workspace
    --mount "type=bind,src=$root/tools,dst=/workspace/tools,readonly"
    --mount "type=bind,src=$root/requirements/provider-core.lock,dst=/core-runtime.lock,readonly"
    --mount "type=bind,src=$root/requirements/provider-core-build.lock,dst=/core-build.lock,readonly")
if [[ $action == download ]]; then
    if ! "$printing"; then mkdir -p -- "$wheelhouse_directory"; fi
    network=(bash "$root/tools/docker.sh")
    if "$printing"; then network+=(--print); fi
    exec "${network[@]}" run "${worker[@]}" --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse" \
        --entrypoint python3 "$base_image" -B -m tools.wheelhouse download --pool /wheelhouse \
        --lock /core-runtime.lock --lock /core-build.lock
fi
require_wheelhouse_pool
case "$action" in
    check)
        arguments=("${worker[@]}" --network none --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly"
            --entrypoint python3 "$base_image" -B -m tools.wheelhouse check --pool /wheelhouse
            --lock /core-runtime.lock --lock /core-build.lock)
        budget=60
        ;;
    unit|contract|integration|integration/delivery)
        # The installed cryptography extension requires executable mappings.
        # Only /runtime has that grant; build scratch remains noexec under /tmp.
        arguments=("${worker[@]}" --network none --init --tmpfs /runtime:rw,nosuid,nodev,exec,size=256m,mode=1777
            --mount "type=bind,src=$root/packages/provider-core,dst=/package,readonly"
            --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly"
            --entrypoint python3 "$base_image" -B -m tools.provider_core_tests "$action" --pattern 'test*.py')
        budget=untimed
        ;;
    package)
        if [[ ! -v ARTIFACTS_INPUT_ORIGIN || $ARTIFACTS_INPUT_ORIGIN != 'command line' || ! -v ARTIFACTS_INPUT || -z $ARTIFACTS_INPUT || -L $ARTIFACTS_INPUT ]]; then
            printf '%s\n' 'Cannot qualify release artifacts: select ARTIFACTS as an existing owned directory on the Make command line.' >&2
            exit 2
        fi
        if ! IFS= read -r -d '' artifacts < <(realpath -ze -- "$ARTIFACTS_INPUT"); then
            printf '%s\n' 'Cannot resolve release ARTIFACTS: the selected directory must already exist.' >&2
            exit 2
        fi
        if [[ $artifacts == / || $artifacts =~ [[:cntrl:],] || ! -d $artifacts || $(stat -c %u -- "$artifacts") != "$uid" ]]; then
            printf '%s\n' 'Cannot grant release ARTIFACTS: select a caller-owned directory below the filesystem root without mount delimiters.' >&2
            exit 2
        fi
        revision=$(git -C "$root" rev-parse HEAD)
        arguments=("${worker[@]}" --network none --init --tmpfs /runtime:rw,nosuid,nodev,exec,size=256m,mode=1777
            --mount "type=bind,src=$artifacts,dst=/artifacts,readonly"
            --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly"
            --entrypoint python3 "$base_image" -B -m tools.package --artifacts /artifacts --revision "$revision")
        budget=untimed
        ;;
    build)
        if "$printing"; then printf '%s\n' 'Package build captures clean committed HEAD before dispatch; use make package/build WHEELHOUSE=... .'; exit; fi
        if checkout_status=$(git -C "$root" status --porcelain); then :; else
            printf '%s\n' 'Cannot build a release: Git checkout status could not be inspected; no clean-source claim was established.' >&2
            exit 2
        fi
        if [[ -n $checkout_status ]]; then
            printf '%s\n' 'Cannot build a release: commit or remove pending checkout changes before capturing HEAD.' >&2
            exit 2
        fi
        revision=$(git -C "$root" rev-parse HEAD)
        if [[ ! -v OUTPUT_INPUT_ORIGIN || $OUTPUT_INPUT_ORIGIN != 'command line' || ! -v OUTPUT_INPUT || -z $OUTPUT_INPUT || -L $OUTPUT_INPUT ]]; then
            printf '%s\n' 'Cannot build release artifacts: select OUTPUT as an existing empty owned directory on the Make command line.' >&2
            exit 2
        fi
        if ! IFS= read -r -d '' release_output < <(realpath -ze -- "$OUTPUT_INPUT"); then
            printf '%s\n' 'Cannot resolve release OUTPUT: the selected directory must already exist.' >&2
            exit 2
        fi
        if [[ $release_output == / || $release_output =~ [[:cntrl:],] || ! -d $release_output \
            || $(stat -c %u -- "$release_output") != "$uid" || $(stat -c %a -- "$release_output") != 700 \
            || -n $(find "$release_output" -mindepth 1 -maxdepth 1 -print -quit) ]]; then
            printf '%s\n' 'Cannot grant release OUTPUT: select a private empty caller-owned mode 0700 directory below the filesystem root without mount delimiters.' >&2
            exit 2
        fi
        ;;
esac
source "$root/tools/checker_operation.sh"
if "$printing"; then
    if ! docker_client_prepare print; then printf '%s' "$docker_client_diagnostic" >&2; exit 2; fi
    printf '%q ' "${engine[@]}" create --label org.nmr-provider-tools.operation=PRINT --cidfile /tmp/nmr-provider-tools.PRINT/container.cid "${arguments[@]}"
    printf '\n'
    exit
fi
checker_begin
if [[ $action == build ]]; then
    mkdir -- "$operation_directory/source"
    checker_call 'committed core source capture' 30 stdio git -C "$root" -c tar.umask=0022 archive --format=tar \
        --output="$operation_directory/source.tar" "$revision" packages/provider-core requirements tools
    tar --same-permissions -xf "$operation_directory/source.tar" -C "$operation_directory/source"
    arguments=(--pull never --network none --read-only --user "$uid:$gid" --cap-drop ALL --security-opt no-new-privileges:true
        --cpus 2 --memory 1g --memory-swap 1g --pids-limit 128 --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m
        --workdir /workspace --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1 --env PROVIDER_CORE_IN_CONTAINER=1
        --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/workspace
        --mount "type=bind,src=$operation_directory/source/tools,dst=/workspace/tools,readonly"
        --mount "type=bind,src=$operation_directory/source/packages/provider-core,dst=/package,readonly"
        --mount "type=bind,src=$operation_directory/source/requirements,dst=/requirements,readonly"
        --mount "type=bind,src=$release_output,dst=/output"
        --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly"
        --entrypoint python3 "$base_image" -B -m tools.core_environment release --pool /wheelhouse
        --source /package --runtime-lock /requirements/provider-core.lock --build-lock /requirements/provider-core-build.lock
        --output /output --revision "$revision")
    budget=900
fi
checker_run "$budget" "${arguments[@]}"
