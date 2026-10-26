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
if [[ ! -v ARTIFACTS_INPUT_ORIGIN || $ARTIFACTS_INPUT_ORIGIN != 'command line' || ! -v ARTIFACTS_INPUT || -z $ARTIFACTS_INPUT || -L $ARTIFACTS_INPUT ]]; then
    printf '%s\n' 'Cannot build shared delivery: select ARTIFACTS as the qualified release directory on the Make command line.' >&2; exit 2
fi
if ! IFS= read -r -d '' artifacts < <(realpath -ze -- "$ARTIFACTS_INPUT"); then
    printf '%s\n' 'Cannot resolve delivery ARTIFACTS: select an existing release directory.' >&2; exit 2
fi
if [[ $artifacts == / || $artifacts =~ [[:cntrl:],] || ! -d $artifacts || $(stat -c %u -- "$artifacts") != "$uid" ]]; then
    printf '%s\n' 'Cannot grant delivery ARTIFACTS: select a caller-owned directory below the filesystem root without mount delimiters.' >&2; exit 2
fi
inputs=(tools/base_image.sh tools/container_guard.py tools/artifacts.py tools/wheelhouse.py tools/core_environment.py
    tools/delivery_image.py tools/delivery_deployment.py requirements/provider-core.lock
    containers/delivery/Dockerfile containers/delivery/check_runtime.py)
source "$root/tools/checker_operation.sh"
source "$root/tools/checker_inputs.sh"
checker_begin
checker_input_paths=("${inputs[@]}")
expected=$(checker_fingerprint)
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
build_base_image=$(bash -eu -c 'source "$1"; printf "%s" "$base_image"' bash "$context/tools/base_image.sh")
checker_run 120 --pull never --network none --read-only --user "$uid:$gid" --cap-drop ALL --security-opt no-new-privileges:true \
    --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64 --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m \
    --workdir /workspace --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/workspace \
    --mount "type=bind,src=$context/tools,dst=/workspace/tools,readonly" \
    --mount "type=bind,src=$context/requirements/provider-core.lock,dst=/core-runtime.lock,readonly" \
    --mount "type=bind,src=$artifacts,dst=/artifacts,readonly" \
    --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly" \
    --mount "type=bind,src=$context,dst=/capture" \
    --entrypoint python3 "$build_base_image" -B -m tools.delivery_image --artifacts /artifacts --pool /wheelhouse --destination /capture
for path in "$context"/wheels/*.whl; do
    [[ -f $path && ! -L $path && ${path##*/} =~ ^[A-Za-z0-9_.+-]+\.whl$ ]] || exit 2
    checker_input_paths+=("${path#"$context/"}")
done
checker_input_paths+=(provider-core-install.lock)
fingerprint=$(root=$context checker_fingerprint)
# Failure after dispatch retains build inputs: client retirement does not prove
# that BuildKit has stopped reading them.
retain_operation=true
checker_call 'offline shared delivery image build' 900 stdio "${engine[@]}" build --builder default --platform linux/amd64 \
    --network none --pull=false --build-arg "BASE_IMAGE=$build_base_image" --label "org.nmr-provider-tools.inputs=$fingerprint" --label org.nmr-provider-tools.role=delivery \
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
checker_call 'shared delivery image publication' 30 stdio "${engine[@]}" image tag "$image" "nmr-provider-tools/delivery:$fingerprint"
retain_operation=false
printf -v operation_success 'Prepared shared delivery %s with input fingerprint %s.\n' "$image" "$fingerprint"
