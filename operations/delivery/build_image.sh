#!/usr/bin/env bash
# Build a science-independent runtime from the issued wheel, never package source.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
[[ $# == 0 ]] || exit 2
source "$root/operations/docker/base_image.sh"
source "$root/host/tools/wheelhouse_path.sh"
select_wheelhouse
require_wheelhouse_pool
uid=$(id -u) gid=$(id -g)
if [[ $uid == 0 || $gid == 0 || $root =~ [[:cntrl:],] ]]; then
    printf '%s\n' 'Cannot build shared delivery: use a nonzero caller and repository path without mount delimiters.' >&2; exit 2
fi
source "$root/operations/docker/container_operation.sh"
source "$root/operations/checker/image_identity.sh"
source "$root/operations/delivery/image_identity.sh"
recipe=$(delivery_recipe_identity)
operation_begin
checker_admit_tools
operation_host_identity "$checker_image" /usr/local/bin/python3 "$uid" "$gid" || exit "$?"
inputs=("${delivery_recipe_inputs[@]}")
expected=$(image_fingerprint checker "${inputs[@]}")
[[ $recipe == "$expected" ]] || { printf '%s\n' 'Delivery recipe changed before capture; retry.' >&2; exit 2; }
context=$operation_directory/context
mkdir -- "$context"
for path in "${inputs[@]}"; do
    (cd -- "$root"; cp --parents --preserve=mode -- "$path" "$context/")
done
captured=$(root=$context image_fingerprint checker "${inputs[@]}")
if [[ $captured != "$expected" ]]; then
    operation_diagnostic+=$'Cannot build shared delivery: construction sources changed during capture; retry after edits finish.\n'
    exit 2
fi
base_image_reference=$(bash -eu -c 'source "$1"; printf "%s" "$base_image"' bash "$context/operations/docker/base_image.sh")
if operation_metadata "$operation_directory/base-image" image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$base_image_reference"; then :; else
    base_status=$?
    operation_diagnostic+=$'Cannot inspect the pinned Linux/amd64 base image; verify Docker and acquire the base before building shared delivery.\n'
    exit "$base_status"
fi
read -r build_base_image base_platform < "$operation_directory/base-image"
[[ $build_base_image =~ ^sha256:[0-9a-f]{64}$ && $base_platform == linux/amd64 ]] \
    || { operation_diagnostic+=$'Cannot build shared delivery: the pinned base is not a Linux/amd64 image.\n'; exit 2; }
operation_run 120 --pull never --network none --read-only --user "$operation_host_uid:$operation_host_gid" --cap-drop ALL --security-opt no-new-privileges:true \
    --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64 --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m \
    --workdir /workspace --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/workspace \
    --mount "type=bind,src=$context/tools,dst=/workspace/tools,readonly" \
    --mount "type=bind,src=$context/packages/type-requirements/src/type_requirements.py,dst=/workspace/type_requirements.py,readonly" \
    --mount "type=bind,src=$context/packages/provider-core/sdk_build/provider_core_acquire.py,dst=/workspace/sdk_build/provider_core_acquire.py,readonly" \
    --mount "type=bind,src=$context/operations/delivery/build_inputs.py,dst=/workspace/operations/delivery/build_inputs.py,readonly" \
    --mount "type=bind,src=$context/requirements/provider-core.lock,dst=/core-runtime.lock,readonly" \
    --mount "type=bind,src=$context/requirements/delivery-release.json,dst=/release.json,readonly" \
    --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly" \
    --mount "type=bind,src=$context,dst=/capture" \
    --entrypoint python3 "$checker_image" -B -m operations.delivery.build_inputs capture --pin /release.json --runtime-lock /core-runtime.lock --pool /wheelhouse --destination /capture
for path in provider-core-install.lock provider-core-wheel-inputs.json provider-core-stream.json operations/delivery/image/Dockerfile.selected; do
    [[ -f $context/$path && ! -L $context/$path ]] || exit 2
    inputs+=("$path")
done
fingerprint=$(root=$context image_fingerprint checker "${inputs[@]}")
if ! python3 -I -S -B -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    operation_diagnostic+=$'Cannot build shared delivery: host Python 3.11 or newer is needed to stream build inputs.\n'; exit 2
fi
tool_pin=$(python3 -I -S -B "$context/host/tools/build_stream_inputs.py" "$context" \
    "$operation_directory/source-allowlist.json" "${inputs[@]}")
[[ $(root=$context image_fingerprint checker "${inputs[@]}") == "$fingerprint" ]] || {
    operation_diagnostic+=$'Cannot build shared delivery: captured inputs changed before streaming.\n'; exit 2;
}
read -r tool_name tool_hash <<< "$tool_pin"
[[ $tool_name =~ ^wheelhouse-tool-[0-9.]+\.pyz$ && $tool_hash =~ ^[0-9a-f]{64}$ ]] || {
    operation_diagnostic+=$'Cannot build shared delivery: the wheelhouse tool pin is invalid.\n'; exit 2;
}
if [[ ! -f $wheelhouse_directory/$tool_name || -L $wheelhouse_directory/$tool_name ]] \
    || [[ $(sha256sum -- "$wheelhouse_directory/$tool_name" | cut -d' ' -f1) != "$tool_hash" ]]; then
    operation_diagnostic+=$'Cannot build shared delivery: acquire the pinned wheelhouse tool with make delivery/prepare.\n'; exit 2
fi
# BuildKit cannot resolve a config ID in FROM. Give this operation a private tag
# for that exact image; the tag and inputs remain if build retirement is uncertain.
base_tag="nmr-provider-tools/base-operation:${operation_directory##*.}"
operation_metadata "$operation_directory/base-references" image inspect --format '{{range .RepoTags}}{{println .}}{{end}}{{range .RepoDigests}}{{println .}}{{end}}' "$build_base_image"
base_reference=
while IFS= read -r reference; do
    [[ -n $reference && $reference != nmr-provider-tools/base-operation:* ]] || continue
    base_reference=$reference
    break
done < "$operation_directory/base-references"
if [[ -z $base_reference ]]; then
    operation_diagnostic+=$'Cannot build shared delivery: the pinned base needs a persistent image reference before temporary tagging.\n'; exit 2
fi
operation_metadata "$operation_directory/base-tag-existing" image ls --quiet --no-trunc "$base_tag"
if [[ -s $operation_directory/base-tag-existing ]]; then
    operation_diagnostic+=$'Cannot build shared delivery: the operation base tag already exists.\n'; exit 2
fi
printf '%s\n' "$base_tag" > "$operation_directory/base-tag-name"
retain_operation=true
operation_call 'tag verified shared delivery base' 30 stdio "${engine[@]}" image tag "$build_base_image" "$base_tag"
operation_metadata "$operation_directory/base-tag" image inspect --format '{{.Id}}' "$base_tag"
if ! cmp -s "$operation_directory/base-tag" <(printf '%s\n' "$build_base_image"); then
    operation_diagnostic+=$'Cannot build shared delivery: the operation base tag does not identify the pinned image.\n'; exit 2
fi
operation_call 'offline shared delivery image build' 900 stdio bash -o pipefail -c '
    tool=$1 pool=$2 allowlist=$3 manifest=$4
    shift 4
    python3 -I -S -B "$tool" stream --pool "$pool" --source-allowlist "$allowlist" --manifest "$manifest" | "$@"
' bash "$wheelhouse_directory/$tool_name" "$wheelhouse_directory" "$operation_directory/source-allowlist.json" \
    "$context/provider-core-stream.json" "${engine[@]}" build --builder default --platform linux/amd64 \
    --network none --pull=false --build-arg "BASE_IMAGE=$base_tag" --label "org.nmr-provider-tools.inputs=$fingerprint" --label "org.nmr-provider-tools.recipe=$recipe" --label org.nmr-provider-tools.role=delivery \
    --iidfile "$operation_directory/built-image" --file operations/delivery/image/Dockerfile.selected -
image=
if [[ -f $operation_directory/built-image && ! -L $operation_directory/built-image && $(stat -c %s -- "$operation_directory/built-image") -le 72 ]]; then
    if IFS= read -r -N 71 image < "$operation_directory/built-image"; then :; else image=; fi
fi
if [[ ! $image =~ ^sha256:[0-9a-f]{64}$ ]] \
    || ! { cmp -s "$operation_directory/built-image" <(printf '%s' "$image") || cmp -s "$operation_directory/built-image" <(printf '%s\n' "$image"); }; then
    operation_diagnostic+=$'Cannot admit shared delivery build: BuildKit did not publish an immutable image ID.\n'; exit 2
fi
operation_metadata "$operation_directory/built-label" image inspect --format '{{index .Config.Labels "org.nmr-provider-tools.inputs"}}' "$image"
if ! cmp -s "$operation_directory/built-label" <(printf '%s\n' "$fingerprint"); then
    operation_diagnostic+=$'Cannot admit shared delivery build: image label differs from captured construction inputs.\n'; exit 2
fi
operation_metadata "$operation_directory/base-tag-after-build" image inspect --format '{{.Id}}' "$base_tag"
operation_metadata "$operation_directory/base-reference-after-build" image inspect --format '{{.Id}}' "$base_reference"
if ! cmp -s "$operation_directory/base-tag-after-build" <(printf '%s\n' "$build_base_image") \
    || ! cmp -s "$operation_directory/base-reference-after-build" <(printf '%s\n' "$build_base_image"); then
    operation_diagnostic+=$'Cannot retire the shared delivery base tag: the tag or persistent base reference changed; inspect the retained operation.\n'; exit 2
fi
operation_call 'shared delivery image publication' 30 stdio "${engine[@]}" image tag "$image" "nmr-provider-tools/delivery:$fingerprint"
operation_call 'shared delivery recipe publication' 30 stdio "${engine[@]}" image tag "$image" "nmr-provider-tools/delivery-recipe:$recipe"
operation_call 'remove shared delivery operation base tag' 30 stdio "${engine[@]}" image rm --no-prune "$base_tag"
retain_operation=false
image_retire_after_success=true
printf -v operation_success 'Prepared shared delivery %s with input fingerprint %s.\n' "$image" "$fingerprint"
