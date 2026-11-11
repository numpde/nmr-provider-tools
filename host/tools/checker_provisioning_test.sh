#!/usr/bin/env bash
# Exercise cold acquisition and offline admission through the real owners.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
if [[ ! -v DOCKER_CONTEXT_INPUT_ORIGIN || $DOCKER_CONTEXT_INPUT_ORIGIN != 'command line' || ! -v DOCKER_CONTEXT_INPUT || -z $DOCKER_CONTEXT_INPUT ]]; then
    printf '%s\n' 'Select DOCKER_CONTEXT explicitly for live checker provisioning: make tests/repository/provisioning/live DOCKER_CONTEXT=<name> NETWORK_INTERFACE=<system|interface>.' >&2
    exit 2
fi
source "$root/host/tools/docker_context.sh"
select_docker_context
source "$root/host/tools/acquisition_interface.sh"
select_acquisition_interface
source "$root/host/tools/checker_operation.sh"
checker_begin
cache="$operation_directory/cache \$literal 'quoted'"
mkdir -m 755 -- "$cache"

# Child owners must confirm retirement before the parent releases cache bytes.
# A failed or interrupted child leaves those bytes available for inspection.
retain_operation=true
for action in download check; do
    selected_interface=$interface
    if [[ $action == check ]]; then selected_interface=missing-interface; fi
    # Make owns nested cleanup; cancellation must wait for that owner to return.
    if controller_native "qualify checker $action" untimed stdio \
        make --no-print-directory -C "$root" "checks/dependencies/$action" \
        "DOCKER_CONTEXT=$DOCKER_CONTEXT_INPUT" "WHEELHOUSE=$cache" "NETWORK_INTERFACE=$selected_interface"; then :; else
        status=$?
        operation_diagnostic+=$inference_diagnostic
        exit "$status"
    fi
    if ! "$inference_retirement_confirmed"; then exit 2; fi
done
retain_operation=false
printf -v operation_success 'Real cold checker acquisition and offline cache admission passed.\n'
