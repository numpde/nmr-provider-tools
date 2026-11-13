#!/usr/bin/env bash
# Prepare one checker from explicit shared-cache inputs; normal checks stay offline.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
if [[ $# != 1 ]]; then printf '%s\n' 'Supply one checker preparation action from make help.' >&2; exit 2; fi
action=$1
case "$action" in
    base-download) exec bash "$root/host/tools/docker.sh" download-proxy ;;
    bootstrap-build|download|check|build|prepare) ;;
    *) printf 'Cannot prepare checker: unknown action %q. Use make help.\n' "$action" >&2; exit 2 ;;
esac
if [[ $root =~ [[:cntrl:],] ]]; then
    printf '%s\n' 'Cannot mount checker inputs: repository path contains a control character or comma.' >&2
    exit 2
fi
uid=$(id -u)
gid=$(id -g)
if [[ $uid == 0 || $gid == 0 ]]; then printf '%s\n' 'Checker preparation requires a nonzero caller UID and GID.' >&2; exit 2; fi
source "$root/host/tools/docker_context.sh"
select_docker_context
source "$root/host/tools/base_image.sh"
source "$root/host/tools/wheelhouse_path.sh"
if [[ $action != bootstrap-build ]]; then select_wheelhouse; fi
if [[ $action == download || $action == prepare ]]; then
    source "$root/host/tools/acquisition_interface.sh"
    select_acquisition_interface
    mkdir -p -- "$wheelhouse_directory"
elif [[ $action != bootstrap-build ]]; then
    require_wheelhouse_pool
fi
source "$root/host/tools/checker_operation.sh"
checker_begin
source "$root/host/tools/checker_inputs.sh"
bootstrap_fingerprint=$(checker_fingerprint bootstrap)
worker=(--pull never --read-only --cap-drop ALL --security-opt no-new-privileges:true
    --workdir /opt/provider-tools --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1
    --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m)
declaration=/opt/provider-tools/requirements/checker-dependencies.json
installer=/opt/provider-tools/installer/release.json

if [[ $action == check ]]; then
    checker_admit_image "$bootstrap_fingerprint" bootstrap
    checker_run 60 --network none --user "$uid:$gid" "${worker[@]}" \
        --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly" \
        --entrypoint python3 "$checker_image" -B -m tools.checker_dependencies check \
        --declaration "$declaration" --directory /wheelhouse --installer "$installer"
    exit
fi

if [[ $action == download ]]; then
    checker_admit_image "$bootstrap_fingerprint" bootstrap
    bootstrap_image=$checker_image
fi

if [[ $action != download ]]; then
expected=$(checker_fingerprint checker)
context=$operation_directory/context
mkdir -p -- "$context/dependencies"
for path in "${checker_input_paths[@]}"; do
    (cd -- "$root"; cp --parents --preserve=mode -- "$path" "$context/")
done
captured=$(root=$context checker_fingerprint checker)
captured_bootstrap=$(root=$context checker_fingerprint bootstrap)
if [[ $captured != "$expected" || $captured_bootstrap != "$bootstrap_fingerprint" ]]; then
    operation_diagnostic+=$'Cannot build checker: source inputs changed during capture. Retry after source edits finish.\n'
    exit 2
fi
build_base_image=$(bash -eu -c 'source "$1"; printf "%s" "$base_image"' bash "$context/host/tools/base_image.sh")

build_checker_target() {
    local target=$1 fingerprint=$2 image= receipt=$operation_directory/built-$1
    # Retain the captured build context if daemon-side completion is uncertain.
    retain_operation=true
    checker_call "offline $target image build" 900 stdio "${engine[@]}" build --builder default --platform linux/amd64 \
        --network none --pull=false --target "$target" --build-arg "BASE_IMAGE=$build_base_image" \
        --label "org.nmr-provider-tools.inputs=$fingerprint" --iidfile "$receipt" \
        --file "$context/containers/checker/Dockerfile" "$context"
    retain_operation=false
    if [[ -f $receipt && ! -L $receipt && $(stat -c %s -- "$receipt") -le 72 ]]; then
        if IFS= read -r -N 71 image < "$receipt"; then :; else image=; fi
    fi
    if [[ ! $image =~ ^sha256:[0-9a-f]{64}$ ]] \
        || ! { cmp -s "$receipt" <(printf '%s' "$image") || cmp -s "$receipt" <(printf '%s\n' "$image"); }; then
        operation_diagnostic+=$'Cannot admit checker build: BuildKit did not publish an immutable image ID.\n'
        return 2
    fi
    checker_metadata "$operation_directory/built-label" image inspect --format '{{index .Config.Labels "org.nmr-provider-tools.inputs"}}' "$image"
    if ! cmp -s "$operation_directory/built-label" <(printf '%s\n' "$fingerprint"); then
        operation_diagnostic+=$'Cannot admit checker build: image input label differs from captured inputs.\n'
        return 2
    fi
    local tag
    tag=$(checker_image_tag "$target" "$fingerprint") || return "$?"
    checker_call "$target image publication" 30 stdio "${engine[@]}" image tag "$image" "$tag"
    checker_image=$image
}

if [[ $action == bootstrap-build || $action == prepare ]]; then
    build_checker_target bootstrap "$bootstrap_fingerprint"
    bootstrap_image=$checker_image
else
    checker_admit_image "$bootstrap_fingerprint" bootstrap
    bootstrap_image=$checker_image
fi
if [[ $action == bootstrap-build ]]; then
    image_retire_after_success=true
    printf -v operation_success 'Prepared bootstrap image %s.\n' "$bootstrap_image"
    exit
fi
fi
if [[ $action == download || $action == prepare ]]; then
    # The transport owns both networked containers and their cleanup. Do not
    # put it beneath a signal-forwarding process-group killer.
    source "$root/host/tools/inference_native.sh"
    checker_acquisition_receipt
    if controller_native 'checker acquisition' untimed stdio bash "$root/host/tools/docker.sh" run --image "$bootstrap_image" --retired-receipt "$operation_acquisition_receipt" -- \
        "${worker[@]}" --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse" \
        --entrypoint python3 -- -B -m tools.checker_dependencies download \
        --declaration "$declaration" --directory /wheelhouse --installer "$installer"; then :; else
        acquisition_status=$?
        printf '%s' "$inference_diagnostic" >&2
        exit "$acquisition_status"
    fi
    if ! "$inference_retirement_confirmed"; then exit 2; fi
    if [[ $action == download ]]; then exit; fi
fi

checker_host_identity "$bootstrap_image" /usr/local/bin/python3 "$uid" "$gid" || exit "$?"
worker_uid=$checker_host_uid worker_gid=$checker_host_gid
checker_run 60 "${worker[@]}" --network none --user "$worker_uid:$worker_gid" \
    --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly" \
    --mount "type=bind,src=$context/dependencies,dst=/capture" \
    --entrypoint python3 "$bootstrap_image" -B -m tools.checker_dependencies capture \
    --declaration "$declaration" --directory /wheelhouse --destination /capture --installer "$installer"

build_checker_target checker "$captured"
retain_operation=false
image_retire_after_success=true
printf -v operation_success 'Prepared checker %s with input fingerprint %s.\n' "$checker_image" "$captured"
