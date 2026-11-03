#!/usr/bin/env bash
# Qualify the actual SDK relay on one selected local engine, without downloads.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ $root =~ [[:cntrl:],] ]]; then
    printf '%s\n' 'Cannot mount test inputs: checkout path contains a comma or control character.' >&2
    exit 2
fi
source "$root/tools/docker_context.sh"
select_docker_context
source "$root/tools/base_image.sh"
source "$root/tools/checker_operation.sh"
checker_begin
ordered_container_retirement=true
record_container() {
    operation_retire_order=("$operation_container" "${operation_retire_order[@]}")
}

checker_metadata "$operation_directory/base" image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$base_image"
read -r resolved_image platform extra < "$operation_directory/base"
[[ $resolved_image =~ ^sha256:[0-9a-f]{64}$ && $platform == linux/amd64 && -z ${extra:-} ]] || {
    printf '%s\n' 'The pinned Python base has invalid selected-engine metadata.' >&2; exit 2;
}
checker_host_identity "$base_image" python3 "$(id -u)" "$(id -g)"
record_container
uid=$checker_host_uid gid=$checker_host_gid
mkdir -m 700 "$operation_directory/route" "$operation_directory/relay-ready"
route=$operation_directory/route
relay_ready=$operation_directory/relay-ready
fixture=$root/tests/acquisition_network/probe.py
sdk=$root/packages/provider-core/src
common=(--pull never --read-only --user "$uid:$gid" --cap-drop ALL --security-opt no-new-privileges:true
    --cpus 1 --memory 128m --memory-swap 128m --pids-limit 32
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m
    --env PROVIDER_CORE_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1)

wait_ready() {
    local file=$1 count
    for count in {1..100}; do
        [[ -s "$file" && ! -L "$file" ]] && return 0
        sleep 0.05
    done
    printf 'Timed out waiting for %s.\n' "${file##*/}" >&2
    return 2
}

checker_create "${common[@]}" --network host \
    --mount "type=bind,src=$route,dst=/route" \
    --mount "type=bind,src=$fixture,dst=/fixture/probe.py,readonly" \
    --entrypoint python3 "$base_image" -I -B /fixture/probe.py origin /route/origin.ready
origin=$operation_container
record_container
checker_metadata "$operation_directory/origin-start" start "$origin"
wait_ready "$route/origin.ready"
origin_port=$(<"$route/origin.ready")
[[ $origin_port =~ ^[0-9]+$ ]] && ((origin_port > 0 && origin_port <= 65535))

checker_create "${common[@]}" --network host \
    --env PYTHONPATH=/sdk --mount "type=bind,src=$sdk,dst=/sdk,readonly" \
    --mount "type=bind,src=$route,dst=/route" --entrypoint python3 "$base_image" -P -B \
    -m provider_core.bound_http_proxy --interface system --unix-socket /route/gateway.sock --ready-file /route/gateway.ready
gateway=$operation_container
record_container
checker_metadata "$operation_directory/gateway-start" start "$gateway"
wait_ready "$route/gateway.ready"
cmp -s "$route/gateway.ready" <(printf '/route/gateway.sock\n')
[[ -S "$route/gateway.sock" && ! -L "$route/gateway.sock" && $(stat -c %a "$route/gateway.sock") == 600 ]]

checker_create "${common[@]}" --network none \
    --env PYTHONPATH=/sdk --mount "type=bind,src=$sdk,dst=/sdk,readonly" \
    --mount "type=bind,src=$route,dst=/route,readonly" \
    --mount "type=bind,src=$relay_ready,dst=/ready" --entrypoint python3 "$base_image" -P -B \
    -m provider_core.bound_http_proxy --relay-socket /route/gateway.sock --port 0 --ready-file /ready/relay
relay_id=$operation_container
record_container
checker_metadata "$operation_directory/relay-start" start "$relay_id"
checker_metadata "$operation_directory/relay-mode" inspect --format '{{.HostConfig.NetworkMode}} {{.State.Running}}' "$relay_id"
cmp -s "$operation_directory/relay-mode" <(printf 'none true\n')
wait_ready "$relay_ready/relay"
relay_port=$(<"$relay_ready/relay")
[[ $relay_port =~ ^[0-9]+$ ]] && ((relay_port > 0 && relay_port <= 65535))
checker_metadata "$operation_directory/relay-netns" exec "$relay_id" python3 -I -B -c 'import os; print(os.stat("/proc/self/ns/net").st_ino)'

checker_create "${common[@]}" --network "container:$relay_id" \
    --mount "type=bind,src=$fixture,dst=/fixture/probe.py,readonly" \
    --entrypoint python3 "$base_image" -I -B /fixture/probe.py worker "$relay_port" "$origin_port"
worker=$operation_container
record_container
checker_metadata "$operation_directory/worker-mode" inspect --format '{{.HostConfig.NetworkMode}}' "$worker"
cmp -s "$operation_directory/worker-mode" <(printf 'container:%s\n' "$relay_id")
checker_call 'isolated acquisition network test' 30 stdio "${engine[@]}" start --attach "$worker" >"$operation_directory/worker-result"
python3 -I -B - "$operation_directory/relay-netns" "$operation_directory/worker-result" <<'PY'
import json, pathlib, sys
relay = int(pathlib.Path(sys.argv[1]).read_text())
result = json.loads(pathlib.Path(sys.argv[2]).read_text())
if result != {"netns": relay, "http": True, "connect": True}:
    raise SystemExit("Worker did not prove the relay namespace and both proxy paths.")
PY
printf -v operation_success 'Isolated acquisition network test passed on the selected engine.\n'
