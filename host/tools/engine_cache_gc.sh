#!/usr/bin/env bash
# One selected engine owns BuildKit cache; project image retirement is separate.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ $(id -u) != 0 ]] || { printf '%s\n' 'Run engine cache GC as the nonroot engine owner.' >&2; exit 2; }
action=${1:?Select install or run}
shift
case "$action" in
    install)
        [[ $# == 0 && -v DOCKER_CONTEXT_INPUT_ORIGIN && $DOCKER_CONTEXT_INPUT_ORIGIN == 'command line' \
            && -v DOCKER_CONTEXT_INPUT && -n $DOCKER_CONTEXT_INPUT \
            && -v ENGINE_CONFIG_INPUT_ORIGIN && $ENGINE_CONFIG_INPUT_ORIGIN == 'command line' \
            && -v ENGINE_CONFIG_INPUT && -n $ENGINE_CONFIG_INPUT ]] || {
            printf '%s\n' 'Use make engine/cache/gc/install DOCKER_CONTEXT=<context> ENGINE_CONFIG=/absolute/daemon.json.' >&2; exit 2;
        }
        source "$root/tools/docker_context.sh"
        select_docker_context
        exec python3 -I -S -B "$root/tools/engine_cache_gc.py" install "$root" \
            "$DOCKER_CONTEXT_INPUT" "$NMR_SELECTED_DOCKER_ENDPOINT" "$ENGINE_CONFIG_INPUT"
        ;;
    run)
        [[ $# == 3 ]] || exit 2
        export DOCKER_CONTEXT_INPUT_ORIGIN='command line' DOCKER_CONTEXT_INPUT=$1
        expected_endpoint=$2 native_config=$3
        unset NMR_SELECTED_DOCKER_ENDPOINT
        source "$root/tools/docker_context.sh"
        select_docker_context
        [[ $NMR_SELECTED_DOCKER_ENDPOINT == "$expected_endpoint" ]] || {
            printf '%s\n' 'Cache GC context now selects another engine; reinstall the timer for the intended engine.' >&2; exit 2;
        }
        policy=$(python3 -I -S -B "$root/tools/engine_cache_gc.py" policy "$native_config")
        if [[ $policy == disabled ]]; then exit; fi
        mapfile -t limits <<< "$policy"
        [[ ${#limits[@]} == 3 ]] || exit 2
        source "$root/tools/docker_client.sh"
        source "$root/tools/inference_native.sh"
        docker_client_directory= inference_diagnostic=
        inference_retirement_confirmed=true
        finish() {
            local status=$?
            trap - EXIT
            if "$inference_retirement_confirmed"; then
                docker_client_cleanup || status=2
            else
                printf 'Cache GC client remains unsettled; retain its private client at %q.\n' "$docker_client_directory" >&2
                status=2
            fi
            printf '%s' "$inference_diagnostic" >&2
            exit "$status"
        }
        trap finish EXIT
        trap 'exit 129' HUP
        trap 'exit 130' INT
        trap 'exit 143' TERM
        docker_client_prepare run || { printf '%s' "$docker_client_diagnostic" >&2; exit 2; }
        inference_native 'engine BuildKit cache GC' 300 stdio "${engine[@]}" buildx prune \
            --builder default --all --force --reserved-space "${limits[0]}" \
            --max-used-space "${limits[1]}" --min-free-space "${limits[2]}"
        ;;
    *) exit 2 ;;
esac
