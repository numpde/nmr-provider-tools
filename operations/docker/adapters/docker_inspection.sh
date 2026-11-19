# Project Docker container and image inspection records
docker_inspect_container_state() {
    local receipt=$1 container_ref=$2
    operation_metadata "$receipt" inspect --format '{{.State.Running}} {{.State.ExitCode}}' "$container_ref"
}

docker_inspect_image_id_and_label() {
    local receipt=$1 image_ref=$2 label=$3
    operation_metadata "$receipt" image inspect --format "{{.Id}} {{index .Config.Labels \"$label\"}}" "$image_ref"
}

docker_inspect_image_label() {
    local receipt=$1 image_ref=$2 label=$3
    operation_metadata "$receipt" image inspect --format "{{index .Config.Labels \"$label\"}}" "$image_ref"
}

docker_inspect_image_id_and_platform() {
    local receipt=$1 image_ref=$2
    operation_metadata "$receipt" image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$image_ref"
}

docker_inspect_image_references() {
    local receipt=$1 image_ref=$2
    operation_metadata "$receipt" image inspect --format '{{range .RepoTags}}{{println .}}{{end}}{{range .RepoDigests}}{{println .}}{{end}}' "$image_ref"
}

docker_inspect_image_id() {
    local receipt=$1 image_ref=$2
    operation_metadata "$receipt" image inspect --format '{{.Id}}' "$image_ref"
}
