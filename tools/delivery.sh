#!/usr/bin/env bash
# One explicit private state root owns its frozen Compose recipe and lifecycle.
# The operation helper owns transient containers; Compose owns durable services.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ $# == 1 && $1 =~ ^(install|up|down|status|logs|held|discard)$ ]] || exit 2
action=$1
uid=$(id -u) gid=$(id -g)
[[ $uid != 0 && $gid != 0 ]] || { printf '%s\n' 'Run shared delivery as its nonzero installing UID/GID.' >&2; exit 2; }
command -v jq >/dev/null || { printf '%s\n' 'Cannot operate shared delivery: install the jq host prerequisite, then retry this command.' >&2; exit 2; }
refuse() { printf 'Cannot %s shared delivery: %s\n' "$action" "$1" >&2; return 2; }
mountable_path() {
    [[ $1 == /* && $1 != / && $(realpath -m -- "$1") == "$1" && ! -L $1 && ! $1 =~ [[:cntrl:],] ]] \
        || { refuse 'paths must be canonical absolute paths below root without links or mount delimiters.'; return 2; }
}
[[ -v DELIVERY_ROOT_INPUT_ORIGIN && $DELIVERY_ROOT_INPUT_ORIGIN == 'command line' && -v DELIVERY_ROOT_INPUT && -n $DELIVERY_ROOT_INPUT ]] \
    || { refuse 'select DELIVERY_ROOT as the private installed state directory on the Make command line.'; exit 2; }
state_root=$DELIVERY_ROOT_INPUT
mountable_path "$state_root"
umask 077
if [[ $action == install && ! -e $state_root ]]; then mkdir -m 700 -- "$state_root"; fi
[[ -d $state_root && $(stat -c '%u:%a' -- "$state_root") == "$uid:700" ]] \
    || { refuse 'DELIVERY_ROOT must be a caller-owned mode 0700 directory.'; exit 2; }
exec {deployment_lock}< "$state_root"
flock --nonblock "$deployment_lock" || { refuse 'another command owns this state root; nothing was queued.'; exit 2; }
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
select_bundle() {
    [[ -L $state_root/current ]] || { refuse 'install its frozen configuration first.'; return 2; }
    local target
    target=$(readlink -- "$state_root/current")
    [[ $target =~ ^bundles/generation-[A-Za-z0-9_-]{1,64}$ ]] || return 2
    bundle=$state_root/$target
    mountable_path "$bundle"
    [[ -d $bundle && $(stat -c '%u:%a' -- "$bundle") == "$uid:500" \
        && -f $bundle/compose.json && ! -L $bundle/compose.json && $(stat -c '%u:%a' -- "$bundle/compose.json") == "$uid:400" ]] \
        || { refuse 'the installed recipe must remain sealed and caller-owned; reinstall through its owner.'; return 2; }
    project=$(jq -er '.name | select(type == "string")' "$bundle/compose.json")
    image=$(jq -er '.services.delivery.image | select(type == "string")' "$bundle/compose.json")
    [[ $project =~ ^[a-z0-9][a-z0-9_-]{0,62}$ && $image =~ ^sha256:[0-9a-f]{64}$ ]] || return 2
}
check_project() {
    checker_metadata "$operation_directory/project-containers" ps --all --quiet --no-trunc --filter "label=com.docker.compose.project=$project"
    local identifier
    local -a identifiers=()
    mapfile -t identifiers < "$operation_directory/project-containers"
    if [[ ${#identifiers[@]} != 0 ]]; then
        cmp -s "$operation_directory/project-containers" <(printf '%s\n' "${identifiers[@]}") || return 2
    fi
    for identifier in "${identifiers[@]}"; do
        [[ $identifier =~ ^[0-9a-f]{64}$ ]] || return 2
        if [[ $action == install ]]; then
            refuse 'remove the preceding project containers with their owning down command before installing; volumes must remain.'; return 2
        fi
        checker_metadata "$operation_directory/owner" inspect --type container --format '{{.Id}}|{{index .Config.Labels "org.nmr-provider-tools.root"}}|{{index .Config.Labels "com.docker.compose.project"}}' "$identifier"
        cmp -s "$operation_directory/owner" <(printf '%s|%s|%s\n' "$identifier" "$state_root" "$project") \
            || { refuse 'a project container belongs to another installation; use its original owner to retire it.'; return 2; }
    done
}
helper() {
    checker_run 120 --pull never --network none --read-only --user "$uid:$gid" --init --cap-drop ALL \
        --security-opt no-new-privileges:true --cpus 1 --memory 512m --memory-swap 512m --pids-limit 64 \
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m --env PROVIDER_CORE_IN_CONTAINER=1 \
        "${grants[@]}" --entrypoint /opt/provider-core/environment/bin/python "$image" -I -B "$@"
}

if [[ $action == install ]]; then
    previous_project=''
    if [[ -e $state_root/current && ! -L $state_root/current ]]; then
        refuse 'current is an unsupported control file; preserve it for inspection instead of replacing it.'; exit 2
    fi
    if [[ -L $state_root/current ]]; then select_bundle; previous_project=$project; fi
    [[ -v SHARED_RUNTIME_IMAGE_INPUT_ORIGIN && $SHARED_RUNTIME_IMAGE_INPUT_ORIGIN == 'command line' && -v SHARED_RUNTIME_IMAGE_INPUT ]] \
        || { refuse 'select SHARED_RUNTIME_IMAGE from delivery/image/build on the Make command line.'; exit 2; }
    image=$SHARED_RUNTIME_IMAGE_INPUT
    admit_image
    [[ -v DELIVERY_BINDINGS_INPUT_ORIGIN && $DELIVERY_BINDINGS_INPUT_ORIGIN == 'command line' && -v DELIVERY_BINDINGS_INPUT ]] \
        || { refuse 'select DELIVERY_BINDINGS as the complete host binding definition on the Make command line.'; exit 2; }
    definition=$DELIVERY_BINDINGS_INPUT
    mountable_path "$definition"
    [[ -f $definition && $(stat -c %u -- "$definition") == "$uid" && $(stat -c %s -- "$definition") -le 65536 \
        && $(stat -c %a -- "$definition") =~ ^[46]00$ ]] \
        || { refuse 'the binding definition must be caller-owned mode 0400/0600 and at most 65536 bytes.'; exit 2; }
    checker_call 'capture bounded binding definition' 30 stdio head -c 65537 -- "$definition" > "$operation_directory/definition.toml"
    grants=(--mount "type=bind,src=$operation_directory/definition.toml,dst=/input/definition.toml,readonly")
    helper /opt/provider-tools/delivery_deployment.py inspect-definition --definition /input/definition.toml > "$operation_directory/definition-identity"
    mapfile -d '' -t identity < "$operation_directory/definition-identity"
    [[ ${#identity[@]} == 3 && ${identity[0]} =~ ^[a-z0-9][a-z0-9_-]{0,62}$ && ${identity[1]} == "$uid" && ${identity[2]} =~ ^[1-9][0-9]*$ ]] \
        && cmp -s "$operation_directory/definition-identity" <(printf '%s\0' "${identity[@]}") \
        || { refuse 'the definition must select the installing UID and one explicit project/GID.'; exit 2; }
    project=${identity[0]}
    [[ -z $previous_project || $previous_project == "$project" ]] \
        || { refuse 'changing a project namespace requires a separate state root; preserve the preceding installation.'; exit 2; }
    check_project
    helper -m provider_core.deployment_binding paths --definition /input/definition.toml --all > "$operation_directory/paths"
    mapfile -d '' -t paths < "$operation_directory/paths"
    [[ ${#paths[@]} -gt 0 ]] && cmp -s "$operation_directory/paths" <(printf '%s\0' "${paths[@]}") || exit 2
    for path in "${paths[@]}"; do
        mountable_path "$path"
        [[ -f $path ]] || { refuse 'each selected API trust/resolver grant must be a regular file.'; exit 2; }
        grants+=(--mount "type=bind,src=$path,dst=/grants$path,readonly")
    done
    if [[ ! -e $state_root/bundles ]]; then mkdir -m 700 -- "$state_root/bundles"; fi
    [[ -d $state_root/bundles && ! -L $state_root/bundles && $(stat -c '%u:%a' -- "$state_root/bundles") == "$uid:700" ]] || exit 2
    bundle=$(mktemp -d "$state_root/bundles/generation-XXXXXXXX")
    grants+=(--mount "type=bind,src=$bundle,dst=$bundle")
    helper /opt/provider-tools/delivery_deployment.py prepare --definition /input/definition.toml --output "$bundle" --image "$image"
    [[ -f $bundle/compose.json && ! -L $bundle/compose.json ]] || exit 2
    # Runtime leaves remain writable by their same-UID API proxies. Seal only
    # frozen inputs and the recipe; never make a parent runtime leaf read-only.
    find "$bundle/bindings" -type f -exec chmod 400 -- {} +
    find "$bundle/bindings" -type d -exec chmod 500 -- {} +
    chmod 400 -- "$bundle/daemon.toml" "$bundle/compose.json"
    chmod 500 -- "$bundle"
    IFS= read -r generation_uuid < /proc/sys/kernel/random/uuid
    ln -s "bundles/${bundle##*/}" "$state_root/.current-${generation_uuid//-/}"
    mv -T -- "$state_root/.current-${generation_uuid//-/}" "$state_root/current"
    printf -v operation_success 'Installed shared delivery for project %s. Start with make delivery/deployment/up DELIVERY_ROOT=%q.\n' "$project" "$state_root"
    exit
fi
select_bundle
check_project
compose=(compose --env-file /dev/null --project-name "$project" --project-directory "$state_root" --file "$bundle/compose.json")
case "$action" in
    up)
        admit_image
        checker_call 'start shared delivery' 180 stdio "${engine[@]}" "${compose[@]}" up --detach --no-build --pull never --wait --wait-timeout 120
        ;;
    down)
        checker_call 'stop shared delivery and retain volumes' 120 stdio "${engine[@]}" "${compose[@]}" down --timeout 60
        checker_metadata "$operation_directory/remaining" ps --all --quiet --no-trunc --filter "label=com.docker.compose.project=$project"
        [[ ! -s $operation_directory/remaining ]] || { refuse 'project containers remain after down; retain configuration and inspect status.'; exit 2; }
        ;;
    status) checker_call 'shared delivery status' 30 stdio "${engine[@]}" "${compose[@]}" ps --all ;;
    logs) checker_call 'shared delivery logs' 30 stdio "${engine[@]}" "${compose[@]}" logs --no-color --tail 80 ;;
    held|discard)
        checker_metadata "$operation_directory/running" ps --quiet --no-trunc --filter "label=com.docker.compose.project=$project"
        [[ ! -s $operation_directory/running ]] || { refuse 'stop shared delivery before opening its single-writer queue for reconciliation.'; exit 2; }
        admit_image
        for variable in API_ORIGIN PROVIDER_REF; do
            origin_variable=${variable}_INPUT_ORIGIN value_variable=${variable}_INPUT
            [[ -v $origin_variable && ${!origin_variable} == 'command line' && -v $value_variable && -n ${!value_variable} ]] \
                || { refuse 'select API_ORIGIN and PROVIDER_REF explicitly on the Make command line.'; exit 2; }
        done
        arguments=(--list-held)
        if [[ $action == discard ]]; then
            for variable in EXECUTION_ATTEMPT_REF BODY_SHA256; do
                origin_variable=${variable}_INPUT_ORIGIN value_variable=${variable}_INPUT
                [[ -v $origin_variable && ${!origin_variable} == 'command line' && -v $value_variable && -n ${!value_variable} ]] \
                    || { refuse 'discard requires explicit EXECUTION_ATTEMPT_REF and BODY_SHA256 from the held metadata.'; exit 2; }
            done
            arguments=(--execution-attempt-ref "$EXECUTION_ATTEMPT_REF_INPUT" --body-sha256 "$BODY_SHA256_INPUT")
        fi
        grants=(--mount "type=bind,src=$bundle/daemon.toml,dst=/input/daemon.toml,readonly"
            --mount "type=volume,src=$project-queue,dst=/state,volume-nocopy")
        helper -m provider_core.delivery_discard --config /input/daemon.toml --api-origin "$API_ORIGIN_INPUT" \
            --provider-ref "$PROVIDER_REF_INPUT" "${arguments[@]}"
        ;;
esac
