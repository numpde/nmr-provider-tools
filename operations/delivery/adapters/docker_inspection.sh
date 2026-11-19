#!/usr/bin/env bash
# Select the Docker fields used by delivery deployment and retirement

delivery_inspect_image() {
    operation_metadata "$1" image inspect --format '{{.Id}} {{index .Config.Labels "org.nmr-provider-tools.role"}} {{index .Config.Labels "org.nmr-provider-tools.inputs"}}' "$2"
}

delivery_inspect_recipe_image() {
    operation_metadata "$1" image inspect --format '{{.Id}} {{index .Config.Labels "org.nmr-provider-tools.recipe"}}' "$2"
}

delivery_list_project_containers() {
    operation_metadata "$1" ps --all --quiet --no-trunc --filter "label=com.docker.compose.project=$2"
}

delivery_list_project_service_containers() {
    operation_metadata "$1" ps --all --quiet --no-trunc \
        --filter "label=com.docker.compose.project=$2" --filter "label=com.docker.compose.service=$3"
}

delivery_list_healthy_service_containers() {
    operation_metadata "$1" ps --quiet --no-trunc --filter "label=com.docker.compose.project=$2" \
        --filter "label=com.docker.compose.service=$3" --filter 'health=healthy'
}

delivery_inspect_container_owner() {
    operation_metadata "$1" inspect --type container --format '{{.Id}}|{{index .Config.Labels "org.nmr-provider-tools.delivery"}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}' "$2"
}

delivery_inspect_container_process() {
    operation_metadata "$1" inspect --type container --format '{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}' "$2"
}

delivery_inspect_container_image() {
    operation_metadata "$1" inspect --type container --format '{{.Image}}' "$2"
}

delivery_inspect_running_proxy_socket() {
    operation_metadata "$1" inspect --type container --format \
        '{{.Id}}|{{.State.Running}}|{{index .Config.Labels "org.nmr-provider-tools.delivery"}}|{{range .Mounts}}{{if eq .Destination "/route"}}{{.Name}}{{end}}{{end}}' "$2"
}

delivery_inspect_proxy_socket_owner() {
    operation_metadata "$1" inspect --type container --format \
        '{{index .Config.Labels "org.nmr-provider-tools.delivery"}}|{{range .Mounts}}{{if eq .Destination "/route"}}{{.Name}}{{end}}{{end}}' "$2"
}

delivery_inspect_proxy_image_socket() {
    operation_metadata "$1" inspect --type container --format \
        '{{.Image}}|{{range .Mounts}}{{if eq .Destination "/route"}}{{.Name}}{{end}}{{end}}' "$2"
}

delivery_inspect_proxy_restart_grants() {
    operation_metadata "$1" inspect --type container --format \
        '[{{json .Config.User}},{{json .Config.Cmd}},{{json .HostConfig.NetworkMode}},{{range .Mounts}}{{if eq .Destination "/settings"}}{{json .Source}}{{end}}{{end}}]' "$2"
}

delivery_inspect_proxy_reuse_grants() {
    operation_metadata "$1" inspect --type container --format \
        '[{{json .Config.User}},{{json .Config.Cmd}},{{json .HostConfig.NetworkMode}},{{range .Mounts}}{{if eq .Destination "/settings"}}{{json .Source}}{{end}}{{end}},{{json .HostConfig.GroupAdd}}]' "$2"
}

delivery_inspect_proxy_resolver() {
    operation_metadata "$1" inspect --type container --format \
        '{{range .Mounts}}{{if eq .Destination "/etc/resolv.conf"}}{{.Source}}{{end}}{{end}}' "$2"
}

delivery_inspect_container_networks() {
    operation_metadata "$1" inspect --type container --format '{{range .NetworkSettings.Networks}}{{.NetworkID}}{{"\n"}}{{end}}' "$2"
}

delivery_inspect_volume_name() {
    operation_metadata "$1" volume inspect --format '{{.Name}}' "$2"
}

delivery_list_volumes_with_prefix() {
    operation_metadata "$1" volume ls --filter "name=^$2" --format '{{.Name}}'
}

delivery_inspect_volume_owner() {
    operation_metadata "$1" volume inspect --format \
        '{{.Name}}|{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.volume"}}|{{.Driver}}|{{json .Options}}|{{.CreatedAt}}' "$2"
}

delivery_list_volume_borrowers() {
    operation_metadata "$1" ps --all --quiet --no-trunc --filter "volume=$2"
}
