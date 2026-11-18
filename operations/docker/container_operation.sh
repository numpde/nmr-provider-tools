#!/usr/bin/env bash
# One private engine operation owns its mounts, client, and labeled containers.
# Cleanup ordering follows dapp32 tools/compose_cleanup.sh at 911d45d:
# retire owned resources, verify absence, then release mounted host inputs.

source "$root/operations/docker/docker_client.sh"
source "$root/host/tools/inference_native.sh"

operation_begin() {
    source "$root/operations/docker/image_lifecycle.sh"
    image_lifecycle_lock shared || return "$?"
    image_retire_after_success=false
    ordered_container_retirement=false
    operation_retire_order=()
    operation_acquisition_receipts=()
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
    trap operation_finish EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    if docker_client_prepare run; then :; else
        operation_diagnostic+=$docker_client_diagnostic
        return 2
    fi
}

operation_reserve_acquisition_receipt() {
    operation_acquisition_receipt=$operation_directory/acquisition-${#operation_acquisition_receipts[@]}.retired
    operation_acquisition_receipts+=("$operation_acquisition_receipt")
}


operation_can_dispatch() {
    local receipt
    if ! "$inference_retirement_confirmed"; then
        retain_operation=true
        operation_diagnostic+=$'Cannot dispatch another Docker client: previous local process retirement is unconfirmed.\n'
        return 2
    fi
    for receipt in "${operation_acquisition_receipts[@]}"; do
        if ! cmp -s "$receipt" <(printf 'retired\n'); then
            retain_operation=true
            operation_diagnostic+=$'Cannot dispatch another Docker client: acquisition retirement is unconfirmed.\n'
            return 2
        fi
    done
}

operation_call() {
    local status=0
    operation_can_dispatch || return "$?"
    if inference_native "$@"; then :; else status=$?; fi
    operation_diagnostic+=$inference_diagnostic
    if ! "$inference_retirement_confirmed"; then retain_operation=true; fi
    if [[ $inference_cancellation_status != 0 ]]; then return "$inference_cancellation_status"; fi
    return "$status"
}

operation_metadata() {
    local receipt=$1
    shift
    operation_call 'container metadata' 30 stdio bash -eu -c '
        receipt=$1; shift
        umask 077
        ulimit -f 4
        exec "$@" > "$receipt"
    ' bash "$receipt" "${engine[@]}" "$@" || return "$?"
    [[ -f $receipt && ! -L $receipt && $(stat -c %s -- "$receipt") -lt 4096 ]]
}

operation_create() {
    local identifier receipt cid
    operation_container=
    container_sequence=$((container_sequence + 1))
    receipt=$operation_directory/create-$container_sequence
    cid=$operation_directory/container-$container_sequence.cid
    create_pending=true
    operation_metadata "$receipt" create --label "$operation_label" --cidfile "$cid" "$@" || return "$?"
    if ! IFS= read -r identifier < "$receipt"; then return 2; fi
    if [[ ! $identifier =~ ^[0-9a-f]{64}$ || ! -f $cid || -L $cid ]] \
        || ! cmp -s "$receipt" <(printf '%s\n' "$identifier") \
        || ! { cmp -s "$cid" <(printf '%s' "$identifier") || cmp -s "$cid" <(printf '%s\n' "$identifier"); }; then
        printf -v operation_diagnostic '%sCannot start container: create output and private CID receipt disagree; retained inputs at %q.\n' "$operation_diagnostic" "$operation_directory"
        return 2
    fi
    create_pending=false
    operation_container=$identifier
}

operation_run() {
    local budget=$1
    shift
    operation_create "$@" || return "$?"
    operation_call 'container attachment' "$budget" stdio "${engine[@]}" start --attach "$operation_container"
}

# The published owner retires its probe and original native client before
# admitting values. Its receipt remains distinct from our own native-client fact.
operation_project_ids() {
    local identity_output=$1 identity_count=$2 identity_image=$3 identity_status=0
    shift 3
    operation_can_dispatch || return "$?"
    python3 -I -S -B "$root/bootstrap/acquisition/acquisition_tool/source_update.py" \
        verify --source-dir "$root/bootstrap/acquisition" || return "$?"
    source "$root/bootstrap/acquisition/acquisition_tool/owner.sh"
    operation_reserve_acquisition_receipt
    if acquisition_project_ids "$identity_output" "$identity_count" \
        "$NMR_SELECTED_DOCKER_ENDPOINT" "$identity_image" "$operation_acquisition_receipt" "$@"; then :; else
        identity_status=$?
    fi
    if ! cmp -s "$operation_acquisition_receipt" <(printf 'retired\n'); then
        retain_operation=true
        [[ $identity_status != 0 ]] || identity_status=2
    fi
    return "$identity_status"
}

# Inverse projections identify the container user that writes as the host caller.
operation_host_identity() {
    local image=$1 python=$2 host_uid=$3 host_gid=$4
    local -a values=()
    operation_project_ids values 2 "$image" \
        --interpreter "$python" --user-uid 0 --user-gid 0 \
        --memory-bytes 67108864 --pids-limit 16 --nano-cpus 1000000000 \
        --map uid inverse "$host_uid" --map gid inverse "$host_gid" || return "$?"
    operation_host_uid=${values[0]}
    operation_host_gid=${values[1]}
}

operation_teardown() {
    # Repeated caller signals cannot interrupt this bounded retirement attempt.
    # A failed/expired client is not proof of daemon retirement.
    timeout --signal=TERM --kill-after=2s 30s "${engine[@]}" "$@"
}

operation_finish() {
    local status=$? failed=false identifier receipt
    local -a identifiers=()
    trap - EXIT
    trap '' HUP INT TERM
    for receipt in "${operation_acquisition_receipts[@]}"; do
        if ! cmp -s "$receipt" <(printf 'retired\n'); then retain_operation=true; fi
    done
    if "$inference_retirement_confirmed" && [[ -n $docker_client_directory ]]; then
        if operation_teardown ps --all --quiet --no-trunc --filter "label=$operation_label" > "$operation_directory/containers"; then
            mapfile -t identifiers < "$operation_directory/containers"
            if [[ ${#identifiers[@]} != 0 ]] && ! cmp -s "$operation_directory/containers" <(printf '%s\n' "${identifiers[@]}"); then
                failed=true
                identifiers=()
            fi
            if [[ $ordered_container_retirement == true ]]; then
                # Refuse all removal if an unverified create may add a borrower,
                # or if the census contains any container outside the order.
                local expected seen
                local -A ordered=() actual=()
                if "$create_pending" || "$retain_operation"; then failed=true; fi
                for identifier in "${operation_retire_order[@]}"; do
                    [[ -z $identifier ]] && continue
                    if [[ ! $identifier =~ ^[0-9a-f]{64}$ || -n ${ordered[$identifier]:-} ]]; then
                        failed=true
                    else
                        ordered[$identifier]=1
                    fi
                done
                for identifier in "${identifiers[@]}"; do
                    if [[ ! $identifier =~ ^[0-9a-f]{64}$ || -z ${ordered[$identifier]:-} || -n ${actual[$identifier]:-} ]]; then
                        failed=true
                    else
                        actual[$identifier]=1
                    fi
                done
                if ! "$failed"; then
                    for identifier in "${operation_retire_order[@]}"; do
                        [[ -n $identifier && -n ${actual[$identifier]:-} ]] || continue
                        if ! operation_teardown rm --force --volumes "$identifier" > /dev/null; then
                            failed=true
                            break
                        fi
                        if ! operation_teardown ps --all --quiet --no-trunc --filter "id=$identifier" > "$operation_directory/ordered-residue" || [[ -s $operation_directory/ordered-residue ]]; then
                            failed=true
                            break
                        fi
                    done
                fi
            else
                for identifier in "${identifiers[@]}"; do
                    if [[ $identifier =~ ^[0-9a-f]{64}$ ]]; then
                        if operation_teardown rm --force --volumes "$identifier" > /dev/null; then :; else failed=true; fi
                    else
                        failed=true
                    fi
                done
            fi
            if operation_teardown ps --all --quiet --no-trunc --filter "label=$operation_label" > "$operation_directory/residue"; then
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
    # No automatic retirement may contend with this operation's own borrow.
    image_lifecycle_unlock || { if [[ $status == 0 ]]; then status=2; fi; }
    if [[ $status == 0 && $image_retire_after_success == true ]]; then
        if bash "$root/operations/docker/image_retirement.sh" --automatic >&2; then :; else
            printf '%s\n' 'Build completed; automatic image retirement failed. Retry make packages/images/retire with the same Docker context.' >&2
        fi
    fi
    if [[ -n $operation_diagnostic ]]; then printf '%s' "$operation_diagnostic" >&2; fi
    if [[ $status == 0 && -n $operation_success ]]; then printf '%s' "$operation_success"; fi
    exit "$status"
}
