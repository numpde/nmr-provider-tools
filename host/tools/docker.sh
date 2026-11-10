#!/usr/bin/env bash
# Root operations consume the last qualified acquisition owner, like providers.
set -euo pipefail
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
root=$repository_root
source "$root/host/tools/base_image.sh"
readonly proxy_image=$base_image metadata_timeout=30
print_command=false
if [[ $# -gt 0 && $1 == --print ]]; then print_command=true; shift; fi
next_actions=
if [[ $# -gt 0 && $1 == --success-note ]]; then next_actions=$2; shift 2; fi
operation=
if [[ $# -gt 0 ]]; then operation=$1; shift; fi
case "$operation" in
    check-interface)
        source "$root/host/tools/acquisition_interface.sh"; select_acquisition_interface; exit ;;
    download-proxy) [[ $# == 0 ]] || exit 2 ;;
    require-images) ;;
    run) ;;
    *) printf '%s\n' 'Usage: host/tools/docker.sh [--print] [--success-note TEXT] {download-proxy|check-interface|require-images IMAGE...|run --image IMAGE [--retired-receipt FILE] -- CREATE_ARGS... -- APPLICATION_ARGS...}' >&2; exit 2 ;;
esac
image= retired_receipt= scope_started=false
create=() application=()
if [[ $operation == run ]]; then
    while [[ $# != 0 && $1 != -- ]]; do
        case "$1" in
            --image) image=$2; shift 2 ;;
            --retired-receipt) retired_receipt=$2; shift 2 ;;
            *) printf '%s\n' 'Select the worker image and optional retirement receipt before --.' >&2; exit 2 ;;
        esac
    done
    [[ -n $image && $# -gt 0 && $1 == -- ]] || exit 2
    shift
    while [[ $# != 0 && $1 != -- ]]; do create+=("$1"); shift; done
    [[ $# -gt 0 && $1 == -- ]] || exit 2
    shift; application=("$@")
fi
source "$root/host/tools/docker_context.sh"
select_docker_context
source "$root/host/tools/docker_client.sh"
source "$root/host/tools/inference_native.sh"
docker_client_directory= scratch= pending_diagnostic=
retain_preflight_receipts=false
inference_diagnostic= inference_retirement_confirmed=true inference_cancellation_status=0
private_receipt_below_limit() {
    # The fixed metadata wrapper caps EACH file at 4096 bytes. Reaching the cap
    # cannot prove complete output, even if the native client exits zero.
    local size
    if [[ ! -f "$1" || -L "$1" ]]; then return 1; fi
    if size=$(stat -c '%s' -- "$1" 2>/dev/null); then :; else return 1; fi
    # Bound arithmetic to the admitted decimal width; missing or malformed
    # metadata must not become numeric zero or overflow into an accepted size.
    [[ "$size" =~ ^(0|[1-9][0-9]{0,3})$ && "$size" -lt 4096 ]]
}

require_images() {
    local image inspect_status index=0 diagnostic
    for image in "$@"; do
        if "$print_command"; then
            printf '%q ' "${engine[@]}" image inspect "$image"; printf '\n'
            continue
        fi
        index=$((index + 1))
        diagnostic=$scratch/preload-$index.stderr
        # Image existence needs only status. Discard unused full JSON instead
        # of imposing a small file limit on valid Docker image metadata.
        if inference_native 'acquisition pinned-image inspection' "$metadata_timeout" stdio bash -eu -c '
            diagnostic=$1; shift
            umask 077
            ulimit -f 4
            exec "$@" >/dev/null 2> "$diagnostic"
        ' bash "$diagnostic" "${engine[@]}" image inspect "$image"; then inspect_status=0; else inspect_status=$?; fi
        pending_diagnostic+=$inference_diagnostic
        if ! "$inference_retirement_confirmed"; then exit "$inspect_status"; fi
        if [[ $inference_cancellation_status != 0 ]]; then exit "$inference_cancellation_status"; fi
        # Ordinary 130/143 are deliberately silent preflight outcomes, not
        # evidence that this owner handled a signal. Preserve the existing
        # test_operator_inputs_port.py:248-261,280-302 presentation contract.
        case "$inspect_status" in
            130|143)
                # Silence is presentation policy, not permission to erase the
                # private cause. Retention must not depend on public report text.
                retain_preflight_receipts=true
                exit "$inspect_status"
                ;;
            124|137)
                printf -v pending_diagnostic '%sCannot start acquisition: pinned-image inspection was interrupted (status %s). No acquisition was started.\n' "$pending_diagnostic" "$inspect_status"
                exit "$inspect_status"
                ;;
        esac
        if [[ "$inspect_status" == 0 ]]; then
            if private_receipt_below_limit "$diagnostic"; then continue; fi
            printf -v pending_diagnostic '%sCannot inspect acquisition image: its private diagnostic receipt is not a regular sub-limit file.\n' "$pending_diagnostic"
            exit 2
        fi
        printf -v pending_diagnostic '%sCannot verify pinned image availability in the local Docker engine (inspection status %s): %s. No acquisition was started.\n' \
            "$pending_diagnostic" "$inspect_status" "$image"
        printf -v pending_diagnostic '%s%s\n' "$pending_diagnostic" \
            'Inspection failure does not show whether the image is absent or the Docker daemon is unavailable; check both.'
        if [[ "$image" == "$proxy_image" ]]; then
            printf -v pending_diagnostic '%s%s\n' "$pending_diagnostic" \
                'If this base image is missing, run make checks/base-image/download; it uses Docker default routing, without NETWORK_INTERFACE binding.'
        else
            printf -v pending_diagnostic '%s%s\n' "$pending_diagnostic" \
                'If the image is missing, preload it through an independently interface-bound process; this launcher cannot bind daemon pulls to NETWORK_INTERFACE.'
        fi
        exit 2
    done
}

cleanup() {
    local status=$?
    trap - EXIT
    trap '' HUP INT TERM
    if "$inference_retirement_confirmed"; then
        # Preflight admitted no worker mounts. A settled early failure still
        # lets the caller release files that were never borrowed by a scope.
        if [[ $operation == run && $scope_started == false && -n $retired_receipt ]]; then
            if (umask 077; set -o noclobber; printf 'retired\n' > "$retired_receipt"); then :; else
                [[ $status != 0 ]] || status=2
            fi
        fi
        if ! "$retain_preflight_receipts" && [[ -n $scratch ]]; then
            rm -rf -- "$scratch" || { [[ $status != 0 ]] || status=2; }
        fi
        docker_client_cleanup || { [[ $status != 0 ]] || status=2; }
    else
        printf -v pending_diagnostic '%sCannot confirm acquisition preflight retirement; retained inputs at %q.\n' "$pending_diagnostic" "$scratch"
        [[ $status != 0 ]] || status=2
    fi
    if [[ -n $pending_diagnostic || ( $status == 0 && $print_command == false && -n $next_actions ) ]]; then
        report_status=0
        # The owned metadata and runtime readers are settled before public
        # reporting, and a full caller pipe cannot hold its cleanup forever.
        inference_native 'acquisition reporting' 2 stdio bash -c '
            printf "%s" "$1" >&2
            if [[ $2 == 0 && -n $3 ]]; then printf "%s\n" "$3" >&2; fi
        ' bash "$pending_diagnostic" "$status" "$next_actions" || report_status=$?
        [[ $status != 0 || $report_status == 0 ]] || status=$report_status
    fi
    exit "$status"
}
if "$print_command"; then
    if [[ $operation == run ]]; then
        source "$root/host/tools/acquisition_interface.sh"
        select_acquisition_interface
    fi
    docker_client_prepare print || { printf '%s' "$docker_client_diagnostic" >&2; exit 2; }
else
    trap cleanup EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    if [[ $operation == run ]]; then
        source "$root/host/tools/acquisition_interface.sh"
        select_acquisition_interface
    fi
    docker_client_prepare run || { pending_diagnostic+=$docker_client_diagnostic; exit 2; }
    scratch=$(umask 077; mktemp -d /tmp/acquisition-preflight.XXXXXXXX)
fi
if [[ $operation == require-images ]]; then require_images "$@"; exit; fi
if [[ $operation == download-proxy ]]; then
    next_actions='The shared Python base is available for acquisition and the offline checker build.'
    if "$print_command"; then printf '%q ' "${engine[@]}" image pull "$proxy_image"; printf '\n'; exit; fi
    download_status=0
    inference_native 'proxy image download' untimed stdio "${engine[@]}" image pull "$proxy_image" || download_status=$?
    pending_diagnostic+=$inference_diagnostic
    if [[ $download_status != 0 ]]; then
        printf -v pending_diagnostic '%sBase image download did not finish successfully (status %s). Docker may retain layers or finish the download after its client stops. Retry make checks/base-image/download; no image removal was attempted.\n' "$pending_diagnostic" "$download_status"
    fi
    exit "$download_status"
fi
if "$print_command"; then
    printf '%s\n' '# Verified prior-release bootstrap/acquisition owns gateway, relay and worker retirement.'
    printf '%q ' acquisition_scope --endpoint "$NMR_SELECTED_DOCKER_ENDPOINT" --bootstrap-image "$proxy_image"; printf '\n'
    printf '%q ' acquisition_set_route "$interface"; printf '\n'
    printf '%q ' acquisition_worker online STATUS "$image"; printf '%s\n' ' CREATE APPLICATION'
    printf 'CREATE=('; printf '%q ' "${create[@]}"; printf ')\nAPPLICATION=('; printf '%q ' "${application[@]}"; printf ')\n'
    exit
fi
require_images "$proxy_image" "$image"
# This checked-in source snapshot is refreshed explicitly from a qualified
# prior release; normal operations never extract, copy or download bootstrap.
python3 -I -S -B "$root/host/tools/acquisition_bootstrap.py" \
    "$root/bootstrap/acquisition" "$root/requirements/acquisition-owner-release.json"
source "$root/bootstrap/acquisition/acquisition_tool/owner.sh"
[[ -n $retired_receipt ]] || retired_receipt=$scratch/retired
run_worker() {
    local worker_status
    acquisition_set_route "$interface" || return "$?"
    acquisition_worker online worker_status "$image" create application || return "$?"
    return "$worker_status"
}
scope_started=true
acquisition_scope --endpoint "$NMR_SELECTED_DOCKER_ENDPOINT" --bootstrap-image "$proxy_image" \
    --retired-receipt "$retired_receipt" -- run_worker
