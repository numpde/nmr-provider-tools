#!/usr/bin/env bash
# Build a science-independent runtime from the issued wheel, never package source.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ $# == 0 ]] || exit 2
source "$root/tools/base_image.sh"
source "$root/tools/wheelhouse_path.sh"
select_wheelhouse
require_wheelhouse_pool
uid=$(id -u) gid=$(id -g)
if [[ $uid == 0 || $gid == 0 || $root =~ [[:cntrl:],] ]]; then
    printf '%s\n' 'Cannot build shared delivery: use a nonzero caller and repository path without mount delimiters.' >&2; exit 2
fi
inputs=(tools/base_image.sh tools/container_guard.py tools/artifacts.py tools/wheelhouse.py tools/core_environment.py
    tools/delivery_image.py tools/delivery_deployment.py requirements/provider-core.lock requirements/delivery-release.json
    containers/delivery/Dockerfile containers/delivery/check_runtime.py)
source "$root/tools/checker_operation.sh"
source "$root/tools/checker_inputs.sh"
source "$root/tools/delivery_identity.sh"
recipe=$(delivery_recipe_identity)
checker_begin
checker_host_identity "$base_image" python3 "$uid" "$gid" || {
    printf '%s\n' 'Cannot map the wheelhouse owner into the selected engine.' >&2; exit 2;
}
checker_input_paths=("${inputs[@]}")
expected=$(checker_fingerprint)
[[ $recipe == "$expected" ]] || { printf '%s\n' 'Delivery recipe changed before capture; retry.' >&2; exit 2; }
context=$operation_directory/context
mkdir -- "$context"
for path in "${inputs[@]}"; do
    (cd -- "$root"; cp --parents --preserve=mode -- "$path" "$context/")
done
captured=$(root=$context checker_fingerprint)
if [[ $captured != "$expected" ]]; then
    operation_diagnostic+=$'Cannot build shared delivery: construction sources changed during capture; retry after edits finish.\n'
    exit 2
fi
base_image_reference=$(bash -eu -c 'source "$1"; printf "%s" "$base_image"' bash "$context/tools/base_image.sh")
if checker_metadata "$operation_directory/base-image" image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$base_image_reference"; then :; else
    base_status=$?
    operation_diagnostic+=$'Cannot inspect the pinned Linux/amd64 base image; verify Docker and acquire the base before building shared delivery.\n'
    exit "$base_status"
fi
read -r build_base_image base_platform < "$operation_directory/base-image"
[[ $build_base_image =~ ^sha256:[0-9a-f]{64}$ && $base_platform == linux/amd64 ]] \
    || { operation_diagnostic+=$'Cannot build shared delivery: the pinned base is not a Linux/amd64 image.\n'; exit 2; }
checker_run 120 --pull never --network none --read-only --user "$checker_host_uid:$checker_host_gid" --cap-drop ALL --security-opt no-new-privileges:true \
    --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64 --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m \
    --workdir /workspace --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/workspace \
    --mount "type=bind,src=$context/tools,dst=/workspace/tools,readonly" \
    --mount "type=bind,src=$context/requirements/provider-core.lock,dst=/core-runtime.lock,readonly" \
    --mount "type=bind,src=$context/requirements/delivery-release.json,dst=/release.json,readonly" \
    --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly" \
    --mount "type=bind,src=$context,dst=/capture" \
    --entrypoint python3 "$build_base_image" -B -m tools.delivery_image capture --pin /release.json --runtime-lock /core-runtime.lock --pool /wheelhouse --destination /capture
for path in provider-core-install.lock provider-core-wheel-inputs.json; do
    [[ -f $context/$path && ! -L $context/$path ]] || exit 2
    checker_input_paths+=("$path")
done
fingerprint=$(root=$context checker_fingerprint)
# BuildKit cannot resolve a config ID in FROM. Give this operation a private tag
# for that exact image; the tag and inputs remain if build retirement is uncertain.
base_tag="nmr-provider-tools/base-operation:${operation_directory##*.}"
checker_metadata "$operation_directory/base-references" image inspect --format '{{range .RepoTags}}{{println .}}{{end}}{{range .RepoDigests}}{{println .}}{{end}}' "$build_base_image"
base_reference=
while IFS= read -r reference; do
    [[ -n $reference && $reference != nmr-provider-tools/base-operation:* ]] || continue
    base_reference=$reference
    break
