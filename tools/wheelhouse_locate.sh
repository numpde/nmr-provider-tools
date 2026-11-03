#!/usr/bin/env bash
# Project a flat locked closure using the interface-bound metadata proxy.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ ${LOCK_INPUT_ORIGIN-} == 'command line' && -n ${LOCK_INPUT-} ]] || {
    printf '%s\n' 'Supply LOCK as a target-specific pinned requirements file.' >&2; exit 2;
}
lock=$(realpath -e -- "$LOCK_INPUT")
[[ -f $lock && $lock != *','* && $lock != *$'\n'* && $lock != *$'\r'* ]] || {
    printf '%s\n' 'Cannot locate locked artifacts: select a readable lock without mount delimiters.' >&2; exit 2;
}
arguments=(--lock /input/requirements.lock)
pool_mount=()
if [[ ${WHEELHOUSE_INPUT_ORIGIN-} == 'command line' ]]; then
    source "$root/tools/wheelhouse_path.sh"
    select_wheelhouse
    require_wheelhouse_pool
    pool_mount=(--mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly")
    arguments+=(--pool /wheelhouse)
fi
if [[ -n ${LOCATE_INDEX_INPUT-} ]]; then arguments+=(--index-url "$LOCATE_INDEX_INPUT"); fi
read -r -a sources <<< "${LOCATE_SOURCES_INPUT-}"
for source in "${sources[@]}"; do arguments+=(--source "$source"); done
read -r -a required <<< "${LOCATE_REQUIRED_INPUT-}"
for source in "${required[@]}"; do arguments+=(--require-source "$source"); done
command=(bash "$root/tools/docker.sh" run --image nmr-provider-tools/wheelhouse-tool:local -- --pull never --read-only \
    --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 128 --memory 512m \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m --workdir /opt \
    --mount "type=bind,src=$root/wheelhouse_tool,dst=/opt/wheelhouse_tool,readonly" \
    --mount "type=bind,src=$lock,dst=/input/requirements.lock,readonly" \
    "${pool_mount[@]}" \
    --entrypoint python -- -B -m wheelhouse_tool.locate "${arguments[@]}")
if [[ ${OUTPUT_INPUT_ORIGIN-} != 'command line' ]]; then
    exec "${command[@]}"
fi
output=$(realpath -m -- "$OUTPUT_INPUT")
temporary=$(mktemp -- "$(dirname -- "$output")/.wheelhouse-lock.XXXXXXXX")
trap 'rm -f -- "$temporary"' EXIT
"${command[@]}" > "$temporary"
ln -- "$temporary" "$output"
printf 'Projected lock written to %s\n' "$output" >&2
