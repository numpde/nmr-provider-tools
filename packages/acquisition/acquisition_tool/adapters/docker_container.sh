# Select the Docker inspect fields that report a worker's process state
_acq_inspect_container_state() {
    local receipt=$1 container_ref=$2
    _acq_capture "$receipt" inspect --format '{{.State.Running}} {{.State.ExitCode}}' "$container_ref"
}
