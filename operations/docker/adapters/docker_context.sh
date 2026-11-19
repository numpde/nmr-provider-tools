# Project Docker context records using the caller-selected client environment
docker_context_names() {
    local -n context_client=$1
    "${context_client[@]}" context ls --format '{{.Name}}'
}

docker_context_endpoint() {
    local -n context_client=$1
    "${context_client[@]}" context inspect --format '{{.Endpoints.docker.Host}}' "$2"
}
