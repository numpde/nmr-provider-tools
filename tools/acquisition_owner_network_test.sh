#!/usr/bin/env bash
# Qualify ordered shared-owner workers from exact release source, without downloads.
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
owner_started=false
owner_receipt=$operation_directory/acquisition-retired
owner_checker_finish() {
    local status=$?
    if "$owner_started" && ! cmp -s "$owner_receipt" <(printf 'retired\n'); then
        retain_operation=true
    fi
    if (exit "$status"); then checker_finish; else checker_finish; fi
}
trap owner_checker_finish EXIT
if [[ ${ARTIFACTS_INPUT_ORIGIN:-} != 'command line' || -z ${ARTIFACTS_INPUT:-} || -L $ARTIFACTS_INPUT ]]; then
    printf '%s\n' 'Select ARTIFACTS as an existing owned release directory on the Make command line.' >&2
    exit 2
fi
IFS= read -r -d '' artifacts < <(realpath -ze -- "$ARTIFACTS_INPUT") || exit 2
[[ $artifacts != / && ! $artifacts =~ [[:cntrl:],] && -d $artifacts && $(stat -c %u -- "$artifacts") == "$(id -u)" ]] || exit 2
bundle=$operation_directory/acquisition-source
python3 -I -B - "$artifacts" "$bundle" <<'VERIFY'
import hashlib, json, os
from pathlib import Path, PurePosixPath
import sys, tarfile
artifacts, destination = map(Path, sys.argv[1:])
records = list(artifacts.glob("provider_core-*-release.json"))
if len(records) != 1:
    raise SystemExit("Require one issuer release record in ARTIFACTS.")
record = json.loads(records[0].read_text())
names = [name for name in record["files"] if name.startswith("acquisition-owner-") and name.endswith(".tar.gz")]
if len(names) != 1:
    raise SystemExit("Issuer record must select one acquisition source archive.")
archive_path = artifacts / names[0]
if archive_path.is_symlink() or not archive_path.is_file():
    raise SystemExit("Acquisition source archive must be a regular release file.")
if hashlib.sha256(archive_path.read_bytes()).hexdigest() != record["files"][names[0]]:
    raise SystemExit("Acquisition source archive differs from its issuer hash.")
inventory = record["acquisition_source_files"]
contents = {}
with tarfile.open(archive_path, "r:gz") as archive:
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if (not member.isfile() or path.is_absolute() or ".." in path.parts
                or not path.parts or path.as_posix() != member.name or member.name in contents):
            raise SystemExit("Acquisition source archive has unsupported or duplicate members.")
        with archive.extractfile(member) as source:
            data = source.read()
        actual = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": member.mode}
        if actual != inventory.get(member.name):
            raise SystemExit("Acquisition source member differs from its issuer inventory.")
        contents[member.name] = data
if set(contents) != set(inventory):
    raise SystemExit("Acquisition source inventory is incomplete.")
# Materialize only after the complete normalized inventory is admitted.
destination.mkdir(mode=0o700)
for name, data in contents.items():
    target = destination / name
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as output:
        output.write(data)
    os.chmod(target, inventory[name]["mode"])
VERIFY
source "$bundle/acquisition_tool/owner.sh"
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
mkdir -m 700 "$operation_directory/route"
route=$operation_directory/route
fixture=$root/tests/acquisition_network/probe.py
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

# The callback uses the bundled owner and its qualified identity probe for every
# ordered worker. The checker owns only the known origin and borrowed inputs.
shared_owner_sequence() {
    local status
    local -a grants=(--read-only --cap-drop ALL --security-opt no-new-privileges:true
        --cpus 1 --memory 128m --memory-swap 128m --pids-limit 32
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m
        --env PYTHONDONTWRITEBYTECODE=1
        --mount "type=bind,src=$fixture,dst=/fixture/probe.py,readonly" --entrypoint python3)
    local -a cold=(-I -B /fixture/probe.py session-offline 3)
    acquisition_worker --timeout 30 offline status "$base_image" grants cold || return "$?"
    [[ $status == 3 ]] || return 2
    acquisition_set_route system || return "$?"
    local -a online=(-I -B /fixture/probe.py session-worker "$origin_port")
    acquisition_worker --timeout 30 online status "$base_image" grants online || return "$?"
    [[ $status == 0 ]] || return 2
    local -a final=(-I -B /fixture/probe.py session-offline 0)
    acquisition_worker --timeout 30 offline status "$base_image" grants final || return "$?"
    [[ $status == 0 ]]
}
owner_started=true
acquisition_scope --endpoint "$NMR_SELECTED_DOCKER_ENDPOINT" --bootstrap-image "$base_image" \
    --retired-receipt "$owner_receipt" -- shared_owner_sequence > "$operation_directory/worker-result"
cmp -s "$owner_receipt" <(printf 'retired\n')
python3 -I -B - "$operation_directory/worker-result" <<'RESULT'
import json, pathlib, sys
result = json.loads(pathlib.Path(sys.argv[1]).read_text())
if (set(result) != {"netns", "http", "connect"} or type(result["netns"]) is not int
        or result["http"] is not True or result["connect"] is not True):
    raise SystemExit("Shared owner worker did not prove both proxy paths with JSON-only stdout.")
RESULT
printf -v operation_success 'Shared acquisition owner network test passed on the selected engine.\n'
