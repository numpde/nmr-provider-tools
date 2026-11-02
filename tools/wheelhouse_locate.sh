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
if [[ -n ${LOCATE_INDEX_INPUT-} ]]; then arguments+=(--index-url "$LOCATE_INDEX_INPUT"); fi
read -r -a sources <<< "${LOCATE_SOURCES_INPUT-}"
for source in "${sources[@]}"; do arguments+=(--source "$source"); done
read -r -a required <<< "${LOCATE_REQUIRED_INPUT-}"
for source in "${required[@]}"; do arguments+=(--require-source "$source"); done
identity=$(bash "$root/tools/host_identity.sh" nmr-provider-tools/wheelhouse-tool:local)
command=(bash "$root/tools/docker.sh" run --pull never --read-only --user "$identity" \
    --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 128 --memory 512m \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m --workdir /opt \
    --mount "type=bind,src=$root/wheelhouse_tool,dst=/opt/wheelhouse_tool,readonly" \
    --mount "type=bind,src=$lock,dst=/input/requirements.lock,readonly" \
    --entrypoint python nmr-provider-tools/wheelhouse-tool:local -m wheelhouse_tool.locate "${arguments[@]}")
if [[ ${OUTPUT_INPUT_ORIGIN-} != 'command line' ]]; then
    exec "${command[@]}"
fi
output=$(realpath -m -- "$OUTPUT_INPUT")
temporary=$(mktemp -- "$(dirname -- "$output")/.wheelhouse-lock.XXXXXXXX")
trap 'rm -f -- "$temporary"' EXIT
"${command[@]}" > "$temporary"
ln -- "$temporary" "$output"
printf 'Projected lock written to %s\n' "$output" >&2
