#!/usr/bin/env bash
# Build and test the shared tool without installing provider-core.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
action=${1:?Select build or test}
source "$root/host/tools/base_image.sh"
source "$root/host/tools/wheelhouse_path.sh"
select_wheelhouse
require_wheelhouse_pool
source "$root/host/tools/checker_operation.sh"
checker_begin
checker_admit_tools
image=nmr-provider-tools/wheelhouse-tool:local
case "$action" in
    build)
        if ! python3 -I -S -B -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
            printf '%s\n' 'Cannot build the wheelhouse tool: host Python 3.11 or newer is required.' >&2; exit 2
        fi
        checker_host_identity "$checker_image" python3 "$(id -u)" "$(id -g)"
        sources=(containers/wheelhouse/Dockerfile requirements/wheelhouse-tool.lock requirements/wheelhouse-tool-release.json
            tools/installer.py tools/container_guard.py requirements/installer-release.json)
        for path in "$root"/wheelhouse_tool/*.py; do sources+=("${path#"$root"/}"); done
        tool_pin=$(python3 -I -S -B "$root/host/tools/build_stream_inputs.py" "$root" \
            "$operation_directory/source-allowlist.json" "${sources[@]}")
        read -r tool_name tool_hash <<< "$tool_pin"
        [[ $tool_name =~ ^wheelhouse-tool-[0-9.]+\.pyz$ && $tool_hash =~ ^[0-9a-f]{64}$ ]] || {
            printf '%s\n' 'Cannot build the wheelhouse tool: its release pin is invalid.' >&2; exit 2;
        }
        if [[ ! -e $wheelhouse_directory/$tool_name && ! -L $wheelhouse_directory/$tool_name ]]; then
            printf '%s\n' 'Cannot build the wheelhouse tool: its pinned streamer is missing. Run make packages/provider-core/download with WHEELHOUSE and NETWORK_INTERFACE first.' >&2
            exit 2
        fi
        [[ -f $wheelhouse_directory/$tool_name && ! -L $wheelhouse_directory/$tool_name ]] \
            && [[ $(sha256sum -- "$wheelhouse_directory/$tool_name" | cut -d' ' -f1) == "$tool_hash" ]] || {
            printf '%s\n' 'Cannot build the wheelhouse tool: cached release bytes differ from the pin.' >&2; exit 2;
        }
        checker_run 120 --pull never --network none --read-only --user "$checker_host_uid:$checker_host_gid" \
            --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 64 --memory 512m \
            --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m --workdir /workspace \
            --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/workspace \
            --mount "type=bind,src=$root/tools,dst=/workspace/tools,readonly" \
            --mount "type=bind,src=$root/requirements/wheelhouse-tool.lock,dst=/tool.lock,readonly" \
            --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly" \
            --entrypoint python3 "$checker_image" -B -m tools.wheelhouse check --pool /wheelhouse --lock /tool.lock > "$operation_directory/selection.json"
        checker_metadata "$operation_directory/selection-exit" inspect --format '{{.State.Running}} {{.State.ExitCode}}' "$operation_container"
        cmp -s "$operation_directory/selection-exit" <(printf 'false 0\n') || {
            printf '%s\n' 'Cannot build the wheelhouse tool: the selection worker did not complete.' >&2; exit 2;
        }
        python3 -I -S -B "$root/host/tools/wheelhouse_image_manifest.py" "$operation_directory/selection.json" "$operation_directory/manifest.json" "$root/requirements/installer-release.json"
        # BuildKit receives a stream, not host mounts. Once the pipeline is
        # joined it no longer reads scratch; checker_call retains unjoined work.
        checker_call 'offline wheelhouse tool build' 900 stdio bash -o pipefail -c '
            tool=$1 pool=$2 allowlist=$3 manifest=$4
            shift 4
            python3 -I -S -B "$tool" stream --pool "$pool" --source-allowlist "$allowlist" --manifest "$manifest" | "$@"
        ' bash "$wheelhouse_directory/$tool_name" "$wheelhouse_directory" "$operation_directory/source-allowlist.json" \
            "$operation_directory/manifest.json" "${engine[@]}" build --builder default --network none --pull=false \
            --label org.nmr-provider-tools.role=wheelhouse-tool \
            --build-arg "BASE_IMAGE=$base_image" --file containers/wheelhouse/Dockerfile \
            --iidfile "$operation_directory/built-image" -
        built_image=
        if [[ -f $operation_directory/built-image && ! -L $operation_directory/built-image && $(stat -c %s -- "$operation_directory/built-image") -le 72 ]]; then
            if IFS= read -r -N 71 built_image < "$operation_directory/built-image"; then :; else built_image=; fi
        fi
        [[ $built_image =~ ^sha256:[0-9a-f]{64}$ ]] \
            && { cmp -s "$operation_directory/built-image" <(printf '%s' "$built_image") || cmp -s "$operation_directory/built-image" <(printf '%s\n' "$built_image"); } || {
            printf '%s\n' 'Cannot admit the wheelhouse tool build: no immutable image ID was returned.' >&2; exit 2;
        }
        checker_call 'publish wheelhouse tool image' 30 stdio "${engine[@]}" image tag "$built_image" "$image"
        image_retire_after_success=true
        ;;
    test)
        checker_run 180 --pull never --network none --read-only --cap-drop ALL \
            --security-opt no-new-privileges:true --pids-limit 128 --memory 512m \
            --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m \
            --mount "type=bind,src=$root/wheelhouse_tool,dst=/opt/wheelhouse_tool,readonly" \
            --mount "type=bind,src=$root/tests/test_shared_wheelhouse.py,dst=/tests/test_shared_wheelhouse.py,readonly" \
            --entrypoint python "$image" -B -m unittest discover -s /tests -p test_shared_wheelhouse.py -v
        ;;
    *) printf 'Unknown wheelhouse image operation: %s\n' "$action" >&2; exit 2 ;;
esac
