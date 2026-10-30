#!/usr/bin/env bash
# Retire the former delivery-owned socket volumes after their borrowers stop.
set -euo pipefail
umask 077
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ $# == 2 && $1 =~ ^[a-z0-9][a-z0-9_-]{0,62}$ && $2 =~ ^sha256:[0-9a-f]{64}$ ]] || {
    printf '%s\n' 'Cannot retire old delivery sockets: select the project and immutable shared delivery image.' >&2
    exit 2
}
project=$1 image=$2 uid=$(id -u) gid=$(id -g)
[[ $uid != 0 && $gid != 0 ]] || { printf '%s\n' 'Cannot retire old delivery sockets as root; use the installing account.' >&2; exit 2; }
source "$root/tools/checker_operation.sh"
checker_begin
removal_requested=false
report_failure() {
    printf 'Cannot retire old delivery sockets: %s\n' "$1" >&2
    if "$removal_requested"; then
        printf '%s\n' 'A volume removal was requested; one or more old socket volumes may already be removed. Recheck the project before retrying.' >&2
    fi
}
retain_uncertain_removal() {
    trap - EXIT
    printf 'Removal may still be running; retained operation %q and client %q, including its project reservation. Inspect before retrying.\n' \
        "$operation_directory" "$docker_client_directory" >&2
    if [[ -n $operation_diagnostic ]]; then printf '%s' "$operation_diagnostic" >&2; fi
}
interrupted() {
    local status=$1
    report_failure 'the operation was interrupted.'
    if "$removal_requested"; then retain_uncertain_removal; fi
    exit "$status"
}
trap 'interrupted 129' HUP
trap 'interrupted 130' INT
trap 'interrupted 143' TERM
checker_metadata "$operation_directory/image" image inspect --format '{{.Id}} {{index .Config.Labels "org.nmr-provider-tools.role"}} {{index .Config.Labels "org.nmr-provider-tools.inputs"}}' "$image" || { status=$?; report_failure 'the shared delivery image could not be inspected.'; exit "$status"; }
IFS= read -r observed < "$operation_directory/image"
[[ $observed =~ ^(sha256:[0-9a-f]{64})\ delivery\ ([0-9a-f]{64})$ && ${BASH_REMATCH[1]} == "$image" ]] \
    && cmp -s "$operation_directory/image" <(printf '%s\n' "$observed") \
    || { report_failure 'the image is not an admitted shared delivery build.'; exit 2; }
checker_create --name "nmr-provider-tools-operation-$project" --pull never --network none --read-only \
    --user "$uid:$gid" --cap-drop ALL --security-opt no-new-privileges:true --cpus 1 \
    --memory 64m --memory-swap 64m --pids-limit 16 --entrypoint /bin/true "$image" \
    || { status=$?; report_failure 'another project operation may still own the reservation.'; exit "$status"; }
inventory() {
    local destination=$1 kind
    local -a found
    for kind in ipc route; do
        checker_metadata "$operation_directory/inventory-$kind" volume ls --filter "name=^${project}-${kind}-" --format '{{.Name}}' \
            || return "$?"
        mapfile -t found < "$operation_directory/inventory-$kind"
        if (( ${#found[@]} )); then
            cmp -s "$operation_directory/inventory-$kind" <(printf '%s\n' "${found[@]}") || return 2
        fi
    done
    cat "$operation_directory/inventory-ipc" "$operation_directory/inventory-route" > "$destination"
}
inventory "$operation_directory/volumes" \
    || { status=$?; report_failure 'the volume inventory could not be confirmed.'; exit "$status"; }
mapfile -t listed < "$operation_directory/volumes"
if (( ${#listed[@]} )); then
    cmp -s "$operation_directory/volumes" <(printf '%s\n' "${listed[@]}") \
        || { report_failure 'the volume inventory was malformed.'; exit 2; }
fi
candidates=()
for volume in "${listed[@]}"; do
    if [[ $volume =~ ^${project}-(ipc|route)-[0-9a-f]{64}$ ]]; then candidates+=("$volume"); fi
done
inspect_candidate() {
    local volume=$1 receipt=$2 borrower=$3 owner logical driver options created extra
    checker_metadata "$receipt" volume inspect --format '{{.Name}}|{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.volume"}}|{{.Driver}}|{{json .Options}}|{{.CreatedAt}}' "$volume" \
        || { local status=$?; report_failure "ownership of $volume could not be inspected."; return "$status"; }
    extra=
    IFS='|' read -r observed owner logical driver options created extra < "$receipt"
    [[ $observed == "$volume" && $owner == "$project" && $logical == "$volume" && $driver == local \
        && ( $options == null || $options == '{}' ) && -n $created && -z $extra ]] \
        && cmp -s "$receipt" <(printf '%s|%s|%s|%s|%s|%s\n' "$observed" "$owner" "$logical" "$driver" "$options" "$created") \
        || { report_failure "ownership or storage settings of $volume could not be confirmed."; return 2; }
    checker_metadata "$borrower" ps --all --quiet --no-trunc --filter "volume=$volume" \
        || { local status=$?; report_failure "borrowers of $volume could not be inspected."; return "$status"; }
    [[ ! -s $borrower ]] || { report_failure "$volume is still borrowed by a container; stop or remove the borrower first."; return 2; }
}
for volume in "${candidates[@]}"; do
    inspect_candidate "$volume" "$operation_directory/receipt-$volume" "$operation_directory/borrowers" || exit "$?"
done
for volume in "${candidates[@]}"; do
    inspect_candidate "$volume" "$operation_directory/recheck" "$operation_directory/borrowers" || exit "$?"
    cmp -s "$operation_directory/receipt-$volume" "$operation_directory/recheck" \
        || { report_failure "$volume changed during inspection."; exit 2; }
    removal_requested=true
    checker_call 'retire old delivery socket volume' 60 stdio "${engine[@]}" volume rm -- "$volume" \
        || { status=$?; report_failure "removal of $volume was not confirmed."; retain_uncertain_removal; exit "$status"; }
done
inventory "$operation_directory/remaining" \
    || { status=$?; report_failure 'the final volume inventory could not be confirmed.'; exit "$status"; }
mapfile -t remaining < "$operation_directory/remaining"
if (( ${#remaining[@]} )); then
    cmp -s "$operation_directory/remaining" <(printf '%s\n' "${remaining[@]}") \
        || { report_failure 'the final volume inventory was malformed.'; exit 2; }
fi
for volume in "${remaining[@]}"; do
    [[ ! $volume =~ ^${project}-(ipc|route)-[0-9a-f]{64}$ ]] \
        || { report_failure "$volume remains in the project inventory."; exit 2; }
done
operation_success='Old delivery socket volumes are retired; the queue and shared sockets were not targeted.'$'\n'
