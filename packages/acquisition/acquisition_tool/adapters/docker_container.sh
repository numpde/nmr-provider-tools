# Select the Docker inspect fields used by acquisition
_acq_inspect_container_state() {
    local receipt=$1 container_ref=$2
    _acq_capture "$receipt" inspect --format '{{.State.Running}} {{.State.ExitCode}}' "$container_ref"
}

_acq_inspect_container_running() {
    local container_ref=$1
    shift
    "$@" inspect --format '{{.State.Running}}' "$container_ref"
}

_acq_inspect_container_network() {
    local receipt=$1 container_ref=$2
    _acq_capture "$receipt" inspect --format '{{.HostConfig.NetworkMode}}' "$container_ref"
}

_acq_inspect_container_network_running() {
    local receipt=$1 container_ref=$2
    _acq_capture "$receipt" inspect --format '{{.HostConfig.NetworkMode}} {{.State.Running}}' "$container_ref"
}
