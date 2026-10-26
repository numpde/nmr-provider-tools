#!/usr/bin/env bash
# Interface-bound HTTP transport and explicit default-route proxy bootstrap.
# Transport precedent: explicit interface, loopback proxy, host-network clients.
# https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/tools/docker.sh
# Containerizing the proxy and owning both container lifecycles are local changes.
set -euo pipefail

readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
source "$repository_root/tools/base_image.sh"
readonly proxy_image=$base_image
readonly resolver_source='/run/systemd/resolve/resolv.conf'
# Metadata retains the donor 30-second admission budget.
# Attachment has no operation deadline; local retirement is not engine cleanup.
readonly metadata_timeout=30
print_command=false
if [[ $# -gt 0 && "$1" == --print ]]; then print_command=true; shift; fi
next_actions=
# Exec handoffs may supply inert operator text. The acquisition owner publishes
# it only after successful retirement and cleanup, outside worker/JSON stdout.
if [[ $# -gt 0 && $1 == --success-note ]]; then
    if [[ $# -lt 2 ]]; then printf '%s\n' 'Supply the operator note after --success-note.' >&2; exit 2; fi
    next_actions=$2
    shift 2
fi

require_interface() {
    source "$repository_root/tools/acquisition_interface.sh"
    select_acquisition_interface
}

if [[ $# == 0 ]]; then
    printf '%s\n' 'Usage: tools/docker.sh [--print] {download-proxy|check-interface|require-images IMAGE...|run DOCKER_RUN_ARGS...}' >&2
    exit 2
fi
operation=$1
case "$operation" in
    download-proxy)
        if [[ $# != 1 ]]; then
            printf '%s\n' 'Usage: tools/docker.sh [--print] download-proxy' >&2
            exit 2
        fi
        ;;
    check-interface) require_interface; exit ;;
    require-images) shift; if [[ $# == 0 ]]; then exit 0; fi ;;
    run) ;;
    *) printf '%s\n' 'Usage: tools/docker.sh [--print] {download-proxy|check-interface|require-images IMAGE...|run DOCKER_RUN_ARGS...}' >&2; exit 2 ;;
esac
if [[ "$operation" == run ]]; then
    require_interface
    shift
    readonly caller_uid="$(id -u)"
    readonly caller_gid="$(id -g)"
    if [[ "$caller_uid" == 0 || "$caller_gid" == 0 ]]; then
        printf '%s\n' 'Acquisition requires a nonzero host UID and GID.' >&2
        exit 2
    fi
    # Grant the same configured upstream file used by the host proxy precedent.
    # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L141
    resolver_mount=()
    if [[ -e "$resolver_source" || -L "$resolver_source" ]]; then
        if [[ ! -f "$resolver_source" || -L "$resolver_source" || ! -r "$resolver_source" ]]; then
            printf '%s\n' 'Cannot start acquisition proxy: configured upstream resolver must be a readable regular file without a symlink: /run/systemd/resolve/resolv.conf.' >&2
            exit 2
        fi
        resolver_mount=(--mount "type=bind,src=$resolver_source,dst=/run/systemd/resolve/resolv.conf,readonly")
    fi
fi

source "$repository_root/tools/docker_client.sh"
source "$repository_root/tools/inference_native.sh"
docker_client_directory=
scratch=
proxy_id=
worker_id=
pending_diagnostic=
retain_preflight_receipts=false
inference_diagnostic=
inference_retirement_confirmed=true
inference_cancellation_status=0
caller_cancellation=0

container_id_matches_receipt() {
    local identifier=$1 receipt=$2
    # Native effects require one exact ID, never a name from create output.
    # Startup also requires agreement with create output. During cancellation,
    # the private receipt can precede stdout assignment and still owns cleanup.
    # Nonempty disagreeing identities never authorize removal.
    # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/selected-image-build.py#L343-L370
    [[ "$identifier" =~ ^[0-9a-f]{64}$ && -f "$receipt" && ! -L "$receipt" ]] && {
        # Compare bytes: shell substitution discards NULs and trailing newlines.
        cmp -s "$receipt" <(printf '%s' "$identifier") ||
            cmp -s "$receipt" <(printf '%s\n' "$identifier")
    }
}

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

cancellation_stdout_allows_identity() {
    local identifier=$1 output=$2
    # Cancellation can precede create stdout, but cannot erase contradictory
    # bytes already published there. CID-only recovery requires absent or
    # provably empty stdout; every nonempty receipt must agree exactly.
    if [[ ! -e "$output" && ! -L "$output" ]]; then return 0; fi
    if [[ ! -f "$output" || -L "$output" ]]; then return 1; fi
    if cmp -s -- "$output" /dev/null; then return 0; fi
    container_id_matches_receipt "$identifier" "$output"
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
    local status=$? cleanup_failed=false removal_status client_status=0 scratch_status=0 cancelled=0
    local native_cancellation=$inference_cancellation_status cleanup_cancellation=0 candidate
    trap - EXIT
    trap 'cancelled=129' HUP
    trap 'cancelled=130' INT
    trap 'cancelled=143' TERM
    # The same native fact gates EVERY next owner and host removal. Engine-side
    # uncertainty is independent: retained CID receipts never establish that a
    # local group retired, and a local join never establishes container removal.
    if "$inference_retirement_confirmed"; then
        if [[ -n "$scratch" && ( $native_cancellation != 0 || $caller_cancellation != 0 ) ]]; then
            # A complete EOF-terminated CID can precede create stdout. Recover
            # only after checking the raw stdout too; known disagreement must
            # not be discarded merely because startup left its binding empty.
            if [[ -z "$worker_id" && -f "$scratch/worker.cid" && ! -L "$scratch/worker.cid" ]]; then
                candidate=
                if IFS= read -r -N 64 candidate < "$scratch/worker.cid" \
                    && container_id_matches_receipt "$candidate" "$scratch/worker.cid" \
                    && cancellation_stdout_allows_identity "$candidate" "$scratch/worker-create/stdout"; then
                    worker_id=$candidate
                fi
            fi
            if [[ -z "$proxy_id" && -f "$scratch/proxy.cid" && ! -L "$scratch/proxy.cid" ]]; then
                candidate=
                if IFS= read -r -N 64 candidate < "$scratch/proxy.cid" \
                    && container_id_matches_receipt "$candidate" "$scratch/proxy.cid" \
                    && cancellation_stdout_allows_identity "$candidate" "$scratch/proxy-create/stdout"; then
                    proxy_id=$candidate
                fi
            fi
        fi
        if [[ -n "$scratch" && ( -e "$scratch/worker.cid" || -L "$scratch/worker.cid" || -n "$worker_id" ) ]]; then
            if ! container_id_matches_receipt "$worker_id" "$scratch/worker.cid"; then
                printf -v pending_diagnostic '%sCannot confirm acquisition worker removal: its container ID was not admitted; evidence remains in %s.\n' "$pending_diagnostic" "$scratch"
                cleanup_failed=true
            else
                if inference_native 'acquisition worker removal' "$metadata_timeout" discard "${engine[@]}" rm --force "$worker_id"; then removal_status=0; else removal_status=$?; fi
                pending_diagnostic+=$inference_diagnostic
                if [[ $inference_cancellation_status != 0 ]]; then cleanup_cancellation=$inference_cancellation_status; fi
                if [[ "$removal_status" != 0 ]] || ! "$inference_retirement_confirmed"; then
                    printf -v pending_diagnostic '%sCannot confirm acquisition worker removal; container ID is recorded in %s/worker.cid.\n' "$pending_diagnostic" "$scratch"
                    cleanup_failed=true
                fi
            fi
        fi
        if "$inference_retirement_confirmed" && [[ -n "$scratch" && ( -e "$scratch/proxy.cid" || -L "$scratch/proxy.cid" || -n "$proxy_id" ) ]]; then
            if ! container_id_matches_receipt "$proxy_id" "$scratch/proxy.cid"; then
                printf -v pending_diagnostic '%sCannot confirm proxy removal: its container ID was not admitted; evidence remains in %s.\n' "$pending_diagnostic" "$scratch"
                cleanup_failed=true
            else
                if inference_native 'acquisition proxy removal' "$metadata_timeout" discard "${engine[@]}" rm --force "$proxy_id"; then removal_status=0; else removal_status=$?; fi
                pending_diagnostic+=$inference_diagnostic
                if [[ $inference_cancellation_status != 0 ]]; then cleanup_cancellation=$inference_cancellation_status; fi
                if [[ "$removal_status" != 0 ]] || ! "$inference_retirement_confirmed"; then
                    printf -v pending_diagnostic '%sCannot confirm proxy removal; container ID is recorded in %s/proxy.cid.\n' "$pending_diagnostic" "$scratch"
                    cleanup_failed=true
                fi
            fi
        fi
        if "$cleanup_failed" && [[ "$status" == 0 ]]; then status=1; fi
        if "$inference_retirement_confirmed"; then
            # Keep failed metadata/engine-removal evidence. A worker's ordinary
            # exit alone needs no new diagnostic and does not retain metadata.
            if ! "$cleanup_failed" && ! "$retain_preflight_receipts" && [[ -z "$pending_diagnostic" && -n "$scratch" ]]; then
                if rm -rf -- "$scratch" 2>/dev/null; then scratch=; else
                    scratch_status=$?
                    if [[ "$status" == 0 ]]; then status=1; fi
                fi
            fi
            if docker_client_cleanup 2>/dev/null; then :; else
                client_status=$?
                if [[ "$status" == 0 ]]; then status=$client_status; fi
            fi
        fi
    fi
    trap 'caller_cancellation=129; exit 129' HUP
    trap 'caller_cancellation=130; exit 130' INT
    trap 'caller_cancellation=143; exit 143' TERM
    # Actual cancellation finishes permitted cleanup without creating a NEW
    # publisher that could need another signal on an unread stderr pipe. Unlike
    # the former synchronous cleanup, even a CID refusal is silent in this case;
    # ordinary noncancelled failures still publish their cause and retained path.
    if [[ $cancelled != 0 ]]; then exit "$cancelled"; fi
    if [[ $cleanup_cancellation != 0 ]]; then exit "$cleanup_cancellation"; fi
    if [[ $caller_cancellation != 0 ]]; then exit "$caller_cancellation"; fi
    if [[ $native_cancellation != 0 ]]; then exit "$native_cancellation"; fi
    if ! "$inference_retirement_confirmed"; then
        # No further helper can overwrite the unknown retirement fact. This
        # controlled direct report retains the existing blocked-stderr limit.
        printf '%s' "$pending_diagnostic" >&2
        printf 'Cannot confirm acquisition native client retirement; private state retained at %q and %q.\n' "$scratch" "$docker_client_directory" >&2
    elif [[ -n "$pending_diagnostic$docker_client_directory" || $scratch_status != 0 ]]; then
        if inference_native 'acquisition cleanup reporting' untimed stdio bash -eu -c '
            printf "%s" "$1" >&2
            if [[ -n "$2" ]]; then
                printf "Cannot confirm acquisition Docker client configuration removal; attempted path %q (status %s).\n" "$2" "$3" >&2
            fi
            if [[ -n "$4" ]]; then
                if [[ "$5" == 0 ]]; then
                    printf "Acquisition private receipts retained at %q.\n" "$4" >&2
                else
                    printf "Cannot confirm acquisition scratch removal; attempted path %q (status %s).\n" "$4" "$5" >&2
                fi
            fi
        ' bash "$pending_diagnostic" "$docker_client_directory" "$client_status" "$scratch" "$scratch_status"; then :; else
            local report_status=$?
            if [[ $inference_cancellation_status != 0 ]]; then status=$inference_cancellation_status
            elif [[ "$status" == 0 ]]; then status=$report_status; fi
        fi
    fi
    if [[ $status == 0 && $print_command == false && -n $next_actions ]]; then
        if inference_native 'next-action reporting' untimed stdio bash -eu -c \
            'printf "%s\n" "$1" >&2' bash "$next_actions"; then :; else status=$?; fi
    fi
    # Reporting owns its new fact. Exit immediately; no fallback or cleanup.
    exit "$status"
}

if "$print_command"; then
    docker_client_prepare print
else
    trap cleanup EXIT
    trap 'caller_cancellation=129; exit 129' HUP
    trap 'caller_cancellation=130; exit 130' INT
    trap 'caller_cancellation=143; exit 143' TERM
    if docker_client_prepare run; then :; else
        preparation_status=$?
        pending_diagnostic+=$docker_client_diagnostic
        exit "$preparation_status"
    fi
    scratch="$(mktemp -d /tmp/nmrnet-proxy.XXXXXXXX)"
fi
if [[ "$operation" == require-images ]]; then require_images "$@"; exit; fi
if [[ "$operation" == download-proxy ]]; then
    next_actions=$'\nThe shared Python base is available for the acquisition proxy and offline checker build.'
    # Cold start cannot use the proxy image it is acquiring. This explicit lane
    # uses daemon routing; NETWORK_INTERFACE binds only subsequent acquisition.
    # Digest pulls: https://docs.docker.com/reference/cli/docker/image/pull/#pull-an-image-by-digest-immutable-identifier
    # Daemon network settings: https://docs.docker.com/engine/daemon/proxy/
    if "$print_command"; then
        printf '%q ' "${engine[@]}" image pull "$proxy_image"; printf '\n'
        exit
    fi
    if inference_native 'proxy image download' untimed stdio "${engine[@]}" image pull "$proxy_image"; then download_status=0; else download_status=$?; fi
    pending_diagnostic+=$inference_diagnostic
    if [[ "$download_status" != 0 ]]; then
        printf -v pending_diagnostic '%sBase image download did not finish successfully (status %s). Docker may retain layers or finish the download after its client stops. Retry make checks/base-image/download; no image removal was attempted.\n' \
            "$pending_diagnostic" "$download_status"
    fi
    exit "$download_status"
fi
if "$print_command"; then scratch=/tmp/nmrnet-proxy.PRINT; else
    require_images "$proxy_image"
    mkdir -m 700 -- "$scratch/ready"
fi
# The proxy shares the tools namespace with its guard; neither source is writable.
proxy=("${engine[@]}" create --cidfile "$scratch/proxy.cid" --pull never --network host --read-only
    --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1 --env PROVIDER_CORE_IN_CONTAINER=1 --workdir /workspace
    --user "$caller_uid:$caller_gid" --cap-drop ALL --security-opt no-new-privileges:true
    --cpus 1 --memory 128m --memory-swap 128m --pids-limit 64
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m
    "${resolver_mount[@]}"
    --mount "type=bind,src=$repository_root/packages/provider-core/src/provider_core/bound_http_proxy.py,dst=/workspace/provider_core/bound_http_proxy.py,readonly"
    --mount "type=bind,src=$repository_root/packages/provider-core/src/provider_core/container_guard.py,dst=/workspace/provider_core/container_guard.py,readonly"
    --mount "type=bind,src=$scratch/ready,dst=/ready"
    "$proxy_image" python3 -B -m provider_core.bound_http_proxy --interface "$interface" --port 0 --ready-file /ready/port)
if "$print_command"; then
    printf '%q ' "${proxy[@]}"; printf '\n'
    printf '%q ' "${engine[@]}" start PROXY_ID; printf '\n'
    proxy_port=PORT
else
    mkdir -m 700 -- "$scratch/proxy-create"
    # The parent owns these bytes; the native retirement fact never crosses a
    # command-substitution subshell. Both output streams have independent caps.
    if inference_native 'acquisition proxy creation' "$metadata_timeout" stdio bash -eu -c '
        directory=$1; shift
        umask 077
        ulimit -f 4
        exec "$@" > "$directory/stdout" 2> "$directory/stderr"
    ' bash "$scratch/proxy-create" "${proxy[@]}"; then create_status=0; else create_status=$?; fi
    pending_diagnostic+=$inference_diagnostic
    if ! "$inference_retirement_confirmed"; then exit "$create_status"; fi
    candidate=
    if private_receipt_below_limit "$scratch/proxy-create/stdout" \
        && IFS= read -r -N 64 candidate < "$scratch/proxy-create/stdout" \
        && container_id_matches_receipt "$candidate" "$scratch/proxy-create/stdout" \
        && container_id_matches_receipt "$candidate" "$scratch/proxy.cid"; then
        proxy_id=$candidate
    fi
    # A failed create can still have published its exact stdout/CID pair. That
    # pair owns cleanup, while the failed status still forbids starting it.
    if [[ "$create_status" != 0 ]]; then
        printf -v pending_diagnostic '%sCannot start acquisition proxy: its create client failed (status %s); private receipts remain in %s/proxy-create.\n' "$pending_diagnostic" "$create_status" "$scratch"
        exit "$create_status"
    fi
    # Diagnostic completeness gates startup, not the independently admitted
    # stdout/CID pair's authority to remove this known container during cleanup.
    if [[ -z "$proxy_id" ]] || ! private_receipt_below_limit "$scratch/proxy-create/stderr"; then
        printf -v pending_diagnostic '%s%s\n' "$pending_diagnostic" 'Cannot start acquisition proxy: create must return one full lowercase container ID matching its CID file; private output receipts must be regular sub-limit files.'
        exit 2
    fi
    if inference_native 'acquisition proxy start' "$metadata_timeout" discard "${engine[@]}" start "$proxy_id"; then start_status=0; else start_status=$?; fi
    pending_diagnostic+=$inference_diagnostic
    if ! "$inference_retirement_confirmed" || [[ "$start_status" != 0 ]]; then exit "$start_status"; fi
    mkdir -m 700 -- "$scratch/readiness"
    # ONE owner bounds the entire existing fixed poll loop, including a stalled
    # inspection/log request. No nested timeout or per-poll native owner exists.
    if inference_native 'acquisition proxy readiness' "$metadata_timeout" stdio bash -eu -c '
        directory=$1 ready=$2 proxy=$3; shift 3
        umask 077
        ulimit -f 4
        collect_proxy_logs() {
            if "$@" logs "$proxy" > "$directory/logs.stdout" 2> "$directory/logs.stderr"; then
                log_status=0
            else
                log_status=$?
            fi
            printf "%s\n" "$log_status" > "$directory/log-status"
        }
        for _ in {1..100}; do
            [[ -s "$ready" ]] && exit 0
            if "$@" inspect --format "{{.State.Running}}" "$proxy" > "$directory/state" 2> "$directory/inspect.stderr"; then
                inspect_status=0
            else
                inspect_status=$?
            fi
            if [[ "$inspect_status" != 0 || ! -f "$directory/state" || -L "$directory/state" ]] \
                || ! cmp -s -- "$directory/state" <(printf "true\n"); then
                # Only a successful canonical false receipt proves stopped.
                # Failed inspection or malformed state establishes uncertainty.
                readiness_failure=4
                if [[ "$inspect_status" == 0 && -f "$directory/state" && ! -L "$directory/state" ]] \
                    && cmp -s -- "$directory/state" <(printf "false\n"); then
                    readiness_failure=2
                fi
                collect_proxy_logs "$@"
                exit "$readiness_failure"
            fi
            sleep 0.05
        done
        if [[ ! -s "$ready" ]]; then
            collect_proxy_logs "$@"
            exit 3
        fi
    ' bash "$scratch/readiness" "$scratch/ready/port" "$proxy_id" "${engine[@]}"; then readiness_status=0; else readiness_status=$?; fi
    pending_diagnostic+=$inference_diagnostic
    if ! "$inference_retirement_confirmed"; then exit "$readiness_status"; fi
    case "$readiness_status" in
        0) ;;
        2|3|4)
            if [[ "$readiness_status" == 2 ]]; then
                printf -v pending_diagnostic '%s%s\n' "$pending_diagnostic" 'Cannot start acquisition: the interface-bound proxy stopped before becoming ready.'
            elif [[ "$readiness_status" == 3 ]]; then
                printf -v pending_diagnostic '%s%s\n' "$pending_diagnostic" 'Cannot start acquisition: the interface-bound proxy did not become ready.'
            else
                printf -v pending_diagnostic '%sCannot start acquisition: the interface-bound proxy running state could not be verified; private receipts remain in %s/readiness.\n' "$pending_diagnostic" "$scratch"
            fi
            log_status=
            if private_receipt_below_limit "$scratch/readiness/log-status" \
                && IFS= read -r log_status < "$scratch/readiness/log-status" \
                && [[ "$log_status" =~ ^(0|[1-9][0-9]{0,2})$ && "$log_status" -le 255 ]] \
                && cmp -s -- "$scratch/readiness/log-status" <(printf '%s\n' "$log_status"); then
                if [[ "$log_status" != 0 ]]; then
                    printf -v pending_diagnostic '%sCannot retrieve acquisition proxy logs (status %s); private receipts remain in %s/readiness.\n' "$pending_diagnostic" "$log_status" "$scratch"
                fi
            else
                printf -v pending_diagnostic '%s%s\n' "$pending_diagnostic" 'Cannot admit the private acquisition proxy log-retrieval status receipt.'
            fi
            exit 2
            ;;
        *)
            printf -v pending_diagnostic '%sCannot start acquisition: proxy readiness metadata failed (status %s); private receipts remain in %s/readiness.\n' "$pending_diagnostic" "$readiness_status" "$scratch"
            exit "$readiness_status"
            ;;
    esac
    # The fixed proxy publishes one decimal port plus LF. Reject unsupported
    # types/extra bytes before a parent read outside the held metadata owner.
    proxy_port=
    if ! private_receipt_below_limit "$scratch/ready/port" \
        || ! IFS= read -r proxy_port < "$scratch/ready/port" \
        || [[ ! "$proxy_port" =~ ^[1-9][0-9]{0,4}$ || "$proxy_port" -gt 65535 ]] \
        || ! cmp -s -- "$scratch/ready/port" <(printf '%s\n' "$proxy_port"); then
        printf -v pending_diagnostic '%s%s\n' "$pending_diagnostic" 'Cannot start acquisition: the proxy readiness file contains an invalid port.'
        exit 2
    fi
fi
proxy_url="http://127.0.0.1:$proxy_port"
worker=("${engine[@]}" create --cidfile "$scratch/worker.cid" --network host
    --env "HTTP_PROXY=$proxy_url" --env "HTTPS_PROXY=$proxy_url"
    --env "http_proxy=$proxy_url" --env "https_proxy=$proxy_url"
    --env NO_PROXY= --env no_proxy= --env ALL_PROXY= --env all_proxy= "$@")
if "$print_command"; then
    printf '%q ' "${worker[@]}"; printf '\n'
    printf '%q ' "${engine[@]}" start --attach WORKER_ID; printf '\n'
else
    mkdir -m 700 -- "$scratch/worker-create"
    if inference_native 'acquisition worker creation' "$metadata_timeout" stdio bash -eu -c '
        directory=$1; shift
        umask 077
        ulimit -f 4
        exec "$@" > "$directory/stdout" 2> "$directory/stderr"
    ' bash "$scratch/worker-create" "${worker[@]}"; then create_status=0; else create_status=$?; fi
    pending_diagnostic+=$inference_diagnostic
    if ! "$inference_retirement_confirmed"; then exit "$create_status"; fi
    candidate=
    if private_receipt_below_limit "$scratch/worker-create/stdout" \
        && IFS= read -r -N 64 candidate < "$scratch/worker-create/stdout" \
        && container_id_matches_receipt "$candidate" "$scratch/worker-create/stdout" \
        && container_id_matches_receipt "$candidate" "$scratch/worker.cid"; then
        worker_id=$candidate
    fi
    if [[ "$create_status" != 0 ]]; then
        printf -v pending_diagnostic '%sCannot start acquisition worker: its create client failed (status %s); private receipts remain in %s/worker-create.\n' "$pending_diagnostic" "$create_status" "$scratch"
        exit "$create_status"
    fi
    if [[ -z "$worker_id" ]] || ! private_receipt_below_limit "$scratch/worker-create/stderr"; then
        printf -v pending_diagnostic '%s%s\n' "$pending_diagnostic" 'Cannot start acquisition worker: create must return one full lowercase container ID matching its CID file; private output receipts must be regular sub-limit files.'
        exit 2
    fi
    # Preserve the worker’s live streams and duration. The shared owner offers
    # TERM before escalation and joins its held group even if CID cleanup fails.
    if inference_native 'acquisition worker attachment' untimed stdio "${engine[@]}" start --attach "$worker_id"; then status=0; else status=$?; fi
    pending_diagnostic+=$inference_diagnostic
    exit "$status"
fi
