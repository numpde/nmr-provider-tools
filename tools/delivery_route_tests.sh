#!/usr/bin/env bash
# Host owns disposable Engine state; peer/client containers receive no Engine grant.
set -euo pipefail
umask 077
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
peer_options=()
expected_sni='"localhost"'
if [[ $# == 1 && $1 == --literal-ip ]]; then
    peer_options=(--literal-ip)
    expected_sni=null
elif [[ $# != 0 ]]; then
    exit 2
fi
uid=$(id -u) gid=$(id -g)
[[ $uid != 0 && $gid != 0 && ! $root =~ [[:cntrl:],] ]] || exit 2
[[ -v SHARED_RUNTIME_IMAGE_INPUT_ORIGIN && $SHARED_RUNTIME_IMAGE_INPUT_ORIGIN == 'command line' \
    && -v SHARED_RUNTIME_IMAGE_INPUT && $SHARED_RUNTIME_IMAGE_INPUT =~ ^sha256:[0-9a-f]{64}$ ]] \
    || { printf '%s\n' 'Use tests/deployment/route with explicit SHARED_RUNTIME_IMAGE from delivery/image/build.' >&2; exit 2; }
image=$SHARED_RUNTIME_IMAGE_INPUT
python=/opt/provider-core/environment/bin/python
source "$root/tools/checker_operation.sh"
checker_begin
project="route-proof-${operation_directory##*/}"
project=${project,,}
project=${project//./-}
operation=${operation_directory##*/}
evidence_volume=$project-evidence
route_volume=

route_finish() {
    local status=$? identifier volume
    trap - EXIT
    trap '' HUP INT TERM
    if "$inference_retirement_confirmed" && ! "$retain_operation"; then
        if checker_metadata "$operation_directory/route-containers" ps --all --quiet --no-trunc --filter "label=$operation_label"; then
            while IFS= read -r identifier; do
                if [[ $identifier =~ ^[0-9a-f]{64}$ ]]; then
                    if checker_teardown rm --force --volumes "$identifier" > /dev/null; then :; else retain_operation=true; fi
                else
                    retain_operation=true
                fi
            done < "$operation_directory/route-containers"
        else
            retain_operation=true
        fi
        if ! "$retain_operation" && checker_metadata "$operation_directory/route-volumes" volume ls --quiet --filter "label=$operation_label"; then
            while IFS= read -r volume; do
                if [[ $volume == "$evidence_volume" || -n $route_volume && $volume == "$route_volume" ]]; then
                    if checker_teardown volume rm -- "$volume" > /dev/null; then :; else retain_operation=true; fi
                else
                    retain_operation=true
                fi
            done < "$operation_directory/route-volumes"
        else
            retain_operation=true
        fi
        if ! "$retain_operation" && checker_metadata "$operation_directory/remaining-volumes" volume ls --quiet --filter "label=$operation_label"; then
            if [[ -s $operation_directory/remaining-volumes ]]; then retain_operation=true; fi
        else
            retain_operation=true
        fi
    fi
    if "$retain_operation"; then
        printf 'Route proof cleanup is unconfirmed; retain its labeled inputs at %q.\n' "$operation_directory" >&2
        if [[ $status == 0 ]]; then status=2; fi
    fi
    # Preserve the proof's original status for the common finalizer after our
    # named volumes have lost all owned container attachments.
    set +e
    (exit "$status")
    checker_finish
}
trap route_finish EXIT

checker_metadata "$operation_directory/image" image inspect --format '{{.Id}} {{index .Config.Labels "org.nmr-provider-tools.role"}}' "$image"
cmp -s "$operation_directory/image" <(printf '%s delivery\n' "$image") || exit 2
checker_create --name "$project-reservation" --pull never --network none --read-only --user "$uid:$gid" --cap-drop ALL     --security-opt no-new-privileges:true --cpus 1 --memory 64m --memory-swap 64m --pids-limit 16 --entrypoint /bin/true "$image"
fixture=$operation_directory/fixture.py
cp -- "$root/tests/deployment/route_fixture.py" "$fixture"
chmod 444 "$fixture"
support=$root/packages/provider-core/tests
fixture_grants=(--mount "type=bind,src=$fixture,dst=/fixture.py,readonly"
    --mount "type=bind,src=$support,dst=/test-support/tests,readonly")
worker=(--pull never --read-only --init --cap-drop ALL --security-opt no-new-privileges:true
    --cpus 2 --memory 256m --memory-swap 256m --pids-limit 64
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m)

fixture_call() {
    checker_run 30 "${worker[@]}" --network none --user 65532:65532 "${fixture_grants[@]}" \
        --mount "type=volume,src=$evidence_volume,dst=/evidence,readonly,volume-nocopy" \
        --entrypoint "$python" "$image" -I -B /fixture.py "$@"
}

checker_metadata "$operation_directory/created-volume" volume create --label "$operation_label" "$evidence_volume"
cmp -s "$operation_directory/created-volume" <(printf '%s\n' "$evidence_volume") || exit 2
checker_metadata "$operation_directory/volume-owner" volume inspect --format '{{.Name}} {{index .Labels "org.nmr-provider-tools.operation"}}' "$evidence_volume"
cmp -s "$operation_directory/volume-owner" <(printf '%s %s\n' "$evidence_volume" "$operation") || exit 2
checker_run 30 "${worker[@]}" --network none --user 0:0 --cap-add CHOWN \
    --mount "type=volume,src=$evidence_volume,dst=/route,volume-nocopy" \
    --entrypoint "$python" "$image" -I -B -m provider_core.deployment_route initialize --uid 65532 --gid 65532
checker_create "${worker[@]}" --network host --user 65532:65532 "${fixture_grants[@]}" \
    --mount "type=volume,src=$evidence_volume,dst=/evidence,volume-nocopy" \
    --entrypoint "$python" "$image" -I -B /fixture.py peer "${peer_options[@]}"
peer=$operation_container
checker_call 'start synthetic loopback TLS peer' 30 stdio "${engine[@]}" start "$peer"
fixture_call peer-info > "$operation_directory/binding.toml"
checker_run 30 "${worker[@]}" --network none --user "$uid:$gid" \
    --mount "type=bind,src=$operation_directory/binding.toml,dst=/binding.toml,readonly" \
    --entrypoint "$python" "$image" -I -B -m provider_core.deployment_route render \
    --definition /binding.toml --binding fixture --service-name api --image "$image" --interpreter "$python" \
    --namespace "$project" --uid 65532 --gid 65532 --named-volume > "$operation_directory/projection.json"
checker_run 30 "${worker[@]}" --network none --user "$uid:$gid" "${fixture_grants[@]}" \
    --mount "type=bind,src=$operation_directory/projection.json,dst=/projection.json,readonly" \
    --entrypoint "$python" "$image" -I -B /fixture.py compose /projection.json --image "$image" \
    --namespace "$project" --operation "$operation" --fixture "$fixture" --support "$support" > "$operation_directory/compose.json"
compose=("${engine[@]}" compose --env-file /dev/null --project-name "$project" --project-directory / --file "$operation_directory/compose.json")
proxy_role=$(jq -er '.client_depends_on | keys | .[0]' "$operation_directory/projection.json")
client_socket=$(jq -er '.client_socket' "$operation_directory/projection.json")
route_volume=$(jq -er '.client_mount.source' "$operation_directory/projection.json")
checker_metadata "$operation_directory/existing-route" volume ls --quiet --filter "name=$route_volume"
[[ ! -s $operation_directory/existing-route ]] || { printf '%s\n' 'Cannot start route proof: its selected private volume already exists.' >&2; exit 2; }
checker_call 'start emitted private API route' 60 stdio "${compose[@]}" up --detach --no-build --pull never --wait --wait-timeout 30
checker_metadata "$operation_directory/proxy-id" compose --env-file /dev/null --project-name "$project" --project-directory / --file "$operation_directory/compose.json" ps --quiet --no-trunc "$proxy_role"
IFS= read -r proxy < "$operation_directory/proxy-id"
[[ $proxy =~ ^[0-9a-f]{64}$ ]] && cmp -s "$operation_directory/proxy-id" <(printf '%s\n' "$proxy") || exit 2
checker_metadata "$operation_directory/proxy-grants" inspect --format '{{json .Mounts}}' "$proxy"
jq -e '[.[] | select(.Type == "volume" or .Type == "bind")] | length == 1 and .[0].Type == "volume" and .[0].Destination == "/route" and .[0].RW == true' "$operation_directory/proxy-grants" > /dev/null
checker_call 'signed HTTPS through read-only private route' 30 stdio "${compose[@]}" run --rm --no-deps client success "$client_socket"
checker_call 'different UID cannot traverse route' 30 stdio "${compose[@]}" run --rm --no-deps --user 65531:65531 client forbidden "$client_socket"
checker_call 'direct-reachability positive control' 30 stdio "${compose[@]}" run --rm --no-deps canary direct "$client_socket"
checker_call 'stop exact proxy after original exchange settlement' 30 stdio "${engine[@]}" stop --signal SIGTERM --timeout 15 "$proxy"
checker_call 'lost route cannot fall back to reachable direct API' 30 stdio "${compose[@]}" run --rm --no-deps canary lost "$client_socket"
fixture_call received > "$operation_directory/after-loss.json"
jq -e --argjson sni "$expected_sni" '.requests == 2 and .server_names == [$sni, $sni]' "$operation_directory/after-loss.json" > /dev/null
checker_call 'restart exact emitted proxy with owned stale leaves' 60 stdio "${compose[@]}" up --detach --no-build --pull never --no-recreate --wait --wait-timeout 30 "$proxy_role"
checker_call 'signed HTTPS succeeds after owned stale-socket restart' 30 stdio "${compose[@]}" run --rm --no-deps client success "$client_socket"
fixture_call received > "$operation_directory/after-restart.json"
jq -e --argjson sni "$expected_sni" '.requests == 3 and .server_names == [$sni, $sni, $sni]' "$operation_directory/after-restart.json" > /dev/null
checker_call 'settle synthetic peer handlers' 30 stdio "${engine[@]}" stop --signal SIGTERM --timeout 10 "$peer"
checker_metadata "$operation_directory/peer-exit" inspect --format '{{.State.ExitCode}}' "$peer"
cmp -s "$operation_directory/peer-exit" <(printf '0\n') || exit 2
printf -v operation_success '%s\n' 'Emitted private route passed signed HTTPS, read-only/UID isolation, no direct fallback and owned stale-socket restart.'
