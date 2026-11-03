#!/usr/bin/env bash
# Acquire the selected target lock through the interface-bound proxy.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ ${LOCK_INPUT_ORIGIN-} == 'command line' && -n ${LOCK_INPUT-} ]] || {
    printf '%s\n' 'Supply LOCK as a target-specific pylock file.' >&2; exit 2;
}
lock=$(realpath -e -- "$LOCK_INPUT")
[[ -f $lock && $lock != *','* && $lock != *$'\n'* && $lock != *$'\r'* ]] || {
    printf '%s\n' 'Cannot acquire locked artifacts: select a readable lock without mount delimiters.' >&2; exit 2;
}
source "$root/tools/wheelhouse_path.sh"
select_wheelhouse
require_wheelhouse_pool
arguments=(acquire --lock /input/pylock.toml --pool /wheelhouse)
source_mount=()
if [[ -n ${SOURCE_LOCK_INPUT-} ]]; then
    source_lock=$(realpath -e -- "$SOURCE_LOCK_INPUT")
    [[ -f $source_lock && $source_lock != *','* && $source_lock != *$'\n'* && $source_lock != *$'\r'* ]] || {
        printf '%s\n' 'Cannot acquire locked artifacts: select a readable source lock without mount delimiters.' >&2; exit 2;
    }
    source_mount=(--mount "type=bind,src=$source_lock,dst=/input/source.lock,readonly")
    arguments+=(--source-lock /input/source.lock)
fi
read -r -a required <<< "${LOCATE_REQUIRED_INPUT-}"
for package in "${required[@]}"; do arguments+=(--require-sdist "$package"); done
exec bash "$root/tools/docker.sh" run --image nmr-provider-tools/wheelhouse-tool:local -- --pull never --read-only \
    --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 128 --memory 512m \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m --workdir /opt \
    --mount "type=bind,src=$root/wheelhouse_tool,dst=/opt/wheelhouse_tool,readonly" \
    --mount "type=bind,src=$lock,dst=/input/pylock.toml,readonly" \
    "${source_mount[@]}" \
    --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse" \
    --entrypoint python -- -B -m wheelhouse_tool "${arguments[@]}"
