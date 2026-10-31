#!/usr/bin/env bash
# Prepare one checker from explicit shared-cache inputs; normal checks stay offline.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ $# != 1 ]]; then printf '%s\n' 'Supply one checker preparation action from make help.' >&2; exit 2; fi
action=$1
case "$action" in
    base-download) exec bash "$root/tools/docker.sh" download-proxy ;;
    download|check|build|prepare) ;;
    *) printf 'Cannot prepare checker: unknown action %q. Use make help.\n' "$action" >&2; exit 2 ;;
esac
if [[ $root =~ [[:cntrl:],] ]]; then
    printf '%s\n' 'Cannot mount checker inputs: repository path contains a control character or comma.' >&2
    exit 2
fi
uid=$(id -u)
gid=$(id -g)
if [[ $uid == 0 || $gid == 0 ]]; then printf '%s\n' 'Checker preparation requires a nonzero caller UID and GID.' >&2; exit 2; fi
source "$root/tools/docker_context.sh"
select_docker_context
source "$root/tools/base_image.sh"
source "$root/tools/wheelhouse_path.sh"
select_wheelhouse
if [[ $action == download || $action == prepare ]]; then
    source "$root/tools/acquisition_interface.sh"
    select_acquisition_interface
    mkdir -p -- "$wheelhouse_directory"
else
    require_wheelhouse_pool
fi
source "$root/tools/checker_operation.sh"
checker_begin
worker_uid=$uid worker_gid=$gid
if [[ $action != check ]]; then
    checker_host_identity "$base_image" python3 "$uid" "$gid" || {
        printf '%s\n' 'Cannot map the wheelhouse owner into the selected engine.' >&2; exit 2;
    }
    worker_uid=$checker_host_uid worker_gid=$checker_host_gid
fi

worker=(--pull never --read-only --user "$worker_uid:$worker_gid" --cap-drop ALL --security-opt no-new-privileges:true
    --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m --workdir /workspace
    --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/workspace
    --mount "type=bind,src=$root/tools/checker_dependencies.py,dst=/workspace/tools/checker_dependencies.py,readonly"
    --mount "type=bind,src=$root/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly"
    --mount "type=bind,src=$root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly"
    --mount "type=bind,src=$root/requirements/checker-dependencies.json,dst=/workspace/requirements/checker-dependencies.json,readonly")
if [[ $action == download || $action == prepare ]]; then
    # The transport owns both networked containers and their cleanup. Do not
    # put it beneath a signal-forwarding process-group killer.
    source "$root/tools/inference_native.sh"
    if controller_native 'checker acquisition' untimed stdio bash "$root/tools/docker.sh" run \
        "${worker[@]}" --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse" \
        --entrypoint python3 "$base_image" -B -m tools.checker_dependencies download \
        --declaration /workspace/requirements/checker-dependencies.json --directory /wheelhouse; then :; else
        acquisition_status=$?
        printf '%s' "$inference_diagnostic" >&2
        exit "$acquisition_status"
    fi
    if ! "$inference_retirement_confirmed"; then exit 2; fi
    if [[ $action == download ]]; then exit; fi
fi

source "$root/tools/checker_inputs.sh"
if [[ $action == check ]]; then
    checker_run 60 --network none "${worker[@]}" --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly" \
        --entrypoint python3 "$base_image" -B -m tools.checker_dependencies check \
        --declaration /workspace/requirements/checker-dependencies.json --directory /wheelhouse
    exit
fi

expected=$(checker_fingerprint)
context=$operation_directory/context
mkdir -p -- "$context/dependencies"
for path in "${checker_input_paths[@]}"; do
    (cd -- "$root"; cp --parents --preserve=mode -- "$path" "$context/")
done
captured=$(root=$context checker_fingerprint)
if [[ $captured != "$expected" ]]; then
    operation_diagnostic+=$'Cannot build checker: source inputs changed during capture. Retry after source edits finish.\n'
    exit 2
fi
build_base_image=$(bash -eu -c 'source "$1"; printf "%s" "$base_image"' bash "$context/tools/base_image.sh")
# All build-mounted tools and declarations now come from the captured context.
checker_run 60 --pull never --network none --read-only --user "$checker_host_uid:$checker_host_gid" --cap-drop ALL --security-opt no-new-privileges:true \
    --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64 --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
    --workdir /workspace --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/workspace \
    --mount "type=bind,src=$context/tools,dst=/workspace/tools,readonly" \
    --mount "type=bind,src=$context/requirements,dst=/workspace/requirements,readonly" \
    --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly" \
    --mount "type=bind,src=$context/dependencies,dst=/capture" \
    --entrypoint python3 "$build_base_image" -B -m tools.checker_dependencies capture \
    --declaration /workspace/requirements/checker-dependencies.json --directory /wheelhouse --destination /capture

# Local client retirement cannot establish that BuildKit stopped reading.
# Retain captured inputs on every unsuccessful dispatched build/admission/tag.
retain_operation=true
checker_call 'offline checker build' 900 stdio "${engine[@]}" build --builder default --platform linux/amd64 \
    --network none --pull=false --build-arg "BASE_IMAGE=$build_base_image" --label "org.nmr-provider-tools.inputs=$captured" \
    --iidfile "$operation_directory/built-image" --file "$context/containers/checker/Dockerfile" "$context"
image=
if [[ -f $operation_directory/built-image && ! -L $operation_directory/built-image && $(stat -c %s -- "$operation_directory/built-image") -le 72 ]]; then
    if IFS= read -r -N 71 image < "$operation_directory/built-image"; then :; else image=; fi
fi
if [[ ! $image =~ ^sha256:[0-9a-f]{64}$ ]] \
    || ! { cmp -s "$operation_directory/built-image" <(printf '%s' "$image") || cmp -s "$operation_directory/built-image" <(printf '%s\n' "$image"); }; then
    operation_diagnostic+=$'Cannot admit checker build: BuildKit did not publish an immutable image ID.\n'
    exit 2
fi
checker_metadata "$operation_directory/built-label" image inspect --format '{{index .Config.Labels "org.nmr-provider-tools.inputs"}}' "$image"
if ! cmp -s "$operation_directory/built-label" <(printf '%s\n' "$captured"); then
    operation_diagnostic+=$'Cannot admit checker build: image input label differs from captured inputs.\n'
    exit 2
fi
checker_call 'checker image publication' 30 stdio "${engine[@]}" image tag "$image" "nmr-provider-tools/checker:$captured"
retain_operation=false
printf -v operation_success 'Prepared checker %s with input fingerprint %s.\n' "$image" "$captured"
