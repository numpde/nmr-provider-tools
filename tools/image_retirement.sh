#!/usr/bin/env bash
# Exact image retirement uses the same selected Docker client as image owners.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
automatic=false
if [[ $# == 1 && $1 == --automatic ]]; then automatic=true; shift; fi
[[ $# == 0 ]] || exit 2
if "$automatic"; then
    [[ -n ${NMR_SELECTED_DOCKER_ENDPOINT:-} ]] || exit 2
else
    [[ ${DOCKER_CONTEXT_INPUT_ORIGIN:-} == 'command line' ]] || {
        printf '%s\n' 'Use make packages/images/retire DOCKER_CONTEXT=<local-context>.' >&2; exit 2;
    }
fi
[[ $(id -u) != 0 && $(id -g) != 0 ]] || exit 2
source "$root/tools/docker_context.sh"
select_docker_context
source "$root/tools/image_lifecycle.sh"
if image_lifecycle_lock exclusive; then :; else
    status=$?
    if "$automatic" && [[ $status == 75 ]]; then exit 0; fi
    printf '%s\n' 'Image retirement is busy or unavailable; retry its maintained Make target.' >&2
    exit "$status"
fi
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
        printf 'Image retirement client remains unsettled; retain its private client at %q.\n' "$docker_client_directory" >&2
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
staged=
if [[ ${SHARED_RUNTIME_IMAGE_INPUT_ORIGIN:-} == 'command line' ]]; then
    staged=${SHARED_RUNTIME_IMAGE_INPUT:-}
    [[ $staged =~ ^sha256:[0-9a-f]{64}$ ]] || {
        printf '%s\n' 'The staged SHARED_RUNTIME_IMAGE must be an immutable image ID.' >&2; exit 2;
    }
fi
inference_native 'exact project image retirement' 180 stdio \
    python3 -I -S "$root/tools/image_retirement.py" "$root" "$staged" "${engine[@]}"
