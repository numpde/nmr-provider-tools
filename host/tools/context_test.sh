#!/usr/bin/env bash
# Qualify real context selection and endpoint propagation with private client state.
# The selected engine runs one read-only cache check; no image build or download
# is part of this lane. The existing checker owner retires its temporary workers.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/host/tools/docker_context.sh"
select_docker_context
source "$root/host/tools/wheelhouse_path.sh"
select_wheelhouse
require_wheelhouse_pool
source "$root/host/tools/checker_operation.sh"
checker_begin

context_token=${operation_directory##*.}
selected_context=sdk-selected-$context_token
ambient_context=sdk-ambient-$context_token
private_config=$docker_client_directory/config

# Contexts are client metadata in the operation-owned config. The ambient
# context points at an absent socket so accidental fallback cannot pass.
checker_call 'create selected test context' 30 stdio "${engine[@]}" context create "$selected_context" \
    --docker "host=$NMR_SELECTED_DOCKER_ENDPOINT"
checker_call 'create ambient test context' 30 stdio "${engine[@]}" context create "$ambient_context" \
    --docker "host=unix://$operation_directory/absent.sock"
checker_call 'select ambient test context' 30 stdio "${engine[@]}" context use "$ambient_context"

checker_call 'resolve real test context' 30 stdio env -u NMR_SELECTED_DOCKER_ENDPOINT \
    DOCKER_CONFIG="$private_config" DOCKER_HOST="unix://$operation_directory/poison.sock" \
    DOCKER_CONTEXT="$ambient_context" DOCKER_CONTEXT_INPUT="$selected_context" DOCKER_CONTEXT_INPUT_ORIGIN='command line' \
    bash "$root/host/tools/docker_context.sh" --endpoint > "$operation_directory/endpoint"
if ! cmp -s "$operation_directory/endpoint" <(printf '%s\n' "$NMR_SELECTED_DOCKER_ENDPOINT"); then
    printf '%s\n' 'Cannot qualify Docker context selection: the resolved endpoint differs from the selected engine.' >&2
    exit 2
fi

# Start a fresh Make invocation with contradictory ambient selectors. Its
# preview and real cache worker must share the explicitly selected endpoint.
retain_operation=true
if controller_native 'qualify real context handoff' untimed stdio env \
    -u NMR_SELECTED_DOCKER_ENDPOINT -u MAKEFLAGS -u MAKEOVERRIDES -u MFLAGS -u MAKELEVEL \
    DOCKER_CONFIG="$private_config" DOCKER_HOST="unix://$operation_directory/poison.sock" DOCKER_CONTEXT="$ambient_context" \
    make --no-print-directory -C "$root" tests/repository/command checks/dependencies/check \
    "DOCKER_CONTEXT=$selected_context" "WHEELHOUSE=$wheelhouse_directory" NETWORK_INTERFACE=missing-interface; then :; else
    status=$?
    operation_diagnostic+=$inference_diagnostic
    exit "$status"
fi
if ! "$inference_retirement_confirmed"; then exit 2; fi
retain_operation=false

checker_call 'restore private default context' 30 stdio "${engine[@]}" context use default
checker_call 'remove owned test contexts' 30 stdio "${engine[@]}" context rm "$selected_context" "$ambient_context"
printf -v operation_success 'Real Docker context selection and read-only cache handoff passed.\n'