done < "$operation_directory/base-references"
if [[ -z $base_reference ]]; then
    operation_diagnostic+=$'Cannot build shared delivery: the pinned base needs a persistent image reference before temporary tagging.\n'; exit 2
fi
checker_metadata "$operation_directory/base-tag-existing" image ls --quiet --no-trunc "$base_tag"
if [[ -s $operation_directory/base-tag-existing ]]; then
    operation_diagnostic+=$'Cannot build shared delivery: the operation base tag already exists.\n'; exit 2
fi
printf '%s\n' "$base_tag" > "$operation_directory/base-tag-name"
retain_operation=true
checker_call 'tag verified shared delivery base' 30 stdio "${engine[@]}" image tag "$build_base_image" "$base_tag"
checker_metadata "$operation_directory/base-tag" image inspect --format '{{.Id}}' "$base_tag"
if ! cmp -s "$operation_directory/base-tag" <(printf '%s\n' "$build_base_image"); then
    operation_diagnostic+=$'Cannot build shared delivery: the operation base tag does not identify the pinned image.\n'; exit 2
fi
checker_call 'offline shared delivery image build' 900 stdio "${engine[@]}" build --builder default --platform linux/amd64 \
    --network none --pull=false --build-context "wheelhouse=$wheelhouse_directory" --build-arg "BASE_IMAGE=$base_tag" --label "org.nmr-provider-tools.inputs=$fingerprint" --label "org.nmr-provider-tools.recipe=$recipe" --label org.nmr-provider-tools.role=delivery \
    --iidfile "$operation_directory/built-image" --file "$context/containers/delivery/Dockerfile" "$context"
image=
if [[ -f $operation_directory/built-image && ! -L $operation_directory/built-image && $(stat -c %s -- "$operation_directory/built-image") -le 72 ]]; then
    if IFS= read -r -N 71 image < "$operation_directory/built-image"; then :; else image=; fi
fi
if [[ ! $image =~ ^sha256:[0-9a-f]{64}$ ]] \
    || ! { cmp -s "$operation_directory/built-image" <(printf '%s' "$image") || cmp -s "$operation_directory/built-image" <(printf '%s\n' "$image"); }; then
    operation_diagnostic+=$'Cannot admit shared delivery build: BuildKit did not publish an immutable image ID.\n'; exit 2
fi
checker_metadata "$operation_directory/built-label" image inspect --format '{{index .Config.Labels "org.nmr-provider-tools.inputs"}}' "$image"
if ! cmp -s "$operation_directory/built-label" <(printf '%s\n' "$fingerprint"); then
    operation_diagnostic+=$'Cannot admit shared delivery build: image label differs from captured construction inputs.\n'; exit 2
fi
checker_metadata "$operation_directory/base-tag-after-build" image inspect --format '{{.Id}}' "$base_tag"
checker_metadata "$operation_directory/base-reference-after-build" image inspect --format '{{.Id}}' "$base_reference"
if ! cmp -s "$operation_directory/base-tag-after-build" <(printf '%s\n' "$build_base_image") \
    || ! cmp -s "$operation_directory/base-reference-after-build" <(printf '%s\n' "$build_base_image"); then
    operation_diagnostic+=$'Cannot retire the shared delivery base tag: the tag or persistent base reference changed; inspect the retained operation.\n'; exit 2
fi
checker_call 'shared delivery image publication' 30 stdio "${engine[@]}" image tag "$image" "nmr-provider-tools/delivery:$fingerprint"
checker_call 'shared delivery recipe publication' 30 stdio "${engine[@]}" image tag "$image" "nmr-provider-tools/delivery-recipe:$recipe"
checker_call 'remove shared delivery operation base tag' 30 stdio "${engine[@]}" image rm --no-prune "$base_tag"
retain_operation=false
printf -v operation_success 'Prepared shared delivery %s with input fingerprint %s.\n' "$image" "$fingerprint"
