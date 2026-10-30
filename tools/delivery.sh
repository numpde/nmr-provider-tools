#!/usr/bin/env bash
# Operator config/secrets are inputs. Docker owns queues, sockets and lifecycle.
set -euo pipefail
umask 077
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ $# == 1 && $1 =~ ^(up|down|status|logs|held|discard)$ ]] || exit 2
action=$1
uid=$(id -u) gid=$(id -g)
[[ $uid != 0 && $gid != 0 ]] || { printf '%s\n' 'Run shared delivery as its nonzero installing UID/GID.' >&2; exit 2; }
command -v jq >/dev/null || { printf '%s\n' 'Shared delivery requires the jq host prerequisite.' >&2; exit 2; }
refuse() { printf 'Cannot %s shared delivery: %s\n' "$action" "$1" >&2; return 2; }
mountable_path() {
    [[ $1 == /* && $1 != / && $(realpath -m -- "$1") == "$1" && ! -L $1 && ! $1 =~ [[:cntrl:],] ]] \
        || { refuse 'input paths must be canonical absolute files without links or mount delimiters.'; return 2; }
}
source "$root/tools/checker_operation.sh"
checker_begin

admit_image() {
    [[ $image =~ ^sha256:[0-9a-f]{64}$ ]] || { refuse 'select an immutable SHARED_RUNTIME_IMAGE from delivery/image/build.'; return 2; }
    checker_metadata "$operation_directory/image" image inspect --format '{{.Id}} {{index .Config.Labels "org.nmr-provider-tools.role"}} {{index .Config.Labels "org.nmr-provider-tools.inputs"}}' "$image"
    local observed
    IFS= read -r observed < "$operation_directory/image"
    [[ $observed =~ ^(sha256:[0-9a-f]{64})\ delivery\ ([0-9a-f]{64})$ && ${BASH_REMATCH[1]} == "$image" ]] \
        && cmp -s "$operation_directory/image" <(printf '%s\n' "$observed") \
        || { refuse 'the selected image is not an admitted shared delivery build.'; return 2; }
}
helper() {
    checker_run 120 --pull never --network none --read-only --user "$uid:$gid" --init --cap-drop ALL \
        --security-opt no-new-privileges:true --cpus 1 --memory 512m --memory-swap 512m --pids-limit 64 \
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m \
        --tmpfs "/run:rw,nosuid,nodev,noexec,size=64k,mode=0700,uid=$uid,gid=$gid" \
        --env PROVIDER_CORE_IN_CONTAINER=1 "${grants[@]}" \
        --entrypoint /opt/provider-core/environment/bin/python "$image" -I -B "$@"
}
project_containers() {
    checker_metadata "$operation_directory/project-containers" ps --all --quiet --no-trunc --filter "label=com.docker.compose.project=$project"
    identifiers=()
    daemon_identifiers=()
    other_identifiers=()
    mapfile -t identifiers < "$operation_directory/project-containers"
    if [[ ${#identifiers[@]} != 0 ]]; then
        cmp -s "$operation_directory/project-containers" <(printf '%s\n' "${identifiers[@]}") || return 2
    fi
    local identifier kind observed
    for identifier in "${identifiers[@]}"; do
        [[ $identifier =~ ^[0-9a-f]{64}$ ]] || return 2
        checker_metadata "$operation_directory/owner" inspect --type container --format '{{.Id}}|{{index .Config.Labels "org.nmr-provider-tools.delivery"}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}' "$identifier"
        IFS= read -r observed < "$operation_directory/owner"
        kind=${observed##*|}
        cmp -s "$operation_directory/owner" <(printf '%s|%s|%s|%s\n' "$identifier" "$project" "$project" "$kind") \
            || { refuse 'a project container belongs to another owner; retire it with that owner first.'; return 2; }
        if [[ $kind == delivery ]]; then
            daemon_identifiers+=("$identifier")
        elif [[ $kind == init || $kind =~ ^api-(init-)?[0-9a-f]{16}$ ]]; then
            other_identifiers+=("$identifier")
        else
            refuse 'a project container has an unsupported service identity; inspect it before mutation.'; return 2
        fi
    done
}
reserve_project() {
    # Docker atomically reserves this name. It is never started and has no
    # mounts or Compose-project label. Existing CID/operation cleanup owns it;
    # an old stopped reservation is not evidence that its caller has retired.
    if checker_create --name "nmr-provider-tools-operation-$project" --pull never --network none --read-only \
        --user "$uid:$gid" --cap-drop ALL --security-opt no-new-privileges:true --cpus 1 \
        --memory 64m --memory-swap 64m --pids-limit 16 --entrypoint /bin/true "$image"; then :; else
        refuse 'another command or an unresolved previous operation reserves this project; inspect its named operation container before retrying.'
        return 2
    fi
}
select_project() {
    [[ -v PROJECT_INPUT_ORIGIN && $PROJECT_INPUT_ORIGIN == 'command line' && -v PROJECT_INPUT \
        && $PROJECT_INPUT =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] \
        || { refuse 'select PROJECT explicitly on the Make command line.'; return 2; }
    project=$PROJECT_INPUT
}
select_shared_definition() {
    [[ -v SHARED_RUNTIME_IMAGE_INPUT_ORIGIN && $SHARED_RUNTIME_IMAGE_INPUT_ORIGIN == 'command line' && -v SHARED_RUNTIME_IMAGE_INPUT ]] \
        || { refuse 'select SHARED_RUNTIME_IMAGE from delivery/image/build.'; exit 2; }
    image=$SHARED_RUNTIME_IMAGE_INPUT
    admit_image
    [[ -v DELIVERY_BINDINGS_INPUT_ORIGIN && $DELIVERY_BINDINGS_INPUT_ORIGIN == 'command line' && -v DELIVERY_BINDINGS_INPUT ]] \
        || { refuse 'select DELIVERY_BINDINGS as the shared operator settings file.'; exit 2; }
    definition=$DELIVERY_BINDINGS_INPUT
    mountable_path "$definition"
    [[ -f $definition && $(stat -c %s -- "$definition") -le 65536 ]] \
        || { refuse 'the shared settings must be a regular file of at most 65536 bytes.'; exit 2; }
    checker_call 'capture shared settings for rendering' 30 stdio head -c 65537 -- "$definition" > "$operation_directory/shared.toml"
    grants=(--mount "type=bind,src=$operation_directory/shared.toml,dst=/input/shared.toml,readonly")
    helper /opt/provider-tools/delivery_deployment.py render-shared --definition /input/shared.toml --source "$definition" --image "$image" > "$operation_directory/shared.json"
    jq '.proxy' "$operation_directory/shared.json" > "$operation_directory/proxy.json"
    jq '.outbox' "$operation_directory/shared.json" > "$operation_directory/outbox.json"
    project=$(jq -er '.name' "$operation_directory/outbox.json")
    proxy_project=$(jq -er '.name' "$operation_directory/proxy.json")
    if [[ -v PROJECT_INPUT_ORIGIN && $PROJECT_INPUT_ORIGIN == 'command line' ]]; then
        [[ -v PROJECT_INPUT && $PROJECT_INPUT == "$project" ]] || { refuse 'PROJECT differs from the shared settings.'; exit 2; }
    fi
}

if [[ $action == up ]]; then
    select_shared_definition
    proxy_identity=$(jq -er '.services.proxy.user' "$operation_directory/proxy.json")
    settings_directory=$(dirname -- "$definition")
    settings_name=$(basename -- "$definition")
    checker_run 30 --pull never --network none --read-only --user "$proxy_identity" --cap-drop ALL \
        --security-opt no-new-privileges:true --cpus 1 --memory 64m --memory-swap 64m --pids-limit 16 \
        --mount "type=bind,src=$settings_directory,dst=/settings,readonly" \
        --entrypoint /opt/provider-core/environment/bin/python "$image" -I -B -c \
        'import sys; f = open(sys.argv[1], "rb"); f.read(1); f.close()' "/settings/$settings_name" \
        || { refuse 'the distinct proxy UID cannot read the shared settings directory and file; grant the shared group read access.'; exit 2; }
    reserve_project
    checker_metadata "$operation_directory/proxy-containers" ps --all --quiet --no-trunc --filter "label=com.docker.compose.project=$proxy_project"
    mapfile -t proxy_identifiers < "$operation_directory/proxy-containers"
    if [[ ${#proxy_identifiers[@]} == 0 ]]; then
        checker_call 'start the independent shared API proxy' 180 stdio "${engine[@]}" compose --env-file /dev/null \
            --project-name "$proxy_project" --project-directory / --file "$operation_directory/proxy.json" \
            up --detach --no-build --pull never --wait --wait-timeout 120
    else
        checker_metadata "$operation_directory/proxy-running" ps --quiet --no-trunc --filter "label=com.docker.compose.project=$proxy_project" \
            --filter 'label=com.docker.compose.service=proxy' --filter 'health=healthy'
        mapfile -t proxy_running < "$operation_directory/proxy-running"
        [[ ${#proxy_running[@]} == 1 ]] || { refuse 'the existing independent proxy is not uniquely healthy; leave it unchanged and inspect it.'; exit 2; }
        checker_metadata "$operation_directory/proxy-owner" inspect --type container --format '{{index .Config.Labels "org.nmr-provider-tools.delivery"}}|{{range .Mounts}}{{if eq .Destination "/route"}}{{.Name}}{{end}}{{end}}' "${proxy_running[0]}"
        IFS= read -r existing_proxy_owner < "$operation_directory/proxy-owner"
        [[ $existing_proxy_owner == "$proxy_project|$project-proxy-socket" ]] \
            || { refuse 'the existing proxy does not own the expected shared socket; leave it unchanged and inspect the deployment.'; exit 2; }
        checker_metadata "$operation_directory/proxy-grants" inspect --type container --format '[{{json .Config.User}},{{json .Config.Cmd}},{{json .HostConfig.NetworkMode}},{{range .Mounts}}{{if eq .Destination "/settings"}}{{json .Source}}{{end}}{{end}}]' "${proxy_running[0]}"
        jq -e --slurpfile actual "$operation_directory/proxy-grants" '
            (.services.proxy | [.user, .command, (.network_mode // ""),
                (.volumes[] | select(.target == "/settings") | .source)] ) as $expected |
            $actual[0][0] == $expected[0] and $actual[0][1] == $expected[1] and
            ($expected[2] == "" or $actual[0][2] == $expected[2]) and $actual[0][3] == $expected[3]
        ' "$operation_directory/proxy.json" > /dev/null \
            || { refuse 'the running proxy has different fixed grants; leave it unchanged and inspect the deployment.'; exit 2; }
        expected_resolver=$(jq -r '.services.proxy.volumes[] | select(.target == "/etc/resolv.conf") | .source' "$operation_directory/proxy.json")
        if [[ -n $expected_resolver ]]; then
            checker_metadata "$operation_directory/proxy-resolver" inspect --type container --format '{{range .Mounts}}{{if eq .Destination "/etc/resolv.conf"}}{{.Source}}{{end}}{{end}}' "${proxy_running[0]}"
            IFS= read -r existing_resolver < "$operation_directory/proxy-resolver"
            [[ $existing_resolver == "$expected_resolver" ]] \
                || { refuse 'the running proxy has a different resolver grant; leave it unchanged and inspect the deployment.'; exit 2; }
        fi
        expected_network=$(jq -r '.networks.api.name // empty' "$operation_directory/proxy.json")
        if [[ -n $expected_network ]]; then
            checker_metadata "$operation_directory/proxy-networks" inspect --type container --format '{{range .NetworkSettings.Networks}}{{.NetworkID}}{{"\n"}}{{end}}' "${proxy_running[0]}"
            [[ $(cat "$operation_directory/proxy-networks") == "$expected_network" ]] \
                || { refuse 'the running proxy uses a different network; leave it unchanged and inspect the deployment.'; exit 2; }
        fi
    fi
    project_containers
    if [[ ${#daemon_identifiers[@]} != 0 ]]; then
        checker_call 'settle the existing delivery owner' 120 stdio "${engine[@]}" stop --timeout 60 "${daemon_identifiers[@]}"
    fi
    if [[ ${#other_identifiers[@]} != 0 ]]; then
        checker_call 'stop old delivery-owned proxy roles' 60 stdio "${engine[@]}" stop --timeout 15 "${other_identifiers[@]}"
    fi
    if [[ ${#identifiers[@]} != 0 ]]; then
        checker_call 'retire old delivery containers and retain their volumes' 60 stdio "${engine[@]}" rm "${identifiers[@]}"
    fi
    checker_call 'start shared delivery without replacing its API proxy' 180 stdio "${engine[@]}" compose --env-file /dev/null \
        --project-name "$project" --project-directory / --file "$operation_directory/outbox.json" \
        up --detach --no-build --pull never --wait --wait-timeout 120
    printf -v operation_success 'Shared delivery and its independent API proxy are running for %s.\n' "$project"
    exit
fi

if [[ $action == held || $action == discard ]]; then
    select_shared_definition
    reserve_project
    project_containers
    [[ ${#identifiers[@]} == 0 ]] || { refuse 'run the owning down command before starting or opening its single-writer queue; volumes must remain.'; exit 2; }
        for variable in API_ORIGIN PROVIDER_REF; do
            origin_variable=${variable}_INPUT_ORIGIN value_variable=${variable}_INPUT
            [[ -v $origin_variable && ${!origin_variable} == 'command line' && -v $value_variable && -n ${!value_variable} ]] \
                || { refuse 'select API_ORIGIN and PROVIDER_REF explicitly.'; exit 2; }
        done
        arguments=(--list-held)
        if [[ $action == discard ]]; then
            for variable in EXECUTION_ATTEMPT_REF BODY_SHA256; do
                origin_variable=${variable}_INPUT_ORIGIN value_variable=${variable}_INPUT
                [[ -v $origin_variable && ${!origin_variable} == 'command line' && -v $value_variable && -n ${!value_variable} ]] \
                    || { refuse 'discard requires exact EXECUTION_ATTEMPT_REF and BODY_SHA256.'; exit 2; }
            done
            arguments=(--execution-attempt-ref "$EXECUTION_ATTEMPT_REF_INPUT" --body-sha256 "$BODY_SHA256_INPUT")
        fi
        grants=(--mount "type=bind,src=$operation_directory/shared.toml,dst=/input/shared.toml,readonly"
            --mount "type=volume,src=$project-queue,dst=/state,volume-nocopy")
        checker_metadata "$operation_directory/queue" volume inspect --format '{{.Name}}' "$project-queue"
        cmp -s "$operation_directory/queue" <(printf '%s\n' "$project-queue") \
            || { refuse 'the selected queue is absent or its name differs; no inspection helper was started.'; exit 2; }
        helper -m provider_core.delivery_discard --shared-config /input/shared.toml --state-directory /state \
            --api-origin "$API_ORIGIN_INPUT" --provider-ref "$PROVIDER_REF_INPUT" "${arguments[@]}"
    exit
fi

select_project
project_containers
if [[ $action == down && ${#identifiers[@]} != 0 ]]; then
    checker_metadata "$operation_directory/runtime-image" inspect --type container --format '{{.Image}}' "${identifiers[0]}"
    IFS= read -r image < "$operation_directory/runtime-image"
    admit_image
    reserve_project
    # Reobserve under the project reservation; mutation follows these exact IDs.
    project_containers
    if [[ ${#daemon_identifiers[@]} != 0 ]]; then
        checker_call 'settle the exact delivery daemon before its proxies' 120 stdio "${engine[@]}" stop --timeout 60 "${daemon_identifiers[@]}"
    fi
    if [[ ${#other_identifiers[@]} != 0 ]]; then
        checker_call 'stop exact shared API proxies and initializer' 60 stdio "${engine[@]}" stop --timeout 15 "${other_identifiers[@]}"
    fi
    if [[ ${#identifiers[@]} != 0 ]]; then
        checker_call 'remove exact stopped delivery containers and retain volumes' 60 stdio "${engine[@]}" rm "${identifiers[@]}"
    fi
    project_containers
    [[ ${#identifiers[@]} == 0 ]] || { refuse 'project containers remain; retain state and inspect status.'; exit 2; }
elif [[ $action == status ]]; then
    checker_call 'shared delivery status' 30 stdio "${engine[@]}" ps --all --no-trunc --filter "label=com.docker.compose.project=$project"
    checker_call 'independent shared API proxy status' 30 stdio "${engine[@]}" ps --all --no-trunc --filter "label=com.docker.compose.project=$project-proxy"
elif [[ $action == logs ]]; then
    for identifier in "${identifiers[@]}"; do
        checker_call 'bounded shared delivery logs' 30 stdio "${engine[@]}" logs --tail 80 "$identifier"
    done
fi
