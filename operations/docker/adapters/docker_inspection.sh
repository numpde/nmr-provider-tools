# Select the Docker fields that report a container's running state and exit code
docker_inspect_container_state() {
    local receipt=$1 container_ref=$2
    operation_metadata "$receipt" inspect --format '{{.State.Running}} {{.State.ExitCode}}' "$container_ref"
}
