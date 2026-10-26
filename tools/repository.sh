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
case "$1" in
    tests/repository) suite=tests/repository ;;
    tests/repository/quality) suite=tests/repository/quality ;;
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
if [[ ! -f config/checker-image.txt || -L config/checker-image.txt ]]; then
    printf '%s\n' 'Cannot select the checker image: config/checker-image.txt must be a regular, non-symlink file.' >&2
    exit 2
fi
image=$(cat config/checker-image.txt)
if [[ ! $image =~ ^sha256:[0-9a-f]{64}$ ]]; then
    printf '%s\n' 'Cannot select the checker image: config/checker-image.txt must contain one immutable sha256 image ID.' >&2
    exit 2
fi
mounts=()
shopt -s dotglob nullglob
for entry in ./*; do
    name=${entry#./}
    case "$name" in .git|.agents|.codex|.aws|.work|secrets) continue ;; esac
    if [[ -L $entry || ! -f $entry && ! -d $entry || $name =~ [[:cntrl:],] ]]; then
        printf 'Cannot grant repository source %q: public entries must be regular files or directories with mountable names.\n' "$name" >&2
        exit 2
    fi
    mounts+=(--mount "type=bind,src=$root/$name,dst=/workspace/$name,readonly")
done

# The preview does not create files or contact an engine. The real invocation
# substitutes a private host snapshot of root names/types, without their contents.
metadata=/tmp/nmr-provider-tools-root-entries
client=/tmp/nmr-provider-tools-docker-client
if [[ $printing == 0 ]]; then
    client=$(mktemp -d /tmp/nmr-provider-tools-checker.XXXXXXXX)
    trap 'rm -rf -- "$client"' EXIT
    chmod 755 "$client"
    metadata=$client/root-entries
    find . -mindepth 1 -maxdepth 1 -printf '%y\t%f\0' > "$metadata"
    chmod 444 "$metadata"
fi
command=(env -i "PATH=/usr/bin:/bin" "HOME=$client" /usr/bin/docker
    --host=unix:///var/run/docker.sock --config "$client"
    run --rm --init --pull=never --network=none --read-only
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
    command+=("${module//\//.}")
else
    command+=(discover -s "$suite" -t . -v)
fi
if [[ $printing == 1 ]]; then
    printf '%q ' "${command[@]}"
    printf '\n'
    exit 0
fi
"${command[@]}"
