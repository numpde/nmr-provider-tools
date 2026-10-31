#!/usr/bin/env bash
# One private engine operation owns its mounts, client, and labeled containers.
# Cleanup ordering follows dapp32 tools/compose_cleanup.sh at 911d45d:
# retire owned resources, verify absence, then release mounted host inputs.

source "$root/tools/docker_client.sh"
source "$root/tools/inference_native.sh"

checker_begin() {
    operation_directory=$(umask 077; mktemp -d /tmp/nmr-provider-tools.XXXXXXXX) || return "$?"
    operation_label="org.nmr-provider-tools.operation=${operation_directory##*/}"
    docker_client_directory=
    inference_retirement_confirmed=true
    create_pending=false
    retain_operation=false
    container_sequence=0
    operation_container=
    operation_diagnostic=
    operation_success=
    trap checker_finish EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    if docker_client_prepare run; then :; else
        operation_diagnostic+=$docker_client_diagnostic
        return 2
    fi
}

checker_call() {
    local status=0
    if ! "$inference_retirement_confirmed"; then
        retain_operation=true
        operation_diagnostic+=$'Cannot dispatch another checker client: previous local process retirement is unconfirmed.\n'
        return 2
    fi
    if inference_native "$@"; then :; else status=$?; fi
    operation_diagnostic+=$inference_diagnostic
    if ! "$inference_retirement_confirmed"; then retain_operation=true; fi
    if [[ $inference_cancellation_status != 0 ]]; then return "$inference_cancellation_status"; fi
    return "$status"
}

checker_metadata() {
    local receipt=$1
    shift
    checker_call 'checker metadata' 30 stdio bash -eu -c '
        receipt=$1; shift
        umask 077
        ulimit -f 4
        exec "$@" > "$receipt"
    ' bash "$receipt" "${engine[@]}" "$@" || return "$?"
    [[ -f $receipt && ! -L $receipt && $(stat -c %s -- "$receipt") -lt 4096 ]]
}

checker_create() {
    local identifier receipt cid
    operation_container=
    container_sequence=$((container_sequence + 1))
    receipt=$operation_directory/create-$container_sequence
    cid=$operation_directory/container-$container_sequence.cid
    create_pending=true
    checker_metadata "$receipt" create --label "$operation_label" --cidfile "$cid" "$@" || return "$?"
    if ! IFS= read -r identifier < "$receipt"; then return 2; fi
    if [[ ! $identifier =~ ^[0-9a-f]{64}$ || ! -f $cid || -L $cid ]] \
        || ! cmp -s "$receipt" <(printf '%s\n' "$identifier") \
        || ! { cmp -s "$cid" <(printf '%s' "$identifier") || cmp -s "$cid" <(printf '%s\n' "$identifier"); }; then
        printf -v operation_diagnostic '%sCannot start checker container: create output and private CID receipt disagree; retained inputs at %q.\n' "$operation_diagnostic" "$operation_directory"
        return 2
    fi
    create_pending=false
    operation_container=$identifier
}

checker_run() {
    local budget=$1
    shift
    checker_create "$@" || return "$?"
    checker_call 'checker container attachment' "$budget" stdio "${engine[@]}" start --attach "$operation_container"
}

# Find the container identity that writes as the calling host user. The probe
# uses the same selected engine as the worker and carries no host mounts.
checker_host_identity() {
    local image=$1 python=$2 host_uid=$3 host_gid=$4 pid running
    checker_create --pull never --network none --read-only --user 0:0 --cap-drop ALL \
        --security-opt no-new-privileges:true --cpus 1 --memory 64m --memory-swap 64m --pids-limit 16 \
        --entrypoint "$python" "$image" -I -B -c 'import time; time.sleep(30)' || return 2
    checker_metadata "$operation_directory/identity-start" start "$operation_container" || return 2
    checker_metadata "$operation_directory/identity-pid" inspect --format '{{.State.Pid}} {{.State.Running}}' "$operation_container" || return 2
    read -r pid running < "$operation_directory/identity-pid" || return 2
    [[ $pid =~ ^[1-9][0-9]*$ && $running == true ]] || return 2
    checker_host_uid=$(awk -v id="$host_uid" '$2 <= id && id < $2 + $3 {printf "%.0f\n", $1 + id - $2; found=1; exit} END {if (!found) exit 1}' "/proc/$pid/uid_map") || return 2
    checker_host_gid=$(awk -v id="$host_gid" '$2 <= id && id < $2 + $3 {printf "%.0f\n", $1 + id - $2; found=1; exit} END {if (!found) exit 1}' "/proc/$pid/gid_map") || return 2
}

checker_teardown() {
    # Repeated caller signals cannot interrupt this bounded retirement attempt.
    # A failed/expired client is not proof of daemon retirement.
    timeout --signal=TERM --kill-after=2s 30s "${engine[@]}" "$@"
}

checker_finish() {
    local status=$? failed=false identifier
    local -a identifiers=()
    trap - EXIT
    trap '' HUP INT TERM
    if "$inference_retirement_confirmed" && [[ -n $docker_client_directory ]]; then
        if checker_teardown ps --all --quiet --no-trunc --filter "label=$operation_label" > "$operation_directory/containers"; then
            mapfile -t identifiers < "$operation_directory/containers"
            if [[ ${#identifiers[@]} != 0 ]] && ! cmp -s "$operation_directory/containers" <(printf '%s\n' "${identifiers[@]}"); then
                failed=true
                identifiers=()
            fi
            for identifier in "${identifiers[@]}"; do
                if [[ $identifier =~ ^[0-9a-f]{64}$ ]]; then
                    if checker_teardown rm --force --volumes "$identifier" > /dev/null; then :; else failed=true; fi
                else
                    failed=true
                fi
            done
            if checker_teardown ps --all --quiet --no-trunc --filter "label=$operation_label" > "$operation_directory/residue"; then
                if [[ -s $operation_directory/residue ]]; then failed=true; fi
            else
                failed=true
            fi
        else
            failed=true
        fi
    else
        failed=true
    fi
    if "$create_pending" || "$retain_operation" || "$failed"; then
        printf 'Checker cleanup could not release all inputs safely. Retained operation %q and client %q; inspect owned containers with label %q.\n' \
            "$operation_directory" "$docker_client_directory" "$operation_label" >&2
        if [[ $status == 0 ]]; then status=2; fi
    else
        if rm -rf -- "$operation_directory" && docker_client_cleanup; then :; else
            if [[ $status == 0 ]]; then status=2; fi
        fi
    fi
    if [[ -n $operation_diagnostic ]]; then printf '%s' "$operation_diagnostic" >&2; fi
    if [[ $status == 0 && -n $operation_success ]]; then printf '%s' "$operation_success"; fi
    exit "$status"
}
