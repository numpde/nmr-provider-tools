#!/usr/bin/env bash
# This launcher grants the checker public source reads and disposable /tmp only.
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd -- "$root"
printing=0
if [[ $# -gt 0 && $1 == --print ]]; then
    printing=1
    shift
fi
if [[ $# != 1 ]]; then
    printf '%s\n' 'Cannot select repository checks: supply one maintained tests/repository target.' >&2
    exit 2
fi
pattern='test*.py'
case "$1" in
    tests/repository) suite=tests/repository ;;
    tests/repository/quality) suite=tests/repository/quality ;;
    tests/repository/provisioning) suite=tests/repository; pattern='test_checker*.py' ;;
    tests/repository/runtime) suite=tests/repository/test_container_port_runtime.py ;;
    *) printf 'Cannot select repository checks: unknown target %q. Use make help.\n' "$1" >&2; exit 2 ;;
esac
if [[ $root =~ [[:cntrl:],] ]]; then
    printf '%s\n' 'Cannot mount repository source: its physical path contains a control character or comma.' >&2
    exit 2
fi
uid=$(id -u)
gid=$(id -g)
if [[ $uid == 0 || $gid == 0 ]]; then
    printf '%s\n' 'Cannot start repository checks as UID or GID zero; use a non-root caller.' >&2
    exit 2
fi
source "$root/tools/checker_inputs.sh"
fingerprint=$(checker_fingerprint)
image="nmr-provider-tools/checker:$fingerprint"
mounts=()
shopt -s dotglob nullglob
for entry in ./*; do
    name=${entry#./}
    case "$name" in .git|.agents|.codex|.aws|secrets) continue ;; esac
    if [[ -L $entry || ! -f $entry && ! -d $entry || $name =~ [[:cntrl:],] ]]; then
        printf 'Cannot grant repository source %q: public entries must be regular files or directories with mountable names.\n' "$name" >&2
        exit 2
    fi
    mounts+=(--mount "type=bind,src=$root/$name,dst=/workspace/$name,readonly")
done

# The preview does not create files or contact an engine. The real invocation
# substitutes a private host snapshot of root names/types, without their contents.
metadata=/tmp/nmr-provider-tools-root-entries
source "$root/tools/checker_operation.sh"
if [[ $printing == 1 ]]; then
    if ! docker_client_prepare print; then printf '%s' "$docker_client_diagnostic" >&2; exit 2; fi
else
    checker_begin
    checker_admit_image "$fingerprint"
    image=$checker_image
    metadata=$operation_directory/root-entries
    find . -mindepth 1 -maxdepth 1 -printf '%y\t%f\0' > "$metadata"
    chmod 444 "$metadata"
fi
arguments=(--init --pull=never --network=none --read-only
    --cap-drop=ALL --security-opt=no-new-privileges
    --pids-limit=64 --memory=512m --memory-swap=512m --cpus=2
    --user "$uid:$gid" --tmpfs /tmp:rw,noexec,nosuid,nodev,size=67108864
    --workdir /workspace --env HOME=/tmp --env PYTHONDONTWRITEBYTECODE=1
    --env PYTHONPATH=/workspace --env NMR_PROVIDER_TOOLS_IN_CONTAINER=1
    --env "NMR_PROVIDER_TOOLS_EXPECTED_UID=$uid" --env "NMR_PROVIDER_TOOLS_EXPECTED_GID=$gid"
    "${mounts[@]}"
    --mount "type=bind,src=$metadata,dst=/repository-source-inventory/root-entries,readonly"
    --entrypoint python3 "$image" -B -m unittest)
if [[ $suite == *.py ]]; then
    module=${suite%.py}
    arguments+=("${module//\//.}")
else
    arguments+=(discover -s "$suite" -t . -p "$pattern" -v)
fi
if [[ $printing == 1 ]]; then
    printf '%q ' "${engine[@]}" create --label org.nmr-provider-tools.operation=PRINT --cidfile /tmp/nmr-provider-tools.PRINT/container.cid "${arguments[@]}"
    printf '\n'
    exit 0
fi
checker_run untimed "${arguments[@]}"
